"""借展器物到馆点交的端到端测试。

覆盖需求矩阵：
  1. 发运箱单录入：器物跨箱重复拒绝、批次重复录入拒绝、待查初值
  2. 按箱整份点交：相符、空清单算已点、更正（整份替换）
  3. 差异结论：冲突（跨箱重复实见）、错箱（只在别箱找到）、
     多出（箱单未列单箱实见）、待查 -> 全部点完 -> 短缺
  4. 撤销一箱点交：短缺回退待查、多出器物差异行删除、幂等撤销
  5. 增量重核范围：只动新旧清单涉及的器物
  6. 原子性：差异表更新失败时实点记录一并回滚，保留原结果
  7. 查询直接读物化差异表；箱点交状态与 summary 计数
  8. 多线程并发点交/查询的一致性
"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import config, loan_crud
from app.db import connect
from app.main import app


def _manifest(batch_id, boxes):
    return {"batch_id": batch_id, "boxes": boxes}


def _box(box_id, *artifact_ids):
    return {"box_id": box_id, "artifact_ids": list(artifact_ids)}


def _disc_map(report: dict) -> dict[str, dict]:
    return {row["artifact_id"]: row for row in report["discrepancies"]}


def _record_simple(client, batch_id="B1", boxes=(("C1", ("A1", "A2")), ("C2", ("A3",)))):
    payload = _manifest(
        batch_id, [_box(box_id, *arts) for box_id, arts in boxes]
    )
    resp = client.post(f"/loan/batches/{batch_id}/manifest", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 1. 发运箱单录入
# ---------------------------------------------------------------------------
def test_manifest_records_and_initializes_pending(client):
    body = _record_simple(client, "B1")
    assert body == {
        "batch_id": "B1",
        "box_count": 2,
        "artifact_count": 3,
        "all_boxes_checked": False,
    }

    report = client.get("/loan/batches/B1/discrepancies").json()
    assert report["all_boxes_checked"] is False
    assert {b["box_id"]: b["checked"] for b in report["boxes"]} == {
        "C1": False,
        "C2": False,
    }
    # 箱单器物先落待查，应在箱来自箱单，实见箱为空。
    rows = _disc_map(report)
    assert set(rows) == {"A1", "A2", "A3"}
    assert rows["A1"]["expected_box"] == "C1"
    assert rows["A1"]["observed_boxes"] == []
    assert rows["A1"]["conclusion"] == "pending"
    assert rows["A1"]["conclusion_label"] == "待查"
    assert report["summary"] == {"pending": 3}


def test_manifest_rejects_artifact_in_multiple_boxes_atomically(client):
    payload = _manifest("B-dup", [_box("C1", "A1", "A2"), _box("C2", "A2", "A3")])
    resp = client.post("/loan/batches/B-dup/manifest", json=payload)
    assert resp.status_code == 400
    assert "A2" in resp.json()["detail"]

    # 整批回滚：批次不存在，差异表没有半成品。
    assert client.get("/loan/batches/B-dup/discrepancies").status_code == 404
    conn = connect(config.DB_PATH)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM loan_manifest WHERE batch_id = 'B-dup'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_manifest_cannot_be_recorded_twice(client):
    _record_simple(client, "B1")
    resp = client.post(
        "/loan/batches/B1/manifest",
        json=_manifest("B1", [_box("C1", "A1")]),
    )
    assert resp.status_code == 409


def test_manifest_validation_empty_boxes(client):
    resp = client.post(
        "/loan/batches/B-empty/manifest",
        json=_manifest("B-empty", []),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 2. 按箱整份点交：相符 / 空清单 / 更正
# ---------------------------------------------------------------------------
def test_happy_count_marks_ok_and_empty_list_counts_as_checked(client):
    _record_simple(client, "B1")
    # C1 实点与箱单一致。
    r1 = client.put("/loan/batches/B1/boxes/C1/count", json={"artifact_ids": ["A1", "A2"]})
    assert r1.status_code == 200
    body = r1.json()
    assert body["replaced"] is False
    assert body["all_boxes_checked"] is False
    assert body["reevaluated_artifacts"] == ["A1", "A2"]

    report = client.get("/loan/batches/B1/discrepancies").json()
    rows = _disc_map(report)
    assert rows["A1"]["conclusion"] == "ok"
    assert rows["A1"]["observed_boxes"] == ["C1"]
    assert rows["A2"]["conclusion"] == "ok"
    assert rows["A3"]["conclusion"] == "pending"

    # 空清单也算已点：C2 点交为空。
    r2 = client.put("/loan/batches/B1/boxes/C2/count", json={"artifact_ids": []})
    assert r2.status_code == 200
    assert r2.json()["all_boxes_checked"] is True
    report = client.get("/loan/batches/B1/discrepancies").json()
    c2 = next(b for b in report["boxes"] if b["box_id"] == "C2")
    assert c2["checked"] is True and c2["artifact_ids"] == []
    # 全部箱点完，A3 没找到 -> 短缺（不再是待查）。
    rows = _disc_map(report)
    assert rows["A3"]["conclusion"] == "shortage"
    assert rows["A3"]["conclusion_label"] == "短缺"
    assert report["summary"] == {"ok": 2, "shortage": 1}


def test_count_correction_replaces_whole_list_and_only_reevaluates_affected(client):
    _record_simple(client, "B1", boxes=(("C1", ("A1", "A2")), ("C2", ("A3",))))
    client.put("/loan/batches/B1/boxes/C1/count", json={"artifact_ids": ["A1", "A2"]})
    client.put("/loan/batches/B1/boxes/C2/count", json={"artifact_ids": ["A3"]})
    report = client.get("/loan/batches/B1/discrepancies").json()
    assert report["summary"] == {"ok": 3}
    a3_updated = _disc_map(report)["A3"]

    # 更正 C1：漏掉 A2。重核范围只含 A1/A2，A3 不在列表里。
    resp = client.put(
        "/loan/batches/B1/boxes/C1/count", json={"artifact_ids": ["A1"]}
    )
    body = resp.json()
    assert body["replaced"] is True
    assert body["reevaluated_artifacts"] == ["A1", "A2"]
    assert body["all_boxes_checked"] is True  # C2 仍点着，点齐状态不变

    report = client.get("/loan/batches/B1/discrepancies").json()
    rows = _disc_map(report)
    assert rows["A1"]["conclusion"] == "ok"
    assert rows["A2"]["conclusion"] == "shortage"  # 点齐后仍未找到
    assert rows["A3"]["conclusion"] == "ok"
    # 箱状态里 C1 的现行清单已被整份替换。
    c1 = next(b for b in report["boxes"] if b["box_id"] == "C1")
    assert c1["artifact_ids"] == ["A1"]
    # 未参与重核的 A3 行内容保持不变。
    assert _disc_map(report)["A3"] == a3_updated


def test_duplicate_ids_inside_one_count_are_deduped(client):
    _record_simple(client, "B1")
    resp = client.put(
        "/loan/batches/B1/boxes/C1/count",
        json={"artifact_ids": ["A1", "A1", "A2", "A2"]},
    )
    assert resp.status_code == 200
    assert resp.json()["artifact_count"] == 2
    report = client.get("/loan/batches/B1/discrepancies").json()
    c1 = next(b for b in report["boxes"] if b["box_id"] == "C1")
    assert c1["artifact_ids"] == ["A1", "A2"]


# ---------------------------------------------------------------------------
# 3. 差异结论：冲突 / 错箱 / 多出
# ---------------------------------------------------------------------------
def test_cross_box_duplicate_observation_reports_conflict(client):
    # 箱单：A1 应在 C1，A2 应在 C2。
    _record_simple(client, "B1", boxes=(("C1", ("A1",)), ("C2", ("A2",))))
    client.put(
        "/loan/batches/B1/boxes/C1/count", json={"artifact_ids": ["A1", "A2"]}
    )
    client.put(
        "/loan/batches/B1/boxes/C2/count", json={"artifact_ids": ["A2"]}
    )
    report = client.get("/loan/batches/B1/discrepancies").json()
    rows = _disc_map(report)
    # A2 在两个箱实见 -> 冲突，即使其中一个是应在箱。
    assert rows["A2"]["conclusion"] == "conflict"
    assert rows["A2"]["conclusion_label"] == "冲突"
    assert rows["A2"]["observed_boxes"] == ["C1", "C2"]
    assert rows["A1"]["conclusion"] == "ok"
    assert report["summary"]["conflict"] == 1


def test_misboxed_when_only_found_in_another_box(client):
    _record_simple(client, "B1", boxes=(("C1", ("A1",)), ("C2", ("A2",))))
    # C1 空箱已点；A1 只在 C2 出现。
    client.put("/loan/batches/B1/boxes/C1/count", json={"artifact_ids": []})
    client.put(
        "/loan/batches/B1/boxes/C2/count", json={"artifact_ids": ["A1", "A2"]}
    )
    rows = _disc_map(client.get("/loan/batches/B1/discrepancies").json())
    assert rows["A1"]["conclusion"] == "misboxed"
    assert rows["A1"]["conclusion_label"] == "错箱"
    assert rows["A1"]["expected_box"] == "C1"
    assert rows["A1"]["observed_boxes"] == ["C2"]
    assert rows["A2"]["conclusion"] == "ok"


def test_extra_artifact_not_on_manifest_single_box(client):
    _record_simple(client, "B1")
    client.put(
        "/loan/batches/B1/boxes/C1/count",
        json={"artifact_ids": ["A1", "A2", "X9"]},
    )
    rows = _disc_map(client.get("/loan/batches/B1/discrepancies").json())
    assert rows["X9"]["conclusion"] == "extra"
    assert rows["X9"]["conclusion_label"] == "多出"
    assert rows["X9"]["expected_box"] is None
    assert rows["X9"]["observed_boxes"] == ["C1"]


def test_pending_stays_pending_until_all_boxes_checked_then_shortage(client):
    _record_simple(client, "B1", boxes=(("C1", ("A1",)), ("C2", ("A2",))))
    client.put("/loan/batches/B1/boxes/C1/count", json={"artifact_ids": ["A1"]})
    # 还有 C2 没点：A2 只是待查，不能提前报短缺。
    rows = _disc_map(client.get("/loan/batches/B1/discrepancies").json())
    assert rows["A2"]["conclusion"] == "pending"

    client.put("/loan/batches/B1/boxes/C2/count", json={"artifact_ids": []})
    rows = _disc_map(client.get("/loan/batches/B1/discrepancies").json())
    assert rows["A2"]["conclusion"] == "shortage"


# ---------------------------------------------------------------------------
# 4. 撤销一箱点交
# ---------------------------------------------------------------------------
def test_revoke_box_count_rolls_back_shortage_and_extra(client):
    _record_simple(client, "B1", boxes=(("C1", ("A1",)), ("C2", ("A2",))))
    # C1 点出 A1 与箱单外的 X9；C2 空点 -> 全部点完。
    client.put(
        "/loan/batches/B1/boxes/C1/count", json={"artifact_ids": ["A1", "X9"]}
    )
    client.put("/loan/batches/B1/boxes/C2/count", json={"artifact_ids": []})
    before = _disc_map(client.get("/loan/batches/B1/discrepancies").json())
    assert before["X9"]["conclusion"] == "extra"
    assert before["A2"]["conclusion"] == "shortage"

    # 撤销 C1：X9 失去唯一实见 -> 差异行删除；A1 回到待查；
    # 批次不再点齐 -> A2 从短缺回退待查。
    resp = client.delete("/loan/batches/B1/boxes/C1/count")
    body = resp.json()
    assert body["was_checked"] is True
    assert body["all_boxes_checked"] is False
    assert body["reevaluated_artifacts"] == ["A1", "X9"]

    report = client.get("/loan/batches/B1/discrepancies").json()
    rows = _disc_map(report)
    assert "X9" not in rows  # 多出器物随撤销消失
    assert rows["A1"]["conclusion"] == "pending"
    assert rows["A1"]["observed_boxes"] == []
    assert rows["A2"]["conclusion"] == "pending"
    c1 = next(b for b in report["boxes"] if b["box_id"] == "C1")
    assert c1["checked"] is False and c1["artifact_ids"] is None

    # 重新点交 C1 后结论恢复，证明撤销没有破坏其他状态。
    client.put(
        "/loan/batches/B1/boxes/C1/count", json={"artifact_ids": ["A1", "X9"]}
    )
    rows = _disc_map(client.get("/loan/batches/B1/discrepancies").json())
    assert rows["X9"]["conclusion"] == "extra"
    assert rows["A2"]["conclusion"] == "shortage"


def test_revoke_is_idempotent_and_unknown_box_rejected(client):
    _record_simple(client, "B1")
    first = client.delete("/loan/batches/B1/boxes/C1/count").json()
    assert first["was_checked"] is False and first["reevaluated_artifacts"] == []
    second = client.delete("/loan/batches/B1/boxes/C1/count").json()
    assert second["was_checked"] is False

    # 箱号不在发运箱单内：提交点交被拒，不能凭空造箱。
    resp = client.put(
        "/loan/batches/B1/boxes/NOPE/count", json={"artifact_ids": []}
    )
    assert resp.status_code == 400


def test_unknown_batch_returns_404(client):
    assert client.get("/loan/batches/GONE/discrepancies").status_code == 404
    assert client.put(
        "/loan/batches/GONE/boxes/C1/count", json={"artifact_ids": []}
    ).status_code == 404
    assert client.delete("/loan/batches/GONE/boxes/C1/count").status_code == 404


# ---------------------------------------------------------------------------
# 5. 原子性：差异表更新失败 -> 实点记录一并回滚，保留原结果
# ---------------------------------------------------------------------------
def test_failed_discrepancy_update_rolls_back_count(client, monkeypatch):
    _record_simple(client, "B1")
    client.put(
        "/loan/batches/B1/boxes/C1/count", json={"artifact_ids": ["A1", "A2"]}
    )
    original = client.get("/loan/batches/B1/discrepancies").json()

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated discrepancy write failure")

    monkeypatch.setattr(loan_crud, "_upsert_discrepancy", boom)

    # TestClient 默认把服务端异常抛出；write_transaction 必须已回滚。
    with pytest.raises(RuntimeError, match="simulated"):
        client.put(
            "/loan/batches/B1/boxes/C1/count", json={"artifact_ids": ["A1"]}
        )

    # 实点清单仍是旧内容，差异表也保留原结果。
    conn = connect(config.DB_PATH)
    try:
        raw = conn.execute(
            "SELECT items FROM loan_box_counts WHERE batch_id = 'B1' AND box_id = 'C1'"
        ).fetchone()
        assert json.loads(raw["items"]) == ["A1", "A2"]
    finally:
        conn.close()
    after = client.get("/loan/batches/B1/discrepancies").json()
    assert after == original


# ---------------------------------------------------------------------------
# 6. 并发：多线程点交不同箱 + 持续查询，结果始终自洽
# ---------------------------------------------------------------------------
def test_concurrent_counts_always_present_consistent_report(client):
    _record_simple(
        client,
        "B1",
        boxes=(("C1", ("A1",)), ("C2", ("A2",)), ("C3", ("A3",))),
    )
    errors: list[str] = []
    barrier = threading.Barrier(4)

    def counter(box_id: str, artifacts: list[str]) -> None:
        barrier.wait()
        try:
            with TestClient(app) as local:
                r = local.put(
                    f"/loan/batches/B1/boxes/{box_id}/count",
                    json={"artifact_ids": artifacts},
                )
                assert r.status_code == 200, r.text
        except Exception as exc:  # pragma: no cover
            errors.append(f"counter {box_id}: {exc!r}")

    def reader() -> None:
        barrier.wait()
        try:
            with TestClient(app) as local:
                for _ in range(80):
                    report = local.get("/loan/batches/B1/discrepancies").json()
                    # 不变量：任一器物的结论必须合法，且待查/短缺与点齐状态自洽。
                    for row in report["discrepancies"]:
                        assert row["conclusion"] in (
                            "ok", "conflict", "misboxed",
                            "extra", "pending", "shortage",
                        )
                        if not report["all_boxes_checked"]:
                            assert row["conclusion"] != "shortage"
        except Exception as exc:  # pragma: no cover
            errors.append(f"reader: {exc!r}")

    threads = [
        threading.Thread(target=counter, args=("C1", ["A1", "A2"])),  # A2 跨箱
        threading.Thread(target=counter, args=("C2", ["A2"])),
        threading.Thread(target=counter, args=("C3", ["A3"])),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors

    report = client.get("/loan/batches/B1/discrepancies").json()
    assert report["all_boxes_checked"] is True
    rows = _disc_map(report)
    assert rows["A1"]["conclusion"] == "ok"
    assert rows["A2"]["conclusion"] == "conflict"  # 跨箱重复实见
    assert rows["A3"]["conclusion"] == "ok"
