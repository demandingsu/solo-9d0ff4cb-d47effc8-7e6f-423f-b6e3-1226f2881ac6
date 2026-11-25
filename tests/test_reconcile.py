"""纸带核对测试：可行分钟 DP、同温多笔、版号核对、无可行排法、核定过期、
以及试算期间发生整小时更正的并发场景。"""
import threading

import pytest
from fastapi.testclient import TestClient

from app import reconcile

H = "2026-11-21T08:00:00Z"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COLDCHAIN_DB", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def hour(client):
    def _record(temps):
        r = client.post(f"/hours/{H}/readings", json={"temperatures": temps})
        assert r.status_code == 201, r.text
        return r.json()["version"]

    return _record


# ---------- 纯 DP：可行分钟 ----------

def test_dp_no_marks_three_readings():
    # 无标记：3 笔读数可放 0..59 任意保持顺序的位置
    out = reconcile.compute_feasible_minutes([4.0, 5.0, 6.0], {})
    assert out is not None
    assert out[0] == list(range(0, 58))   # 后两笔至少各占一分钟 → 最大 57
    assert out[1] == list(range(1, 59))
    assert out[2] == list(range(2, 60))
    # 无唯一可行分钟 → 一律不补 minute_index（由 API 层判断）
    assert all(len(c) != 1 for c in out)


def test_dp_sixty_readings_fill_every_minute():
    out = reconcile.compute_feasible_minutes([float(i) for i in range(60)], {})
    assert out is not None
    assert [c for c in out] == [[i] for i in range(60)]  # 每分钟恰好一笔，全部唯一


def test_dp_same_temp_multiple_readings_with_mark():
    # 同温多笔：[5, 5, 7]，纸带标出分钟 30 为 5℃
    out = reconcile.compute_feasible_minutes([5.0, 5.0, 7.0], {30: 5.0})
    assert out is not None
    # 两笔 5℃ 都可占据分钟 30（顺序相容），因此都不唯一
    assert 30 in out[0] and 30 in out[1]
    assert len(out[0]) > 1 and len(out[1]) > 1
    assert 30 not in out[2]  # 7℃ 不能放同温标记位
    # 三笔的可行位置都保持顺序：各候选列表升序且非空
    for c in out:
        assert c == sorted(c) and c


def test_dp_mark_forces_unique_assignment():
    # [5, 6, 7]，标记分钟 0=5℃：首笔只能在 0
    out = reconcile.compute_feasible_minutes([5.0, 6.0, 7.0], {0: 5.0})
    assert out is not None
    assert out[0] == [0]
    assert out[1][0] == 1 and out[2][0] == 2
    # 末端锚点：标记分钟 59=7℃ → 末笔唯一
    out2 = reconcile.compute_feasible_minutes([5.0, 6.0, 7.0], {59: 7.0})
    assert out2[2] == [59]
    assert len(out2[0]) > 1 and len(out2[1]) > 1


def test_dp_unmarked_reading_gets_unique_minute():
    # 纸带没直接标出的读数也按同一规则：[5, 9, 9]，标记分钟 1=9℃、59=9℃
    # 两笔 9℃ 被两端锚点夹住 → 分钟 1 和 59，首笔 5℃ 只能在分钟 0
    out = reconcile.compute_feasible_minutes([5.0, 9.0, 9.0], {1: 9.0, 59: 9.0})
    assert out is not None
    assert out[0] == [0]       # 未被纸带直接标出，仍唯一补入
    assert out[1] == [1]
    assert out[2] == [59]


def test_dp_infeasible_wrong_temp_on_mark():
    # 标记分钟 30=5℃，但没有任何 5℃ 读数 → 无可行排法
    out = reconcile.compute_feasible_minutes([6.0, 7.0, 8.0], {30: 5.0})
    assert out is None


def test_dp_infeasible_too_many_readings():
    assert reconcile.compute_feasible_minutes([1.0] * 61, {}) is None


