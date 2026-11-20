"""FastAPI application: event ingestion, visit-path queries, withdrawal."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

from . import crud, loan_crud
from .db import get_connection, init_db, write_transaction
from .loan_models import (
    CountIn,
    CountRevokeResult,
    CountSubmitResult,
    DiscrepancyReport,
    ManifestIn,
    ManifestResult,
)
from .models import (
    ErrorResponse,
    EventBatchIn,
    EventIn,
    IngestBatchResult,
    IngestResult,
    VisitPath,
    WithdrawRequest,
    WithdrawResult,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Ensure schema exists before serving the first request.
    init_db()
    yield


app = FastAPI(
    title="展馆 API：观众参观路径 + 借展器物点交",
    description=(
        "从浏览器 open/close 事件还原观众实际参观路径（支持重传去重、乱序补报、"
        "缺关闭标记停留未知与同意撤回）；并提供借展器物到馆点交：先发运箱单、"
        "按箱提交整份实点清单（可更正/撤销），维护物化差异表区分相符、冲突、"
        "错箱、多出、待查与短缺。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(crud.BadEventTime)
def _bad_event_time_handler(_request, exc: crud.BadEventTime) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


@app.exception_handler(loan_crud.BatchNotFound)
def _batch_not_found_handler(_request, exc: loan_crud.BatchNotFound) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": str(exc)},
    )


@app.exception_handler(loan_crud.ManifestAlreadyExists)
def _manifest_exists_handler(
    _request, exc: loan_crud.ManifestAlreadyExists
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc)},
    )


@app.exception_handler(loan_crud.DuplicateArtifactInManifest)
def _duplicate_manifest_handler(
    _request, exc: loan_crud.DuplicateArtifactInManifest
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


@app.exception_handler(loan_crud.UnknownBox)
def _unknown_box_handler(_request, exc: loan_crud.UnknownBox) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


@app.post(
    "/events",
    response_model=IngestResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
    summary="上报单个浏览事件（打开/关闭）",
)
def post_event(
    event: EventIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> IngestResult:
    """同一 ``event_id`` 重传返回 duplicate，不重复计数。

    整个请求在一个 ``BEGIN IMMEDIATE`` 事务内：校验、撤回检查与插入要么
    全部生效，要么全部回滚，不会出现半份写入。
    """
    with write_transaction(conn):
        return crud.ingest_event(conn, event)


@app.post(
    "/events/batch",
    response_model=IngestBatchResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
    summary="批量上报浏览事件（同一事务，原子提交）",
)
def post_events(
    batch: EventBatchIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> IngestBatchResult:
    """批量事件作为一个事务提交：任一事件非法则整批回滚。

    已撤回观众的事件记为 ``rejected_withdrawn``，其余事件仍可正常入库，
    二者在同一已提交状态上串行判定。
    """
    with write_transaction(conn):
        results = [crud.ingest_event(conn, event) for event in batch.events]
    accepted = sum(r.status == "stored" for r in results)
    duplicates = sum(r.status == "duplicate" for r in results)
    rejected = sum(r.status == "rejected_withdrawn" for r in results)
    return IngestBatchResult(
        accepted=accepted,
        duplicates=duplicates,
        rejected_withdrawn=rejected,
        results=results,
    )


@app.get(
    "/visitors/{visitor_id}/path",
    response_model=VisitPath,
    summary="查询观众参观路径（按打开时间排序）",
)
def get_path(
    visitor_id: str,
    conn: sqlite3.Connection = Depends(get_connection),
) -> VisitPath:
    """按 ``page_view_id`` 配对开关；缺关闭的展品 ``dwell_known=false``。

    路径在查询时从已提交事件实时推导，因此较早事件的补报会自动改变
    路径顺序与停留时长。撤回后返回空路径（``consent_withdrawn=true``）。
    """
    return crud.get_visitor_path(conn, visitor_id)


@app.post(
    "/visitors/{visitor_id}/withdraw",
    response_model=WithdrawResult,
    summary="撤回数据使用同意（删除已存事件，之后拒绝该观众的任何事件）",
)
def post_withdraw(
    visitor_id: str,
    payload: WithdrawRequest | None = None,
    conn: sqlite3.Connection = Depends(get_connection),
) -> WithdrawResult:
    """撤回标记与事件删除在同一事务内完成，重复撤回幂等。

    若请求带 body，其中的 visitor_id 必须与路径参数一致。
    """
    if payload is not None and payload.visitor_id != visitor_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径中的 visitor_id 与请求体不一致",
        )
    with write_transaction(conn):
        return crud.withdraw_consent(conn, visitor_id)


# ===========================================================================
# 借展器物到馆点交
# ===========================================================================


@app.post(
    "/loan/batches/{batch_id}/manifest",
    response_model=ManifestResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="录入一批借展的发运箱单（每个器物号只归一个箱）",
)
def post_manifest(
    batch_id: str,
    payload: ManifestIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> ManifestResult:
    """先于开箱点交录入发运箱单。

    - 路径中的 ``batch_id`` 与请求体一致，否则 400；
    - 同一器物号出现在多个箱中返回 400（整批回滚，不留半成品）；
    - 同一批次重复录入返回 409；
    - 录入成功后，每个箱单器物先在差异表落 ``pending``（待查）。
    """
    if payload.batch_id != batch_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径中的 batch_id 与请求体不一致",
        )
    with write_transaction(conn):
        return ManifestResult(**loan_crud.record_manifest(conn, payload))


@app.put(
    "/loan/batches/{batch_id}/boxes/{box_id}/count",
    response_model=CountSubmitResult,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="按箱提交整份实点清单（首次点交/更正，空清单算已点）",
)
def put_box_count(
    batch_id: str,
    box_id: str,
    payload: CountIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> CountSubmitResult:
    """整份替换该箱现行清单：重复提交即更正，``artifact_ids=[]`` 表示空箱已点。

    实点记录与差异表在同一 ``BEGIN IMMEDIATE`` 事务内一并更新，失败整体
    回滚、保留原结果。只重核本箱新旧清单涉及的器物；若本次提交使“全部
    箱点完”状态翻转，尚未找到的箱单器物会在待查/短缺之间批量翻转。
    """
    with write_transaction(conn):
        return CountSubmitResult(
            **loan_crud.submit_box_count(conn, batch_id, box_id, payload)
        )


@app.delete(
    "/loan/batches/{batch_id}/boxes/{box_id}/count",
    response_model=CountRevokeResult,
    responses={404: {"model": ErrorResponse}},
    summary="撤销一箱点交（删除该箱实点清单，幂等）",
)
def delete_box_count(
    batch_id: str,
    box_id: str,
    conn: sqlite3.Connection = Depends(get_connection),
) -> CountRevokeResult:
    """撤销后该箱视为未点；差异表按撤销前清单涉及的器物增量重核。

    撤销未点过的箱不报错（``was_checked=false``）。若撤销使批次不再
    “全部点完”，已报短缺的器物回退为待查。
    """
    with write_transaction(conn):
        return CountRevokeResult(
            **loan_crud.revoke_box_count(conn, batch_id, box_id)
        )


@app.get(
    "/loan/batches/{batch_id}/discrepancies",
    response_model=DiscrepancyReport,
    responses={404: {"model": ErrorResponse}},
    summary="查询点交差异表（按器物号列出应在箱、实见箱与结论）",
)
def get_discrepancies(
    batch_id: str,
    conn: sqlite3.Connection = Depends(get_connection),
) -> DiscrepancyReport:
    """直接读取已物化的差异表，不在查询时重新推导。

    结论：``ok`` 相符 / ``conflict`` 冲突（多箱实见）/ ``misboxed`` 错箱
    （只在别箱找到）/ ``extra`` 多出（箱单未列且单箱实见）/ ``pending``
    待查（未找到且尚有箱未点）/ ``shortage`` 短缺（全部箱点完仍未找到）。
    """
    return DiscrepancyReport(**loan_crud.get_discrepancy_report(conn, batch_id))


@app.get("/health", summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}
