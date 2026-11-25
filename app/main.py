"""冷库温度明细/摘要查询 API。/docs 可直接操作全部接口。"""
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Path
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from . import logic, reconcile
from .db import connect, init_db
from .schemas import (
    ApprovalOut,
    HourOut,
    IntervalIn,
    QueryIn,
    QueryOut,
    ReconciliationIn,
    ReconciliationOut,
    SegmentOut,
    SummaryOut,
    TapeMarkOut,
    TemperaturesIn,
    check_whole_hour,
    to_iso,
    to_ts,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="冷库超温占比查询",
    description=(
        "按 UTC 整点录入温度明细，为齐备的整点区间生成摘要（总笔数 / 超 8℃ 笔数）。"
        "更正某小时明细后，按该小时旧明细生成的摘要自动失效。"
        "查询在给定明细行数配额内，组合有效摘要与小时明细恰好覆盖区间，"
        "使实际读取的明细行数最少；缺档或超预算时在读明细前返回原因。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_, exc: RequestValidationError):
    # 默认错误体会回显原始输入；输入含 NaN 时 JSON 序列化会失败变成 500，
    # 这里只保留 type/loc/msg，保证任何非法输入都稳定返回 422。
    errors = [
        {"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _hour_path_param(hour: datetime = Path(description="UTC 整点，如 2026-11-24T10:00:00Z")) -> int:
    try:
        return to_ts(check_whole_hour(hour))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _missing_hours_payload(e: logic.MissingHoursError) -> dict:
    shown = e.missing_ts[:24]
    return {
        "reason": "missing_data",
        "message": "区间内存在既无明细、也不被任何有效摘要覆盖的小时",
        "missing_count": len(e.missing_ts),
        "missing_hours": [to_iso(t) for t in shown],
        "truncated": len(e.missing_ts) > len(shown),
    }


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.post("/hours/{hour}/readings", status_code=201, tags=["明细"])
def record_readings(
    body: TemperaturesIn,
    hour_ts: int = Depends(_hour_path_param),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """录入某 UTC 整点的非空温度明细。该小时已有数据时返回 409，请改用 PUT 更正。"""
    try:
        version = logic.record_hour(conn, hour_ts, body.temperatures, overwrite=False)
    except logic.HourExistsError:
        raise HTTPException(
            status_code=409,
            detail={"reason": "hour_already_recorded", "message": "该小时已有明细，请用 PUT 更正"},
        )
    return {"hour": to_iso(hour_ts), "version": version, "row_count": len(body.temperatures)}


@app.put("/hours/{hour}/readings", tags=["明细"])
def correct_readings(
    body: TemperaturesIn,
    hour_ts: int = Depends(_hour_path_param),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """更正某整点的全部明细（整小时替换）。生效后按该小时旧明细生成的摘要失效。"""
    version = logic.record_hour(conn, hour_ts, body.temperatures, overwrite=True)
    return {"hour": to_iso(hour_ts), "version": version, "row_count": len(body.temperatures)}


@app.get("/hours", response_model=list[HourOut], tags=["明细"])
def hours(conn: sqlite3.Connection = Depends(get_conn)):
    """列出已录入的小时及其版本号、明细行数。"""
    return [
        HourOut(hour=to_iso(r["hour_ts"]), version=r["version"], row_count=r["row_count"])
        for r in logic.list_hours(conn)
    ]


@app.post("/summaries", status_code=201, tags=["摘要"])
def create_summary(body: IntervalIn, conn: sqlite3.Connection = Depends(get_conn)):
    """为齐备的整点区间 [start, end) 生成摘要；任一小时缺明细返回 409。"""
    start_ts, end_ts = to_ts(body.start), to_ts(body.end)
    try:
        result = logic.make_summary(conn, start_ts, end_ts)
    except logic.MissingHoursError as e:
        raise HTTPException(status_code=409, detail=_missing_hours_payload(e))
    return {
        "id": result["id"],
        "start": to_iso(start_ts),
        "end": to_iso(end_ts),
        "total_count": result["total_count"],
        "over_count": result["over_count"],
    }


@app.get("/summaries", response_model=list[SummaryOut], tags=["摘要"])
def summaries(conn: sqlite3.Connection = Depends(get_conn)):
    """列出全部摘要及其当前有效性（更正会使相关摘要失效）。"""
    return [
        SummaryOut(
            id=r["id"],
            start=to_iso(r["start_ts"]),
            end=to_iso(r["end_ts"]),
            total_count=r["total_count"],
            over_count=r["over_count"],
            created_at=r["created_at"],
            valid=bool(r["valid"]),
        )
        for r in logic.list_summaries(conn)
    ]


@app.post("/query", response_model=QueryOut, tags=["查询"])
def query(body: QueryIn, conn: sqlite3.Connection = Depends(get_conn)):
    """在明细行数配额内查询 [start, end) 的超温占比。

    从同一已提交状态的有效摘要与小时明细中，选出恰好覆盖全段、
    读取明细行数最少的组合。缺档或超预算时，在读取明细前返回原因。
    """
    start_ts, end_ts = to_ts(body.start), to_ts(body.end)
    try:
        result = logic.query_range(conn, start_ts, end_ts, body.max_detail_rows)
    except logic.MissingHoursError as e:
        raise HTTPException(status_code=409, detail=_missing_hours_payload(e))
    except logic.BudgetExceededError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "budget_exceeded",
                "message": "最优方案需要读取的明细行数超过上限",
                "required_detail_rows": e.required_rows,
                "max_detail_rows": body.max_detail_rows,
            },
        )
    segments = [
        SegmentOut(
            start=to_iso(s["start_ts"]),
            end=to_iso(s["end_ts"]),
            source=s["source"],
            summary_id=s["summary_id"],
            total_readings=s["total_readings"],
            over_temp_readings=s["over_temp_readings"],
            over_temp_ratio=s["over_temp_readings"] / s["total_readings"],
        )
        for s in result["segments"]
    ]
    return QueryOut(
        start=to_iso(start_ts),
        end=to_iso(end_ts),
        total_readings=result["total"],
        over_temp_readings=result["over"],
        over_temp_ratio=result["over"] / result["total"],
        detail_rows_read=result["detail_rows_read"],
        max_detail_rows=body.max_detail_rows,
        segments=segments,
    )


# ---------- 纸带核对 ----------

def _reading_outs(r: dict) -> list[dict]:
    """按录入序号组装每笔读数的可行分钟与唯一分钟索引（不可行试算单为空）。"""
    if not r["feasible"]:
        return []
    return [
        {
            "entry_order": i,
            "temperature": temp,
            "feasible_minutes": candidates,
            "minute_index": candidates[0] if len(candidates) == 1 else None,
        }
        for i, (temp, candidates) in enumerate(zip(r["readings"], r["feasible_minutes"]))
    ]


def _reconciliation_out(hour_ts: int, r: dict) -> ReconciliationOut:
    return ReconciliationOut(
        id=r["id"],
        hour=to_iso(hour_ts),
        version=r["version"],
        feasible=r["feasible"],
        reading_count=r["reading_count"],
        created_at=r["created_at"],
        marks=[TapeMarkOut(minute=m, temperature=t) for m, t in sorted(r["marks"].items())],
        readings=_reading_outs(r),
    )


@app.post(
    "/hours/{hour}/reconciliations",
    response_model=ReconciliationOut,
    status_code=201,
    tags=["纸带核对"],
)
def create_reconciliation(
    body: ReconciliationIn,
    hour_ts: int = Depends(_hour_path_param),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """按明细版号提交纸带上标出的分钟与温度，试算每笔读数的所有可行分钟。

    版号与明细取自同一已提交快照；版号不符返回 409，整组退回、不动上次核定。
    无可行排法同样 409 整组退回（保留留痕试算单，feasible=false）。
    计算期间该小时整小时更正仍可提交，登记时会再次核对版号。
    """
    marks = {m.minute: m.temperature for m in body.marks}
    try:
        result = reconcile.create_reconciliation(conn, hour_ts, body.version, marks)
    except reconcile.HourNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"reason": "hour_not_found", "message": "该小时尚无已提交明细"},
        )
    except reconcile.VersionMismatchError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "version_mismatch",
                "message": "提交版号与当前已提交明细版号不一致，整组退回并保留上次核定",
                "submitted_version": e.submitted,
                "current_version": e.current,
            },
        )
    if not result["feasible"]:
        # 无可行排法：整组退回。响应仍为 409，留痕试算单可在列表中查看。
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "infeasible_arrangement",
                "message": "不存在满足全部纸带标记的排法，整组退回并保留上次核定",
                "reconciliation_id": result["id"],
            },
        )
    return _reconciliation_out(hour_ts, result)


