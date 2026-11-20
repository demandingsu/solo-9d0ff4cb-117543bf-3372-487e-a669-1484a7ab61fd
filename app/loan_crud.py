"""借展器物点交领域逻辑：发运箱单、逐箱实点清单与差异表维护。

事务边界由调用方（路由层）通过 :func:`app.db.write_transaction` 控制，
每个 API 请求是一个 ``BEGIN IMMEDIATE`` 原子事务：实点清单与差异表
要么一并更新成功，要么整体回滚保留原结果。

重核范围（需求约束）
--------------------
提交/撤销一箱时，只重核该箱新旧两份清单涉及到的器物；当
“全部箱已点完”这一状态发生翻转时，额外更新所有尚未找到的
箱单器物（pending <-> shortage），其余器物的差异行原样保留。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .loan_models import CONCLUSION_LABELS


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# 领域错误：路由层映射为 400 / 404 / 409
# ---------------------------------------------------------------------------


class LoanError(Exception):
    """点交领域错误基类。"""


class BatchNotFound(LoanError):
    """批次（发运箱单）不存在。"""


class ManifestAlreadyExists(LoanError):
    """该批次已录过发运箱单，箱单不可重复录入。"""


class UnknownBox(LoanError):
    """提交点交的箱不在发运箱单内。"""


class DuplicateArtifactInManifest(LoanError):
    """同一器物号在发运箱单里被归到了多个箱。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _dump_items(artifact_ids: list[str]) -> str:
    # 去重 + 排序后落库，保证清单内容稳定、差异比较确定。
    return json.dumps(sorted(set(artifact_ids)), ensure_ascii=False)


def _load_items(raw: str | None) -> list[str]:
    if raw is None:
        return []
    return json.loads(raw)


def _batch_exists(conn: sqlite3.Connection, batch_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM loan_batches WHERE batch_id = ? LIMIT 1", (batch_id,)
        ).fetchone()
        is not None
    )


