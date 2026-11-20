"""借展器物到馆点交的请求/响应模型。

结论（conclusion）取值与中文标签：

- ``ok``       相符：箱单所列且只在应在箱见到
- ``conflict`` 冲突：同一器物在多个箱实见
- ``misboxed`` 错箱：只在别的箱找到（未在应在箱出现）
- ``extra``    多出：箱单没有该器物，只在一个箱实见
- ``pending``  待查：箱单列了但还没找到，且尚有箱未点完
- ``shortage`` 短缺：全部箱点完仍未找到
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 结论 token -> 中文标签，供 /docs 与响应体直接使用。
CONCLUSION_LABELS: dict[str, str] = {
    "ok": "相符",
    "conflict": "冲突",
    "misboxed": "错箱",
    "extra": "多出",
    "pending": "待查",
    "shortage": "短缺",
}

Conclusion = Literal["ok", "conflict", "misboxed", "extra", "pending", "shortage"]


class ManifestBoxIn(BaseModel):
    """发运箱单中的一个箱及其器物号列表。"""

    model_config = ConfigDict(extra="forbid")

    box_id: str = Field(..., min_length=1, description="发运箱号")
    artifact_ids: list[str] = Field(
        ..., min_length=1, description="该箱应装器物号；同一器物号全批只能归一个箱"
    )


class ManifestIn(BaseModel):
    """整批发运箱单（先于开箱点交录入）。"""

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(..., min_length=1, description="批次号")
    boxes: list[ManifestBoxIn] = Field(..., min_length=1, description="发运箱单，至少一个箱")


class CountIn(BaseModel):
    """按箱提交的整份实点清单（整份替换，空列表表示该箱已点、实点为空）。"""

    model_config = ConfigDict(extra="forbid")

    artifact_ids: list[str] = Field(
        ..., description="本箱实见器物号；箱内重复号去重，整份覆盖旧清单"
    )


class ManifestResult(BaseModel):
    batch_id: str
    box_count: int
    artifact_count: int
    # 录入箱单时尚未开箱，恒为 false；保留字段与查询响应对称。
    all_boxes_checked: bool


class BoxCountStatus(BaseModel):
    box_id: str
    checked: bool
    # 现行实点清单（撤销或从未提交为 None）；空数组表示已点且实点为空。
    artifact_ids: list[str] | None
    updated_at: str | None = None


class CountSubmitResult(BaseModel):
    batch_id: str
    box_id: str
    # 区分首次点交与更正（整份替换）。
    replaced: bool
    artifact_count: int
    all_boxes_checked: bool
    # 本次只重核了新旧清单涉及的器物。
    reevaluated_artifacts: list[str]


class CountRevokeResult(BaseModel):
    batch_id: str
    box_id: str
    # 撤销一份不存在的清单视为幂等无操作。
    was_checked: bool
    all_boxes_checked: bool
    reevaluated_artifacts: list[str]


class DiscrepancyItem(BaseModel):
    """按器物号列出的一条差异（直接读自物化差异表）。"""

    artifact_id: str
    expected_box: str | None = Field(None, description="应在箱；箱单未列的多出器物为 null")
    observed_boxes: list[str] = Field(..., description="实见箱列表，已排序去重")
    conclusion: Conclusion
    conclusion_label: str


class DiscrepancyReport(BaseModel):
    batch_id: str
    all_boxes_checked: bool
    # 箱点交状态：箱单中每个箱一行。
    boxes: list[BoxCountStatus]
    # 按器物号排序的差异表。
    discrepancies: list[DiscrepancyItem]
    summary: dict[str, int] = Field(
        ..., description="各结论计数，键为 conclusion token，值为器物数"
    )
