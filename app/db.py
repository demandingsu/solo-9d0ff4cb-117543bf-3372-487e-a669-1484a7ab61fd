"""SQLite connection handling and schema management.

Concurrency model
-----------------
SQLite in WAL mode allows multiple readers concurrently with one writer.
Every state-changing request runs inside an explicit ``BEGIN IMMEDIATE``
transaction, which acquires the reserved (write) lock up front. Combined
with the busy timeout, uploads and withdrawals are serialized against one
another and each sees a fully committed state. Reads use their own
snapshot transaction, so a query never observes a half-written path:
paths are *derived* from committed event rows at read time, there is no
separate path table that could be updated partially.

Loan check-in (``loan_*`` tables) uses the opposite shape deliberately:
the discrepancy table is *materialized*. A box-count write updates the
stored count rows and the discrepancy rows in the same transaction, so a
failed re-evaluation rolls both back and the previous result survives.
Queries read the stored discrepancy table directly instead of
recomputing it; the re-evaluation only touches artifacts appearing in
the box's old/new lists, plus a bulk pending<->shortage flip of still
missing manifest artifacts when the all-boxes-checked state changes.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT    NOT NULL PRIMARY KEY,
    visitor_id    TEXT    NOT NULL,
    page_view_id  TEXT    NOT NULL,
    exhibit_id    TEXT    NOT NULL,
    event_type    TEXT    NOT NULL CHECK (event_type IN ('open', 'close')),
    event_time    TEXT    NOT NULL,
    received_at   TEXT    NOT NULL
);

-- Hot lookup paths: pairing by page view, path ordering by open time,
-- and per-visitor scans / consent checks.
CREATE INDEX IF NOT EXISTS idx_events_visitor_time
    ON events (visitor_id, event_time);
CREATE INDEX IF NOT EXISTS idx_events_visitor_page
    ON events (visitor_id, page_view_id);

CREATE TABLE IF NOT EXISTS withdrawals (
    visitor_id  TEXT    NOT NULL PRIMARY KEY,
    withdrawn_at TEXT   NOT NULL
);

-- =======================================================================
-- 借展器物到馆点交：发运箱单、逐箱实点清单与物化差异表
-- =======================================================================

-- 一批借展（一张发运箱单对应一个 batch）。
CREATE TABLE IF NOT EXISTS loan_batches (
    batch_id    TEXT    NOT NULL PRIMARY KEY,
    created_at  TEXT    NOT NULL
);

-- 发运箱单：每个器物号在一个 batch 内只归一个箱（主键唯一兜底）。
CREATE TABLE IF NOT EXISTS loan_manifest (
    batch_id    TEXT    NOT NULL,
    artifact_id TEXT    NOT NULL,
    expected_box TEXT   NOT NULL,
    PRIMARY KEY (batch_id, artifact_id),
    FOREIGN KEY (batch_id) REFERENCES loan_batches(batch_id)
);

-- 每个箱最多一份现行实点清单；整份替换（可更正），撤销即删行。
-- 空清单（items = '[]'）也落行，表示该箱已点且实点为空。
CREATE TABLE IF NOT EXISTS loan_box_counts (
    batch_id    TEXT    NOT NULL,
    box_id      TEXT    NOT NULL,
    items       TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (batch_id, box_id),
    FOREIGN KEY (batch_id) REFERENCES loan_batches(batch_id)
);

-- 物化差异表：查询直接读这张表，不做现场推导。
-- 与实点清单在同一写事务内一并更新，失败整体回滚、保留原结果。
CREATE TABLE IF NOT EXISTS loan_discrepancies (
    batch_id       TEXT    NOT NULL,
    artifact_id    TEXT    NOT NULL,
    expected_box   TEXT,
    observed_boxes TEXT    NOT NULL,  -- JSON 数组；箱内清单去重后的实见箱
    conclusion     TEXT    NOT NULL CHECK (
        conclusion IN ('ok', 'conflict', 'misboxed',
                       'extra', 'pending', 'shortage')
    ),
    updated_at     TEXT    NOT NULL,
    PRIMARY KEY (batch_id, artifact_id),
    FOREIGN KEY (batch_id) REFERENCES loan_batches(batch_id)
);

-- 按箱重核与状态批量翻转时的热路径。
CREATE INDEX IF NOT EXISTS idx_loan_manifest_batch
    ON loan_manifest (batch_id);
CREATE INDEX IF NOT EXISTS idx_loan_counts_batch
    ON loan_box_counts (batch_id);
CREATE INDEX IF NOT EXISTS idx_loan_discrepancies_batch
    ON loan_discrepancies (batch_id, conclusion);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a connection with the pragmas required for safe concurrency."""
    conn = sqlite3.connect(
        db_path or config.DB_PATH,
        timeout=config.BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # manual transaction control
        # Sync FastAPI endpoints run in uvicorn's worker threadpool, so the
        # connection created by the dependency may be touched from a pool
        # thread. Each request gets its own connection (never shared), which
        # makes cross-thread use safe; SQLite file locking + BEGIN IMMEDIATE
        # still serialize the actual writers.
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str | None = None) -> None:
    """Create tables and indexes if they do not yet exist."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_connection() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, closed afterwards.

    FastAPI consumes this generator directly: the yielded connection is
    injected into the endpoint, and the ``finally`` block runs when the
    request finishes. Do not wrap it in ``@contextmanager`` — doing so hides
    the generator signature from FastAPI's dependency analysis.
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Acquire the write lock immediately and commit/rollback atomically."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
