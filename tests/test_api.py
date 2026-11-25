"""端到端测试：重叠摘要、最优组合、预算/缺档前置报错、更正失效与更正后查询。

数据设计（超温阈值 >8℃，8℃ 本身不算超温）：
  h0: [4,5,6,7,9]   5 行, 超温 1
  h1: [3,4,5,8,10]  5 行, 超温 1  →  更正为 [2,3]  2 行, 超温 0
  h2: [1,2]         2 行, 超温 0
  h3: [9,10]        2 行, 超温 2
  h4: [2]           1 行, 超温 0
  h5: [12]          1 行, 超温 1
摘要：S1=[h0,h4) 共 14 行/超 4；S2=[h2,h6) 共 6 行/超 3（与 S1 重叠 h2、h3）。
"""
import os

import pytest
from fastapi.testclient import TestClient

BASE = "2026-11-20T00:00:00Z"
HOURS = {
    "2026-11-20T00:00:00Z": [4, 5, 6, 7, 9],
    "2026-11-20T01:00:00Z": [3, 4, 5, 8, 10],
    "2026-11-20T02:00:00Z": [1, 2],
    "2026-11-20T03:00:00Z": [9, 10],
    "2026-11-20T04:00:00Z": [2],
    "2026-11-20T05:00:00Z": [12],
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COLDCHAIN_DB", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def seeded(client):
    for hour, temps in HOURS.items():
        r = client.post(f"/hours/{hour}/readings", json={"temperatures": temps})
        assert r.status_code == 201, r.text
    return client


def _make_summaries(client):
    r = client.post("/summaries", json={"start": BASE, "end": "2026-11-20T04:00:00Z"})
    assert r.status_code == 201, r.text
    s1 = r.json()
    r = client.post("/summaries", json={"start": "2026-11-20T02:00:00Z", "end": "2026-11-20T06:00:00Z"})
    assert r.status_code == 201, r.text
    s2 = r.json()
    return s1, s2


# ---------- 录入与校验 ----------

def test_record_and_validation(client):
    r = client.post(f"/hours/{BASE}/readings", json={"temperatures": [5.5, 9.1]})
    assert r.status_code == 201
    assert r.json() == {"hour": BASE, "version": 1, "row_count": 2}

    # 重复录入 → 409，应改用更正
    r = client.post(f"/hours/{BASE}/readings", json={"temperatures": [1.0]})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "hour_already_recorded"

    # 空明细 → 422（非空温度明细）
    r = client.post("/hours/2026-11-20T02:00:00Z/readings", json={"temperatures": []})
    assert r.status_code == 422
    # 非整点 → 422
    r = client.post("/hours/2026-11-20T02:30:00Z/readings", json={"temperatures": [1.0]})
    assert r.status_code == 422
    # 缺时区 → 422
    r = client.post("/hours/2026-11-20T02:00:00/readings", json={"temperatures": [1.0]})
    assert r.status_code == 422
    # 非法温度（NaN）→ 422（发原始 JSON 体，绕过客户端序列化限制）
    r = client.post(
        "/hours/2026-11-20T03:00:00Z/readings",
        content=b'{"temperatures": [NaN]}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


def test_summary_requires_complete_hours(seeded):
    # [h0, h9) 中 h6/h7/h8 无明细 → 409 缺档
    r = seeded.post("/summaries", json={"start": BASE, "end": "2026-11-20T09:00:00Z"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "missing_data"
    assert detail["missing_hours"] == [
        "2026-11-20T06:00:00Z",
        "2026-11-20T07:00:00Z",
        "2026-11-20T08:00:00Z",
    ]


# ---------- 重叠摘要 + 最优组合 ----------

def test_overlapping_summaries_optimal_plan(seeded):
    s1, s2 = _make_summaries(seeded)
    assert (s1["total_count"], s1["over_count"]) == (14, 4)
    assert (s2["total_count"], s2["over_count"]) == (6, 3)

    # 查 [h0,h6)：S1+明细(h4,h5)=2 行 优于 S2+明细(h0,h1)=10 行 优于 全明细=16 行
    r = seeded.post("/query", json={"start": BASE, "end": "2026-11-20T06:00:00Z", "max_detail_rows": 100})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detail_rows_read"] == 2
    assert body["total_readings"] == 16
    assert body["over_temp_readings"] == 5
    assert body["over_temp_ratio"] == pytest.approx(5 / 16)
    assert [(s["source"], s["start"], s["end"]) for s in body["segments"]] == [
        ("summary", BASE, "2026-11-20T04:00:00Z"),
        ("details", "2026-11-20T04:00:00Z", "2026-11-20T06:00:00Z"),
    ]
    assert body["segments"][0]["summary_id"] == s1["id"]
    assert body["segments"][1]["total_readings"] == 2
    assert body["segments"][1]["over_temp_readings"] == 1


def test_budget_exceeded_before_reading(seeded):
    _make_summaries(seeded)
    r = seeded.post("/query", json={"start": BASE, "end": "2026-11-20T06:00:00Z", "max_detail_rows": 1})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "budget_exceeded"
    assert detail["required_detail_rows"] == 2
    assert detail["max_detail_rows"] == 1


def test_missing_data_before_reading(seeded):
    _make_summaries(seeded)
    r = seeded.post("/query", json={"start": BASE, "end": "2026-11-20T08:00:00Z", "max_detail_rows": 100})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "missing_data"
    assert detail["missing_hours"] == ["2026-11-20T06:00:00Z", "2026-11-20T07:00:00Z"]


def test_query_validation(seeded):
    # 起点不早于终点 → 422；非整点 → 422；负配额 → 422
    r = seeded.post("/query", json={"start": BASE, "end": BASE, "max_detail_rows": 0})
    assert r.status_code == 422
    r = seeded.post("/query", json={"start": BASE, "end": "2026-11-20T06:30:00Z", "max_detail_rows": 0})
    assert r.status_code == 422
    r = seeded.post("/query", json={"start": BASE, "end": "2026-11-20T06:00:00Z", "max_detail_rows": -1})
    assert r.status_code == 422


# ---------- 更正：失效与更正后查询 ----------

def test_correction_invalidates_only_dependent_summaries(seeded):
    s1, s2 = _make_summaries(seeded)

    # 摘要生成期间更正 h1（只在 S1 覆盖内，S2 不含 h1）
    r = seeded.put("/hours/2026-11-20T01:00:00Z/readings", json={"temperatures": [2, 3]})
    assert r.status_code == 200
    assert r.json()["version"] == 2

    summaries = {s["id"]: s for s in seeded.get("/summaries").json()}
    assert summaries[s1["id"]]["valid"] is False  # 按 h1 旧明细生成 → 失效
    assert summaries[s2["id"]]["valid"] is True   # 其他摘要照用

    # 更正后查询 [h0,h6)：S1 不可用，最优变为 明细(h0,h1) + S2 = 5+2=7 行
    r = seeded.post("/query", json={"start": BASE, "end": "2026-11-20T06:00:00Z", "max_detail_rows": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detail_rows_read"] == 7
    assert [(s["source"], s["start"], s["end"]) for s in body["segments"]] == [
        ("details", BASE, "2026-11-20T02:00:00Z"),
        ("summary", "2026-11-20T02:00:00Z", "2026-11-20T06:00:00Z"),
    ]
    assert body["segments"][1]["summary_id"] == s2["id"]
    # 更正后的 h1（超温 0）已生效：总 13 行、超温 4
    assert body["total_readings"] == 13
    assert body["over_temp_readings"] == 4
    assert body["over_temp_ratio"] == pytest.approx(4 / 13)

    # [h0,h4) 配额 0：S1 已失效，只能读明细 5+2+2+2=11 行 → 超预算
    r = seeded.post("/query", json={"start": BASE, "end": "2026-11-20T04:00:00Z", "max_detail_rows": 0})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "budget_exceeded"
    assert r.json()["detail"]["required_detail_rows"] == 11

    # 用更正后的明细重新生成 S1'，配额 0 即可查，且反映新数据
    r = seeded.post("/summaries", json={"start": BASE, "end": "2026-11-20T04:00:00Z"})
    assert r.status_code == 201
    s1b = r.json()
    assert (s1b["total_count"], s1b["over_count"]) == (11, 3)

    r = seeded.post("/query", json={"start": BASE, "end": "2026-11-20T04:00:00Z", "max_detail_rows": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["detail_rows_read"] == 0
    assert body["segments"][0]["summary_id"] == s1b["id"]
    assert body["total_readings"] == 11
    assert body["over_temp_readings"] == 3
    assert body["over_temp_ratio"] == pytest.approx(3 / 11)


def test_correction_of_shared_hour_falls_back_to_details(seeded):
    _make_summaries(seeded)
    # 更正 h3（S1、S2 都覆盖它）→ 两个摘要同时失效，查询退回全明细
    r = seeded.put("/hours/2026-11-20T03:00:00Z/readings", json={"temperatures": [1, 1, 1]})
    assert r.status_code == 200
    assert all(s["valid"] is False for s in seeded.get("/summaries").json())

    r = seeded.post("/query", json={"start": BASE, "end": "2026-11-20T06:00:00Z", "max_detail_rows": 100})
    assert r.status_code == 200
    body = r.json()
    assert [s["source"] for s in body["segments"]] == ["details"]
    # h3 现为 3 行：5+5+2+3+1+1 = 17 行，超温 1+1+0+0+0+1 = 3
    assert body["detail_rows_read"] == 17
    assert body["total_readings"] == 17
    assert body["over_temp_readings"] == 3


def test_hours_listing_and_correction_upsert(seeded):
    hours = {h["hour"]: h for h in seeded.get("/hours").json()}
    assert len(hours) == 6
    assert hours[BASE]["version"] == 1
    assert hours[BASE]["row_count"] == 5
    # PUT 对不存在的小时也可建档（版本 1）
    r = seeded.put("/hours/2026-11-20T09:00:00Z/readings", json={"temperatures": [7.5]})
    assert r.status_code == 200
    assert r.json()["version"] == 1
