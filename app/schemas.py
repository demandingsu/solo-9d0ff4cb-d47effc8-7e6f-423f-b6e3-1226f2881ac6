"""请求/响应模型。时间一律为带时区的 UTC 整点（分秒必须为 0）。"""
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

HOUR_SECONDS = 3600


def check_whole_hour(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("时间必须携带时区，例如 2026-11-24T10:00:00Z")
    v = v.astimezone(timezone.utc)
    if v.minute or v.second or v.microsecond:
        raise ValueError("时间必须是整点（分、秒必须为 0）")
    return v


def to_ts(v: datetime) -> int:
    return int(v.timestamp())


def to_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TemperaturesIn(BaseModel):
    """某整点的温度明细，必须非空且为有限数值。"""

    temperatures: list[Annotated[float, Field(allow_inf_nan=False)]] = Field(min_length=1)


class IntervalIn(BaseModel):
    start: datetime = Field(examples=["2026-11-24T00:00:00Z"])
    end: datetime = Field(examples=["2026-11-24T06:00:00Z"])

    @field_validator("start", "end")
    @classmethod
    def _aligned(cls, v: datetime) -> datetime:
        return check_whole_hour(v)

    @model_validator(mode="after")
    def _ordered(self):
        if self.start >= self.end:
            raise ValueError("start 必须早于 end")
        return self


class QueryIn(IntervalIn):
    max_detail_rows: int = Field(ge=0, description="本次查询允许读取的明细行数上限")


class SegmentOut(BaseModel):
    start: str
    end: str
    source: Literal["summary", "details"]
    summary_id: Optional[int] = None
    total_readings: int
    over_temp_readings: int
    over_temp_ratio: float


class QueryOut(BaseModel):
    start: str
    end: str
    total_readings: int
    over_temp_readings: int
    over_temp_ratio: float
    detail_rows_read: int
    max_detail_rows: int
    segments: list[SegmentOut]


class HourOut(BaseModel):
    hour: str
    version: int
    row_count: int


class SummaryOut(BaseModel):
    id: int
    start: str
    end: str
    total_count: int
    over_count: int
    created_at: str
    valid: bool


# ---------- 纸带核对 ----------

class TapeMarkIn(BaseModel):
    """纸带上标出的一笔：分钟（0～59）与同温读数。"""

    minute: int = Field(ge=0, le=59)
    temperature: Annotated[float, Field(allow_inf_nan=False)]


class ReconciliationIn(BaseModel):
    """按小时和明细版号提交的整组纸带标记。"""

    version: int = Field(ge=1, description="提交时该小时明细的版号，计算与登记都须核对")
    marks: list[TapeMarkIn] = Field(
        min_length=1, description="纸带上标出的分钟与温度；同一分钟最多一笔"
    )

    @model_validator(mode="after")
    def _unique_minutes(self):
        minutes = [m.minute for m in self.marks]
        if len(set(minutes)) != len(minutes):
            raise ValueError("同一分钟最多一笔纸带标记")
        return self


class FeasibleReadingOut(BaseModel):
    entry_order: int
    temperature: float
    feasible_minutes: list[int]
    minute_index: Optional[int] = None


class TapeMarkOut(BaseModel):
    minute: int
    temperature: float


class ReconciliationOut(BaseModel):
    id: int
    hour: str
    version: int
    feasible: bool
    reading_count: int
    created_at: str
    marks: list[TapeMarkOut]
    readings: list[FeasibleReadingOut]


class ApprovalOut(BaseModel):
    hour: str
    recon_id: int
    approved_version: int
    current_version: Optional[int] = None
    stale: bool
    approved_at: str
    feasible: bool
    reading_count: int
    marks: list[TapeMarkOut]
    readings: list[FeasibleReadingOut]
