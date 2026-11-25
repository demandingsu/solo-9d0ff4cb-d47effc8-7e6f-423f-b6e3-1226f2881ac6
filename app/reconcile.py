"""纸带核对核心：在整点 60 分钟内求每笔读数（按录入顺序）的可行分钟。

问题约束（质检员据纸带补回可证实的采样时刻）：
- 整小时读数按原录入顺序排入 0..59 各分钟，每分钟最多一笔；
- 纸带上标出的每个分钟都必须被一笔同温读数占据（强制锚点）；
- 未标出的分钟可空、可放任意与顺序相容的读数。

可行分钟用前后两次动态规划求交集：
- forward[i][m]：读数 0..i-1 能否放进分钟 0..m-1（分钟 m 之前）；
- backward[i][m]：读数 i..n-1 能否放进分钟 m..59。
读数 i 能放分钟 m 当且仅当 forward[i][m] 与 backward[i+1][m+1] 同时成立，
且分钟 m 若被纸带标出，温度必须相同。
整张纸带无可行排法（forward[n][60] 不成立）时整组退回，不产生任何登记。

事务约定与 logic 层一致：写操作用 BEGIN IMMEDIATE 单事务提交；
试算在一条只读事务（BEGIN）内完成，看到的是同一已提交快照，
期间整点明细仍可被 PUT 更正（SQLite WAL 下读不阻塞写）。
"""
from __future__ import annotations

import json
import sqlite3

MINUTES_PER_HOUR = 60


class HourNotFoundError(Exception):
    """该小时尚无已提交明细。"""


class VersionMismatchError(Exception):
    """提交版号与当前已提交明细版号不一致。"""

    def __init__(self, submitted: int, current: int):
        super().__init__(f"submitted version {submitted}, current {current}")
        self.submitted = submitted
        self.current = current


class ReconciliationNotFoundError(Exception):
    """试算单不存在，或不属于该小时。"""


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


# ---------- 纯计算：可行分钟 DP ----------

def compute_feasible_minutes(
    readings: list[float], marks: dict[int, float], minutes: int = MINUTES_PER_HOUR
) -> list[list[int]] | None:
    """返回与 readings 等长的可行分钟列表；整体无可行排法时返回 None。

    readings：按录入顺序的温度；marks：{分钟: 纸带标出的温度}。
    minutes：分钟槽总数，生产固定 60，参数化仅为小空间穷举验证。
    """
    n = len(readings)
    if n > minutes:
        return None

    # 基例约束：没有读数可放时，强制标记不能落在空区间里
    first_mark = min(marks) if marks else minutes
    last_mark = max(marks) if marks else -1

    # forward[i][m]：读数 0..i-1 能否排入分钟 [0, m)
    # 基例：空读数前缀只在区间不含任何标记时可行，即 m <= 第一个标记
    forward = [[False] * (minutes + 1) for _ in range(n + 1)]
    for m in range(minutes + 1):
        forward[0][m] = m <= first_mark
    for i in range(1, n + 1):
        temp = readings[i - 1]
        for m in range(1, minutes + 1):
            slot = m - 1
            leave_empty = slot not in marks and forward[i][m - 1]
            place_here = (
                slot not in marks or marks[slot] == temp
            ) and forward[i - 1][m - 1]
            forward[i][m] = leave_empty or place_here

    if not forward[n][minutes]:
        return None  # 不存在覆盖全部强制标记的排法

    # backward[i][m]：读数 i..n-1 能否排入分钟 [m, minutes)
    # 基例：空读数后缀只在区间不含任何标记时可行，即 m > 最后一个标记
    backward = [[False] * (minutes + 1) for _ in range(n + 1)]
    for m in range(minutes + 1):
        backward[n][m] = m > last_mark
    for i in range(n - 1, -1, -1):
        temp = readings[i]
        for m in range(minutes - 1, -1, -1):
            leave_empty = m not in marks and backward[i][m + 1]
            place_here = (m not in marks or marks[m] == temp) and backward[i + 1][m + 1]
            backward[i][m] = leave_empty or place_here

    result: list[list[int]] = []
    for i, temp in enumerate(readings):
        # 读数 i 放分钟 m：前 i 笔能在 m 之前排完（forward[i][m]），
        # 且 i+1 及以后能从 m+1 排起（backward[i+1][m+1]）；标记位另须同温
        candidates = [
            m
            for m in range(minutes)
            if forward[i][m]
            and backward[i + 1][m + 1]
            and (m not in marks or marks[m] == temp)
        ]
        result.append(candidates)
    return result