def test_dp_two_marks_same_temp_force_order():
    # 同温两笔 + 两个同温标记：[5, 5]，分钟 10、20 都是 5℃
    # 读数与标记数相等：两笔必须恰好占据两个标记位，按顺序各锁一个
    out = reconcile.compute_feasible_minutes([5.0, 5.0], {10: 5.0, 20: 5.0})
    assert out is not None
    assert out[0] == [10]
    assert out[1] == [20]
    # 多一笔同温读数后标记位不再唯一：[5, 5, 5] 中各笔都有多个候选
    out3 = reconcile.compute_feasible_minutes([5.0, 5.0, 5.0], {10: 5.0, 20: 5.0})
    assert out3 is not None
    assert all(len(c) > 1 for c in out3)
    # 顺序约束：首笔可占标记 10 但到不了 20（后面两笔还要覆盖 20），末笔反之
    assert 10 in out3[0] and 20 not in out3[0]
    assert 20 in out3[2] and 10 not in out3[2]
    assert {10, 20} <= set(out3[1])
    # 两个读数、三个 5℃ 锚点 → 锚点无法全覆盖，整组无可行排法
    assert reconcile.compute_feasible_minutes([5.0, 5.0], {10: 5.0, 20: 5.0, 59: 5.0}) is None


# ---------- API：试算 ----------