def _load_manifest_map(conn: sqlite3.Connection, batch_id: str) -> dict[str, str]:
    """返回 {器物号: 应在箱}，只含发运箱单上的器物。"""
    rows = conn.execute(
        "SELECT artifact_id, expected_box FROM loan_manifest WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    return {row["artifact_id"]: row["expected_box"] for row in rows}


def _load_all_observations(
    conn: sqlite3.Connection, batch_id: str
) -> dict[str, list[str]]:
    """扫描全部已提交箱的实点清单，返回 {器物号: 实见箱列表（排序去重）}。"""
    rows = conn.execute(
        "SELECT box_id, items FROM loan_box_counts WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    seen: dict[str, set[str]] = {}
    for row in rows:
        for artifact_id in _load_items(row["items"]):
            seen.setdefault(artifact_id, set()).add(row["box_id"])
    return {artifact_id: sorted(boxes) for artifact_id, boxes in seen.items()}


def _all_manifest_boxes(conn: sqlite3.Connection, batch_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT expected_box AS box_id FROM loan_manifest WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    return sorted(row["box_id"] for row in rows)


def _all_checked(conn: sqlite3.Connection, batch_id: str) -> bool:
    """箱单中每个箱都有一份（可能为空的）实点清单，才算全部点完。"""
    total = conn.execute(
        "SELECT COUNT(DISTINCT expected_box) FROM loan_manifest WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()[0]
    checked = conn.execute(
        "SELECT COUNT(*) FROM loan_box_counts WHERE batch_id = ?", (batch_id,)
    ).fetchone()[0]
    return checked >= total and total > 0


# ---------------------------------------------------------------------------
# 差异判定（纯函数，便于单测与推理）
# ---------------------------------------------------------------------------


def _classify(
    expected_box: str | None,
    observed_boxes: list[str],
    all_checked: bool,
) -> str | None:
    """返回 conclusion token；返回 None 表示该器物不再需要差异行（多出且已无实见）。"""
    if observed_boxes:
        if len(observed_boxes) > 1:
            # 多箱实见优先报冲突，即使其中一个恰好是应在箱。
            return "conflict"
        only_box = observed_boxes[0]
        if expected_box is None:
            # 箱单没列、只在一个箱实见 -> 多出。
            return "extra"
        if only_box != expected_box:
            # 只在别箱找到 -> 错箱。
            return "misboxed"
        return "ok"

    # 完全没找到。
    if expected_box is None:
        # 非箱单器物又没有任何实见：差异行应删除。
        return None
    # 箱单列了但没找到：未全部点完先待查，点齐才报短缺。
    return "shortage" if all_checked else "pending"


def _upsert_discrepancy(
    conn: sqlite3.Connection,
    batch_id: str,
    artifact_id: str,
    expected_box: str | None,
    observed_boxes: list[str],
    conclusion: str,
) -> None:
    conn.execute(
        """
        INSERT INTO loan_discrepancies
            (batch_id, artifact_id, expected_box, observed_boxes,
             conclusion, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(batch_id, artifact_id) DO UPDATE SET
            expected_box = excluded.expected_box,
            observed_boxes = excluded.observed_boxes,
            conclusion = excluded.conclusion,
            updated_at = excluded.updated_at
        """,
        (
            batch_id,
            artifact_id,
            expected_box,
            json.dumps(observed_boxes, ensure_ascii=False),
            conclusion,
            utc_now_iso(),
        ),
    )


def _reevaluate_artifacts(
    conn: sqlite3.Connection,
    batch_id: str,
    affected: set[str],
    manifest: dict[str, str],
    observations: dict[str, list[str]],
    all_checked: bool,
) -> None:
    """只重核给定器物：重算结论并 upsert/删除对应差异行。"""
    for artifact_id in sorted(affected):
        expected_box = manifest.get(artifact_id)
        observed_boxes = observations.get(artifact_id, [])
        conclusion = _classify(expected_box, observed_boxes, all_checked)
        if conclusion is None:
            # 箱单外器物且已无任何实见（例如撤销了唯一见到它的箱）。
            conn.execute(
                "DELETE FROM loan_discrepancies WHERE batch_id = ? AND artifact_id = ?",
                (batch_id, artifact_id),
            )
            continue
        _upsert_discrepancy(
            conn, batch_id, artifact_id, expected_box, observed_boxes, conclusion
        )


def _flip_unfound_manifest_artifacts(
    conn: sqlite3.Connection, batch_id: str, all_checked: bool
) -> None:
    """全部箱点完状态翻转时，更新仍未找到的箱单器物。

    - 点齐（false -> true）：pending 升级为 shortage。
    - 撤销导致不再点齐（true -> false）：shortage 回退为 pending。
    已找到的器物结论（ok/错箱/冲突）不受影响。
    """
    target = "shortage" if all_checked else "pending"
    source = "pending" if all_checked else "shortage"
    conn.execute(
        """
        UPDATE loan_discrepancies
           SET conclusion = ?, updated_at = ?
         WHERE batch_id = ? AND conclusion = ?
        """,
        (target, utc_now_iso(), batch_id, source),
    )


# ---------------------------------------------------------------------------
# 用例 1：录入发运箱单
# ---------------------------------------------------------------------------


def record_manifest(conn: sqlite3.Connection, payload) -> dict:
    """录入整批发运箱单并为每个箱单器物建立 pending 差异行。

    必须在写事务内调用。箱单只允许录一次；器物号跨箱重复直接拒绝。
    """
    batch_id = payload.batch_id
    if _batch_exists(conn, batch_id):
        raise ManifestAlreadyExists(f"批次 {batch_id!r} 已存在发运箱单，不可重复录入")

    now = utc_now_iso()
    conn.execute(
        "INSERT INTO loan_batches (batch_id, created_at) VALUES (?, ?)",
        (batch_id, now),
    )

    total_artifacts = 0
    seen_artifacts: set[str] = set()
    for box in payload.boxes:
        for artifact_id in set(box.artifact_ids):
            if artifact_id in seen_artifacts:
                # 主键 (batch_id, artifact_id) 也会兜底，领域层先给明确中文报错。
                raise DuplicateArtifactInManifest(
                    f"器物号 {artifact_id!r} 在发运箱单中被归入多个箱，"
                    "每个器物号只能归一个箱"
                )
            seen_artifacts.add(artifact_id)
            conn.execute(
                """
                INSERT INTO loan_manifest (batch_id, artifact_id, expected_box)
                VALUES (?, ?, ?)
                """,
                (batch_id, artifact_id, box.box_id),
            )
            # 尚未开箱：箱单器物一律先落“待查”，实见箱为空。
            _upsert_discrepancy(conn, batch_id, artifact_id, box.box_id, [], "pending")
            total_artifacts += 1

    return {
        "batch_id": batch_id,
        "box_count": len(payload.boxes),
        "artifact_count": total_artifacts,
        "all_boxes_checked": False,
    }


# ---------------------------------------------------------------------------
# 用例 2：按箱提交整份实点清单（首次点交 / 更正）
# ---------------------------------------------------------------------------


def submit_box_count(conn: sqlite3.Connection, batch_id: str, box_id: str, payload) -> dict:
    """整份替换某箱实点清单，并增量重核差异表。必须在写事务内调用。"""
    if not _batch_exists(conn, batch_id):
        raise BatchNotFound(f"批次 {batch_id!r} 不存在，请先录入发运箱单")

    # 箱号必须来自发运箱单，避免凭空产生箱单外的箱。
    manifest_boxes = set(_all_manifest_boxes(conn, batch_id))
    if box_id not in manifest_boxes:
        raise UnknownBox(
            f"箱号 {box_id!r} 不在批次 {batch_id!r} 的发运箱单内，"
            f"可点交的箱：{sorted(manifest_boxes)}"
        )

    existing = conn.execute(
        "SELECT items FROM loan_box_counts WHERE batch_id = ? AND box_id = ?",
        (batch_id, box_id),
    ).fetchone()
    old_items = _load_items(existing["items"]) if existing is not None else []
    new_items = sorted(set(payload.artifact_ids))  # 箱内重复号去重

    before_all_checked = _all_checked(conn, batch_id)

    # 1) 实点记录落库（upsert，整份覆盖；空清单 '[]' 也算已点）。
    conn.execute(
        """
        INSERT INTO loan_box_counts (batch_id, box_id, items, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(batch_id, box_id) DO UPDATE SET
            items = excluded.items,
            updated_at = excluded.updated_at
        """,
        (batch_id, box_id, _dump_items(new_items), utc_now_iso()),
    )

    after_all_checked = _all_checked(conn, batch_id)

    # 2) 只重核本箱新旧清单涉及的器物。
    affected = set(old_items) | set(new_items)
    manifest = _load_manifest_map(conn, batch_id)
    observations = _load_all_observations(conn, batch_id)
    _reevaluate_artifacts(
        conn, batch_id, affected, manifest, observations, after_all_checked
    )

    # 3) 点齐状态改变时，更新所有尚未找到的箱单器物（待查 <-> 短缺）。
    if before_all_checked != after_all_checked:
        _flip_unfound_manifest_artifacts(conn, batch_id, after_all_checked)

    return {
        "batch_id": batch_id,
        "box_id": box_id,
        "replaced": existing is not None,
        "artifact_count": len(new_items),
        "all_boxes_checked": after_all_checked,
        "reevaluated_artifacts": sorted(affected),
    }


# ---------------------------------------------------------------------------
# 用例 3：撤销一箱点交
# ---------------------------------------------------------------------------


def revoke_box_count(conn: sqlite3.Connection, batch_id: str, box_id: str) -> dict:
    """删除某箱现行实点清单并增量重核。必须在写事务内调用；幂等。"""
    if not _batch_exists(conn, batch_id):
        raise BatchNotFound(f"批次 {batch_id!r} 不存在，请先录入发运箱单")

    existing = conn.execute(
        "SELECT items FROM loan_box_counts WHERE batch_id = ? AND box_id = ?",
        (batch_id, box_id),
    ).fetchone()

    if existing is None:
        # 没有可撤销的清单：无操作，返回当前状态即可。
        return {
            "batch_id": batch_id,
            "box_id": box_id,
            "was_checked": False,
            "all_boxes_checked": _all_checked(conn, batch_id),
            "reevaluated_artifacts": [],
        }

    old_items = _load_items(existing["items"])
    before_all_checked = _all_checked(conn, batch_id)

    conn.execute(
        "DELETE FROM loan_box_counts WHERE batch_id = ? AND box_id = ?",
        (batch_id, box_id),
    )

    after_all_checked = _all_checked(conn, batch_id)

    # 只重核旧清单涉及的器物（撤销没有“新清单”）。
    affected = set(old_items)
    manifest = _load_manifest_map(conn, batch_id)
    observations = _load_all_observations(conn, batch_id)
    _reevaluate_artifacts(
        conn, batch_id, affected, manifest, observations, after_all_checked
    )

    if before_all_checked != after_all_checked:
        _flip_unfound_manifest_artifacts(conn, batch_id, after_all_checked)

    return {
        "batch_id": batch_id,
        "box_id": box_id,
        "was_checked": True,
        "all_boxes_checked": after_all_checked,
        "reevaluated_artifacts": sorted(affected),
    }


# ---------------------------------------------------------------------------
# 用例 4：查询差异表（直接读已存表，不做现场推导）
# ---------------------------------------------------------------------------


def get_discrepancy_report(conn: sqlite3.Connection, batch_id: str) -> dict:
    """在单个只读快照事务内读差异表与箱点交状态。"""
    conn.execute("BEGIN DEFERRED")
    try:
        if not _batch_exists(conn, batch_id):
            conn.rollback()
            raise BatchNotFound(f"批次 {batch_id!r} 不存在，请先录入发运箱单")

        all_checked = _all_checked(conn, batch_id)

        box_rows = conn.execute(
            """
            SELECT m.expected_box AS box_id,
                   c.items        AS items,
                   c.updated_at   AS updated_at
              FROM (SELECT DISTINCT expected_box FROM loan_manifest
                     WHERE batch_id = ?) m
              LEFT JOIN loan_box_counts c
                ON c.batch_id = ? AND c.box_id = m.expected_box
             ORDER BY m.expected_box
            """,
            (batch_id, batch_id),
        ).fetchall()
        boxes = [
            {
                "box_id": row["box_id"],
                "checked": row["items"] is not None,
                "artifact_ids": _load_items(row["items"])
                if row["items"] is not None
                else None,
                "updated_at": row["updated_at"],
            }
            for row in box_rows
        ]

        # 器物行直接来自物化差异表——查询不重新计算结论。
        disc_rows = conn.execute(
            """
            SELECT artifact_id, expected_box, observed_boxes, conclusion
              FROM loan_discrepancies
             WHERE batch_id = ?
             ORDER BY artifact_id
            """,
            (batch_id,),
        ).fetchall()
        discrepancies = []
        summary: dict[str, int] = {}
        for row in disc_rows:
            conclusion = row["conclusion"]
            summary[conclusion] = summary.get(conclusion, 0) + 1
            discrepancies.append(
                {
                    "artifact_id": row["artifact_id"],
                    "expected_box": row["expected_box"],
                    "observed_boxes": json.loads(row["observed_boxes"]),
                    "conclusion": conclusion,
                    "conclusion_label": CONCLUSION_LABELS[conclusion],
                }
            )

        conn.rollback()  # 只读快照
        return {
            "batch_id": batch_id,
            "all_boxes_checked": all_checked,
            "boxes": boxes,
            "discrepancies": discrepancies,
            "summary": summary,
        }
    except Exception:
        conn.rollback()
        raise