# ---------- 试算与登记 ----------

def _load_snapshot(conn: sqlite3.Connection, hour_ts: int) -> dict | None:
    """在当前事务内读取同一已提交状态的版号与有序明细。"""
    row = conn.execute(
        "SELECT version, row_count FROM hours WHERE hour_ts = ?", (hour_ts,)
    ).fetchone()
    if row is None:
        return None
    reading_rows = conn.execute(
        "SELECT temperature FROM readings WHERE hour_ts = ? ORDER BY id", (hour_ts,)
    ).fetchall()
    return {
        "version": row["version"],
        "row_count": row["row_count"],
        "readings": [r["temperature"] for r in reading_rows],
    }


def create_reconciliation(
    conn: sqlite3.Connection,
    hour_ts: int,
    version: int,
    marks: dict[int, float],
    *,
    delay_hook=None,
) -> dict:
    """按提交版号试算一组纸带标记，落库试算单并返回每笔读数的可行分钟。

    版号不符抛 VersionMismatchError、小时无明细抛 HourNotFoundError；
    无可行排法时同样落一张 feasible=0 的试算单并返回 feasible=False。
    delay_hook：读事务快照建立后调用（仅测试用，模拟长计算期间发生更正）。
    """
    conn.execute("BEGIN")  # 读事务：版号与明细取自同一已提交快照
    try:
        snapshot = _load_snapshot(conn, hour_ts)
        if snapshot is None:
            raise HourNotFoundError()
        if snapshot["version"] != version:
            raise VersionMismatchError(version, snapshot["version"])
        readings = snapshot["readings"]
        if delay_hook is not None:
            delay_hook()
        feasible = compute_feasible_minutes(readings, marks)
        conn.execute("COMMIT")
    except Exception:
        _rollback_quietly(conn)
        raise

    is_feasible = feasible is not None
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO reconciliations (hour_ts, version, feasible, reading_count, created_at) "
            "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (hour_ts, version, 1 if is_feasible else 0, len(readings)),
        )
        recon_id = cur.lastrowid
        created_at = conn.execute("SELECT created_at FROM reconciliations WHERE id = ?", (recon_id,)).fetchone()[
            "created_at"
        ]
        conn.executemany(
            "INSERT INTO tape_marks (recon_id, minute, temperature) VALUES (?, ?, ?)",
            [(recon_id, m, t) for m, t in sorted(marks.items())],
        )
        if is_feasible:
            conn.executemany(
                "INSERT INTO reconciliation_readings "
                "(recon_id, entry_order, temperature, feasible_minutes, minute_index) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        recon_id,
                        i,
                        temp,
                        json.dumps(candidates),
                        candidates[0] if len(candidates) == 1 else None,
                    )
                    for i, (temp, candidates) in enumerate(zip(readings, feasible))
                ],
            )
        conn.execute("COMMIT")
    except Exception:
        _rollback_quietly(conn)
        raise

    return {
        "id": recon_id,
        "hour_ts": hour_ts,
        "version": version,
        "feasible": is_feasible,
        "reading_count": len(readings),
        "created_at": created_at,
        "readings": readings,
        "feasible_minutes": feasible if is_feasible else [],
        "marks": marks,
    }