@app.get(
    "/hours/{hour}/reconciliations",
    response_model=list[ReconciliationOut],
    tags=["纸带核对"],
)
def list_reconciliations(
    hour_ts: int = Depends(_hour_path_param),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """列出该小时全部试算单（含 feasible=false 的退回留痕），按试算序号排序。"""
    return [_reconciliation_out(hour_ts, r) for r in reconcile.list_reconciliations(conn, hour_ts)]


@app.post(
    "/hours/{hour}/reconciliations/{recon_id}/approval",
    response_model=ApprovalOut,
    tags=["纸带核对"],
)
def register_approval(
    body: ReconciliationIn,
    recon_id: int,
    hour_ts: int = Depends(_hour_path_param),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """把已试算的整组结果登记为最近一次核定；登记前再次核对版号。

    请求体与试算相同（版号 + 纸带标记），版号不符或试算单无可行排法时整组退回，
    上次核定保持不变。
    """
    try:
        reconcile.register_approval(conn, hour_ts, recon_id, body.version)
    except reconcile.ReconciliationNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"reason": "reconciliation_not_found", "message": "试算单不存在或不属于该小时"},
        )
    except reconcile.VersionMismatchError as e:
        if e.submitted == e.current:
            message = "提交版号与试算单版号不一致，整组退回并保留上次核定"
        else:
            message = "登记时明细已换版，整组退回并保留上次核定"
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "version_mismatch",
                "message": message,
                "submitted_version": e.submitted,
                "current_version": e.current,
            },
        )
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "infeasible_arrangement",
                "message": "该试算单无可行排法，不能登记，上次核定保留",
            },
        )
    latest = reconcile.get_latest_approval(conn, hour_ts)
    return _approval_out(hour_ts, latest)


@app.get("/hours/{hour}/approval", response_model=ApprovalOut, tags=["纸带核对"])
def latest_approval(
    hour_ts: int = Depends(_hour_path_param),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """查询最近一次核定；核定后明细已换版则 stale=true。从未核定返回 404。"""
    latest = reconcile.get_latest_approval(conn, hour_ts)
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "approval_not_found", "message": "该小时尚无核定记录"},
        )
    return _approval_out(hour_ts, latest)


def _approval_out(hour_ts: int, r: dict) -> ApprovalOut:
    return ApprovalOut(
        hour=to_iso(hour_ts),
        recon_id=r["id"],
        approved_version=r["approved_version"],
        current_version=r["current_version"],
        stale=r["stale"],
        approved_at=r["approved_at"],
        feasible=r["feasible"],
        reading_count=r["reading_count"],
        marks=[TapeMarkOut(minute=m, temperature=t) for m, t in sorted(r["marks"].items())],
        readings=_reading_outs(r),
    )