def test_api_reconciliation_happy_path(hour, client):
    hour([5.0, 6.0, 7.0])
    r = client.post(
        f"/hours/{H}/reconciliations",
        json={"version": 1, "marks": [{"minute": 0, "temperature": 5.0}]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1 and body["feasible"] is True
    assert body["reading_count"] == 3
    assert body["marks"] == [{"minute": 0, "temperature": 5.0}]
    readings = body["readings"]
    assert [x["entry_order"] for x in readings] == [0, 1, 2]
    assert readings[0]["feasible_minutes"] == [0]
    assert readings[0]["minute_index"] == 0          # 唯一可行 → 补入分钟索引
    assert readings[1]["minute_index"] is None       # 多解 → 不补
    assert readings[2]["minute_index"] is None

    # GET 列表返回同构的逐笔结果（回归：hydrate 与试算返回结构一致）
    listing = client.get(f"/hours/{H}/reconciliations").json()
    assert len(listing) == 1 and listing[0]["id"] == body["id"]
    assert listing[0]["readings"][0] == readings[0]
    assert listing[0]["marks"] == body["marks"]


def test_api_same_temp_multiple_readings(hour, client):
    hour([5.0, 5.0, 7.0])
    r = client.post(
        f"/hours/{H}/reconciliations",
        json={"version": 1, "marks": [{"minute": 30, "temperature": 5.0}]},
    )
    assert r.status_code == 201, r.text
    readings = r.json()["readings"]
    # 同温两笔都能落在标记分钟，谁都不唯一 → 不补分钟索引
    assert readings[0]["minute_index"] is None and readings[1]["minute_index"] is None
    assert 30 in readings[0]["feasible_minutes"]
    assert 30 in readings[1]["feasible_minutes"]
    assert readings[2]["minute_index"] is None


def test_api_infeasible_group_rejected_and_trail_left(hour, client):
    hour([6.0, 7.0, 8.0])
    r = client.post(
        f"/hours/{H}/reconciliations",
        json={"version": 1, "marks": [{"minute": 30, "temperature": 5.0}]},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "infeasible_arrangement"
    recon_id = detail["reconciliation_id"]
    # 留痕试算单可列出，feasible=false，且无逐笔结果
    listing = client.get(f"/hours/{H}/reconciliations").json()
    trail = next(x for x in listing if x["id"] == recon_id)
    assert trail["feasible"] is False and trail["readings"] == []


def test_api_version_mismatch_rejects_group(hour, client):
    hour([1.0, 2.0])  # version 1
    client.put(f"/hours/{H}/readings", json={"temperatures": [3.0, 4.0]})  # version 2
    r = client.post(
        f"/hours/{H}/reconciliations",
        json={"version": 1, "marks": [{"minute": 0, "temperature": 3.0}]},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "version_mismatch"
    assert detail["submitted_version"] == 1 and detail["current_version"] == 2
    # 版号不符不落试算单
    assert client.get(f"/hours/{H}/reconciliations").json() == []


def test_api_hour_not_found_and_validation(client):
    r = client.post(
        f"/hours/{H}/reconciliations",
        json={"version": 1, "marks": [{"minute": 0, "temperature": 1.0}]},
    )
    assert r.status_code == 404 and r.json()["detail"]["reason"] == "hour_not_found"

    def _seed(temps):
        rr = client.post(f"/hours/{H}/readings", json={"temperatures": temps})
        assert rr.status_code == 201

    _seed([1.0])
    # 分钟越界、空标记、重复分钟、版号非法 → 422
    assert client.post(
        f"/hours/{H}/reconciliations",
        json={"version": 1, "marks": [{"minute": 60, "temperature": 1.0}]},
    ).status_code == 422
    assert client.post(
        f"/hours/{H}/reconciliations", json={"version": 1, "marks": []}
    ).status_code == 422
    assert client.post(
        f"/hours/{H}/reconciliations",
        json={
            "version": 1,
            "marks": [
                {"minute": 10, "temperature": 1.0},
                {"minute": 10, "temperature": 2.0},
            ],
        },
    ).status_code == 422
    assert client.post(
        f"/hours/{H}/reconciliations",
        json={"version": 0, "marks": [{"minute": 10, "temperature": 1.0}]},
    ).status_code == 422


# ---------- API：登记核定与过期标记 ----------

def test_api_approval_flow_and_stale_after_correction(hour, client):
    hour([5.0, 6.0, 7.0])
    r = client.post(
        f"/hours/{H}/reconciliations",
        json={"version": 1, "marks": [{"minute": 59, "temperature": 7.0}]},
    )
    recon_id = r.json()["id"]

    # 从未核定 → 404
    assert client.get(f"/hours/{H}/approval").status_code == 404

    r = client.post(
        f"/hours/{H}/reconciliations/{recon_id}/approval",
        json={"version": 1, "marks": [{"minute": 59, "temperature": 7.0}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recon_id"] == recon_id
    assert body["approved_version"] == 1 and body["stale"] is False
    assert body["current_version"] == 1
    assert body["readings"][2]["minute_index"] == 59

    # 明细换版（整小时更正）→ 最近核定标过期，但内容仍可查
    client.put(f"/hours/{H}/readings", json={"temperatures": [5.0, 6.0, 7.0, 8.0]})
    r = client.get(f"/hours/{H}/approval")
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True
    assert body["approved_version"] == 1 and body["current_version"] == 2


def test_api_failed_registration_keeps_previous_approval(hour, client):
    hour([5.0, 6.0])
    ok = client.post(
        f"/hours/{H}/reconciliations",
        json={"version": 1, "marks": [{"minute": 0, "temperature": 5.0}]},
    ).json()
    assert client.post(
        f"/hours/{H}/reconciliations/{ok['id']}/approval",
        json={"version": 1, "marks": [{"minute": 0, "temperature": 5.0}]},
    ).status_code == 200

    # 登记提交的版号本身就与试算单版号不符（明细尚未换版）→ 同样整组退回
    r = client.post(
        f"/hours/{H}/reconciliations/{ok['id']}/approval",
        json={"version": 99, "marks": [{"minute": 0, "temperature": 5.0}]},
    )
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "version_mismatch"
    assert client.get(f"/hours/{H}/approval").json()["recon_id"] == ok["id"]

    # 更正后用旧试算单登记 → 版号不符，整组退回，上次核定保留
    client.put(f"/hours/{H}/readings", json={"temperatures": [9.0, 9.0]})
    r = client.post(
        f"/hours/{H}/reconciliations/{ok['id']}/approval",
        json={"version": 1, "marks": [{"minute": 0, "temperature": 5.0}]},
    )
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "version_mismatch"
    current = client.get(f"/hours/{H}/approval").json()
    assert current["recon_id"] == ok["id"]  # 仍是上次核定
    assert current["stale"] is True

    # 无可行排法的试算单不能登记
    bad = client.post(
        f"/hours/{H}/reconciliations",
        json={"version": 2, "marks": [{"minute": 0, "temperature": 1.0}]},
    )
    assert bad.status_code == 409
    bad_id = bad.json()["detail"]["reconciliation_id"]
    r = client.post(
        f"/hours/{H}/reconciliations/{bad_id}/approval",
        json={"version": 2, "marks": [{"minute": 0, "temperature": 1.0}]},
    )
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "infeasible_arrangement"
    assert client.get(f"/hours/{H}/approval").json()["recon_id"] == ok["id"]


def test_api_approval_unknown_reconciliation_404(hour, client):
    hour([1.0])
    r = client.post(
        f"/hours/{H}/reconciliations/999/approval",
        json={"version": 1, "marks": [{"minute": 0, "temperature": 1.0}]},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["reason"] == "reconciliation_not_found"


# ---------- 试算进行中发生整小时更正 ----------

def test_correction_during_calculation_then_registration_rejected(hour, client, monkeypatch):
    hour([5.0, 6.0, 7.0])  # version 1
    started = threading.Event()
    release = threading.Event()
    real = reconcile.create_reconciliation

    def patched(conn, hour_ts, version, marks, **kw):
        # 读事务快照建立后挂起，模拟长计算，等待另一连接提交更正
        return real(conn, hour_ts, version, marks, delay_hook=lambda: (started.set(), release.wait(5)))

    monkeypatch.setattr(reconcile, "create_reconciliation", patched)

    holder = {}

    def do_calc():
        holder["response"] = client.post(
            f"/hours/{H}/reconciliations",
            json={"version": 1, "marks": [{"minute": 0, "temperature": 5.0}]},
        )

    t = threading.Thread(target=do_calc)
    t.start()
    assert started.wait(timeout=5)
    # 计算期间整小时更正仍可提交（WAL：读不阻塞写），版号升到 2
    r = client.put(f"/hours/{H}/readings", json={"temperatures": [5.0, 6.0, 7.0, 8.0]})
    assert r.status_code == 200 and r.json()["version"] == 2
    release.set()
    t.join(timeout=10)

    # 试算基于 version 1 的同一已提交快照完成，结果本身有效并落库
    calc = holder["response"]
    assert calc.status_code == 201, calc.text
    body = calc.json()
    recon_id = body["id"]
    assert body["version"] == 1 and body["readings"][0]["minute_index"] == 0

    # 登记时重新核对版号：明细已换版 → 409 整组退回，且无任何核定被写入
    r = client.post(
        f"/hours/{H}/reconciliations/{recon_id}/approval",
        json={"version": 1, "marks": [{"minute": 0, "temperature": 5.0}]},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "version_mismatch"
    assert detail["submitted_version"] == 1 and detail["current_version"] == 2
    assert client.get(f"/hours/{H}/approval").status_code == 404

    # 按新版重新试算并登记成功
    r = client.post(
        f"/hours/{H}/reconciliations",
        json={"version": 2, "marks": [{"minute": 0, "temperature": 5.0}]},
    )
    assert r.status_code == 201 and r.json()["version"] == 2
    new_id = r.json()["id"]
    r = client.post(
        f"/hours/{H}/reconciliations/{new_id}/approval",
        json={"version": 2, "marks": [{"minute": 0, "temperature": 5.0}]},
    )
    assert r.status_code == 200
    assert r.json()["stale"] is False and r.json()["current_version"] == 2