def register_approval(
    conn: sqlite3.Connection, hour_ts: int, recon_id: int, submitted_version: int
) -> dict:
    """把试算单登记为该小时的最近一次核定；登记前核对提交版号并再次核对当前版号。

    - 提交版号与试算单版号不一致：拒绝（不能用别的版号名义登记这张单）；
    - 试算单版号与当前已提交明细版号不一致（计算期间已换版）：拒绝。
    任一不符抛 VersionMismatchError，approvals 保持上次核定不变。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        recon = conn.execute(
            "SELECT id, hour_ts, version, feasible FROM reconciliations WHERE id = ?",
            (recon_id,),
        ).fetchone()
        if recon is None or recon["hour_ts"] != hour_ts:
            raise ReconciliationNotFoundError()
        if not recon["feasible"]:
            # 无可行排法的试算单不能登记；上次核定原样保留
            raise ValueError("infeasible reconciliation cannot be approved")
        current = conn.execute(
            "SELECT version FROM hours WHERE hour_ts = ?", (hour_ts,)
        ).fetchone()
        if current is None:
            raise HourNotFoundError()
        # 登记须核对提交的版号：先对试算单版号，再对当前已提交版号
        if submitted_version != recon["version"] or current["version"] != recon["version"]:
            raise VersionMismatchError(submitted_version, current["version"])

        previous = conn.execute(
            "SELECT recon_id FROM approvals WHERE hour_ts = ?", (hour_ts,)
        ).fetchone()
        conn.execute(
            "INSERT INTO approvals (hour_ts, recon_id, version, created_at) "
            "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) "
            "ON CONFLICT(hour_ts) DO UPDATE SET recon_id = excluded.recon_id, "
            "version = excluded.version, created_at = excluded.created_at",
            (hour_ts, recon_id, recon["version"]),
        )
        conn.execute("COMMIT")
        return {"recon_id": recon_id, "previous_recon_id": previous["recon_id"] if previous else None}
    except Exception:
        _rollback_quietly(conn)
        raise


def _hydrate(conn: sqlite3.Connection, recon_row: sqlite3.Row) -> dict:
    marks = {
        r["minute"]: r["temperature"]
        for r in conn.execute(
            "SELECT minute, temperature FROM tape_marks WHERE recon_id = ? ORDER BY minute",
            (recon_row["id"],),
        ).fetchall()
    }
    reading_rows = conn.execute(
        "SELECT entry_order, temperature, feasible_minutes, minute_index "
        "FROM reconciliation_readings WHERE recon_id = ? ORDER BY entry_order",
        (recon_row["id"],),
    ).fetchall()
    readings_detail = [
        {
            "entry_order": r["entry_order"],
            "temperature": r["temperature"],
            "feasible_minutes": json.loads(r["feasible_minutes"]),
            "minute_index": r["minute_index"],
        }
        for r in reading_rows
    ]
    return {
        "id": recon_row["id"],
        "hour_ts": recon_row["hour_ts"],
        "version": recon_row["version"],
        "feasible": bool(recon_row["feasible"]),
        "reading_count": recon_row["reading_count"],
        "created_at": recon_row["created_at"],
        "marks": marks,
        # 与 create_reconciliation 的成功返回同构：readings 为有序温度，
        # feasible_minutes 为等长候选列表；不可行时两者均为空
        "readings": [r["temperature"] for r in readings_detail],
        "feasible_minutes": [r["feasible_minutes"] for r in readings_detail],
    }

def list_reconciliations(conn: sqlite3.Connection, hour_ts: int) -> list[dict]:
    conn.execute("BEGIN")
    try:
        rows = conn.execute(
            "SELECT id, hour_ts, version, feasible, reading_count, created_at "
            "FROM reconciliations WHERE hour_ts = ? ORDER BY id",
            (hour_ts,),
        ).fetchall()
        result = [_hydrate(conn, r) for r in rows]
        conn.execute("COMMIT")
        return result
    except Exception:
        _rollback_quietly(conn)
        raise


def get_latest_approval(conn: sqlite3.Connection, hour_ts: int) -> dict | None:
    """取最近一次核定；明细已换版时 stale=True。无核定返回 None。"""
    conn.execute("BEGIN")
    try:
        row = conn.execute(
            "SELECT a.recon_id, a.version AS approved_version, a.created_at "
            "FROM approvals a WHERE a.hour_ts = ?",
            (hour_ts,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        current = conn.execute(
            "SELECT version FROM hours WHERE hour_ts = ?", (hour_ts,)
        ).fetchone()
        recon = conn.execute(
            "SELECT id, hour_ts, version, feasible, reading_count, created_at "
            "FROM reconciliations WHERE id = ?",
            (row["recon_id"],),
        ).fetchone()
        payload = _hydrate(conn, recon)
        payload["approved_at"] = row["created_at"]
        payload["approved_version"] = row["approved_version"]
        payload["stale"] = current is None or current["version"] != row["approved_version"]
        payload["current_version"] = current["version"] if current is not None else None
        conn.execute("COMMIT")
        return payload
    except Exception:
        _rollback_quietly(conn)
        raise
