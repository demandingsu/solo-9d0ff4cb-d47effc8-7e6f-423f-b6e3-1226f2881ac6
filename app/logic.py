"""核心业务：录入/更正整点明细、生成摘要、按配额规划并执行查询。

事务约定：
- 写操作（录入、更正、生成摘要）用 BEGIN IMMEDIATE，单事务提交。
- 查询用一条读事务（BEGIN），有效摘要集合与小时行数来自同一已提交快照；
  规划阶段只读 hours/summaries 元数据，确认不缺档、不超预算后才真正读取明细行。
"""
from __future__ import annotations

import sqlite3

HOUR_SECONDS = 3600
OVER_TEMP_THRESHOLD = 8.0  # 超过 8℃ 判定为超温（严格大于）

# 与当前 hours 版本逐小时比对，任一不一致（或小时已被移除）则摘要失效
_VALID_SUMMARIES_SQL = """
SELECT s.id, s.start_ts, s.end_ts, s.total_count, s.over_count
FROM summaries s
WHERE s.start_ts >= ? AND s.end_ts <= ?
  AND NOT EXISTS (
      SELECT 1
      FROM summary_hours sh
      LEFT JOIN hours h ON h.hour_ts = sh.hour_ts
      WHERE sh.summary_id = s.id
        AND (h.version IS NULL OR h.version <> sh.version)
  )
ORDER BY s.id
"""


class HourExistsError(Exception):
    """该小时已有明细，应改用更正接口。"""


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass  # 事务已提交或未开启


class MissingHoursError(Exception):
    """区间内存在既无明细、也不被任何有效摘要覆盖的小时。"""

    def __init__(self, missing_ts: list[int]):
        super().__init__(f"missing {len(missing_ts)} hour(s)")
        self.missing_ts = missing_ts


class BudgetExceededError(Exception):
    """最优方案需要读取的明细行数仍超过上限。"""

    def __init__(self, required_rows: int):
        super().__init__(f"requires {required_rows} detail rows")
        self.required_rows = required_rows


def record_hour(conn: sqlite3.Connection, hour_ts: int, temps: list[float], *, overwrite: bool) -> int:
    """录入（overwrite=False）或更正（overwrite=True）某整点的全部明细，返回新版本号。

    更正是整小时替换：旧明细删除、版本号 +1，依赖旧版本生成的摘要随之失效。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT version FROM hours WHERE hour_ts = ?", (hour_ts,)).fetchone()
        if row is not None and not overwrite:
            raise HourExistsError()
        version = 1 if row is None else row["version"] + 1
        conn.execute("DELETE FROM readings WHERE hour_ts = ?", (hour_ts,))
        conn.executemany(
            "INSERT INTO readings (hour_ts, temperature) VALUES (?, ?)",
            [(hour_ts, t) for t in temps],
        )
        conn.execute(
            "INSERT INTO hours (hour_ts, version, row_count) VALUES (?, ?, ?) "
            "ON CONFLICT(hour_ts) DO UPDATE SET version = excluded.version, row_count = excluded.row_count",
            (hour_ts, version, len(temps)),
        )
        conn.execute("COMMIT")
        return version
    except Exception:
        _rollback_quietly(conn)
        raise


def make_summary(conn: sqlite3.Connection, start_ts: int, end_ts: int) -> dict:
    """为齐备的整点区间生成摘要；任一小时缺明细则拒绝。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT hour_ts, version, row_count FROM hours WHERE hour_ts >= ? AND hour_ts < ?",
            (start_ts, end_ts),
        ).fetchall()
        present = {r["hour_ts"]: r for r in rows}
        missing = [t for t in range(start_ts, end_ts, HOUR_SECONDS) if t not in present]
        if missing:
            raise MissingHoursError(missing)
        total, over = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(temperature > ?), 0) FROM readings "
            "WHERE hour_ts >= ? AND hour_ts < ?",
            (OVER_TEMP_THRESHOLD, start_ts, end_ts),
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO summaries (start_ts, end_ts, total_count, over_count, created_at) "
            "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (start_ts, end_ts, total, over),
        )
        summary_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO summary_hours (summary_id, hour_ts, version) VALUES (?, ?, ?)",
            [(summary_id, t, present[t]["version"]) for t in range(start_ts, end_ts, HOUR_SECONDS)],
        )
        conn.execute("COMMIT")
        return {"id": summary_id, "total_count": total, "over_count": over}
    except Exception:
        _rollback_quietly(conn)
        raise


def query_range(conn: sqlite3.Connection, start_ts: int, end_ts: int, max_detail_rows: int) -> dict:
    """在单个读事务（同一已提交状态）内规划并执行查询。

    返回 {"segments": [...], "total": int, "over": int, "detail_rows_read": int}。
    缺档抛 MissingHoursError、超预算抛 BudgetExceededError，均在读取明细之前。
    """
    conn.execute("BEGIN")  # 读事务：以下所有 SELECT 看到同一快照
    try:
        hours = {
            r["hour_ts"]: r
            for r in conn.execute(
                "SELECT hour_ts, version, row_count FROM hours WHERE hour_ts >= ? AND hour_ts < ?",
                (start_ts, end_ts),
            ).fetchall()
        }
        summaries = [dict(r) for r in conn.execute(_VALID_SUMMARIES_SQL, (start_ts, end_ts)).fetchall()]

        n = (end_ts - start_ts) // HOUR_SECONDS
        # 按起点归类摘要；同时标记哪些小时被有效摘要覆盖（用于缺档诊断）
        summaries_from: dict[int, list[tuple[int, dict]]] = {}
        covered = [False] * n
        for s in summaries:
            i = (s["start_ts"] - start_ts) // HOUR_SECONDS
            j = (s["end_ts"] - start_ts) // HOUR_SECONDS
            summaries_from.setdefault(i, []).append((j, s))
            for k in range(i, j):
                covered[k] = True

        # DP：dp[i] = 覆盖前 i 小时所需的最少明细行数；并列时取段数更少者
        INF = float("inf")
        dp = [INF] * (n + 1)
        segs = [INF] * (n + 1)
        choice: list[tuple | None] = [None] * (n + 1)
        dp[0], segs[0] = 0, 0
        for i in range(n):
            if dp[i] == INF:
                continue
            h = hours.get(start_ts + i * HOUR_SECONDS)
            if h is not None:  # 该小时按明细读取，成本 = 行数
                cand = (dp[i] + h["row_count"], segs[i] + 1)
                if cand < (dp[i + 1], segs[i + 1]):
                    dp[i + 1], segs[i + 1] = cand
                    choice[i + 1] = ("details", i, None)
            for j, s in summaries_from.get(i, []):  # 摘要段，明细成本为 0
                cand = (dp[i], segs[i] + 1)
                if cand < (dp[j], segs[j]):
                    dp[j], segs[j] = cand
                    choice[j] = ("summary", i, s)

        if dp[n] == INF:
            missing = [
                start_ts + k * HOUR_SECONDS
                for k in range(n)
                if (start_ts + k * HOUR_SECONDS) not in hours and not covered[k]
            ]
            raise MissingHoursError(missing)
        if dp[n] > max_detail_rows:
            raise BudgetExceededError(dp[n])

        # 回溯最优方案，并把相邻的明细小时合并为一段（成本不变）
        plan: list[tuple[int, int, str, dict | None]] = []
        j = n
        while j > 0:
            kind, i, s = choice[j]
            if kind == "details" and plan and plan[-1][2] == "details" and plan[-1][0] == j:
                plan[-1] = (i, plan[-1][1], "details", None)
            else:
                plan.append((i, j, kind, s))
            j = i
        plan.reverse()

        # 通过预算检查后才真正读取明细行
        segments = []
        total = over = rows_read = 0
        for i, j, kind, s in plan:
            seg_start = start_ts + i * HOUR_SECONDS
            seg_end = start_ts + j * HOUR_SECONDS
            if kind == "summary":
                seg_total, seg_over, summary_id = s["total_count"], s["over_count"], s["id"]
            else:
                hour_list = [start_ts + k * HOUR_SECONDS for k in range(i, j)]
                fetched = conn.execute(
                    f"SELECT temperature FROM readings WHERE hour_ts IN "
                    f"({','.join('?' * len(hour_list))})",
                    hour_list,
                ).fetchall()
                seg_total = len(fetched)
                seg_over = sum(1 for r in fetched if r["temperature"] > OVER_TEMP_THRESHOLD)
                summary_id = None
                rows_read += seg_total
            total += seg_total
            over += seg_over
            segments.append(
                {
                    "start_ts": seg_start,
                    "end_ts": seg_end,
                    "source": kind,
                    "summary_id": summary_id,
                    "total_readings": seg_total,
                    "over_temp_readings": seg_over,
                }
            )
        assert rows_read == dp[n], "实际读取行数应与规划一致"
        conn.execute("COMMIT")
        return {"segments": segments, "total": total, "over": over, "detail_rows_read": rows_read}
    except Exception:
        _rollback_quietly(conn)
        raise


def list_hours(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT hour_ts, version, row_count FROM hours ORDER BY hour_ts").fetchall()


def list_summaries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.id, s.start_ts, s.end_ts, s.total_count, s.over_count, s.created_at,
               CASE WHEN EXISTS (
                   SELECT 1 FROM summary_hours sh
                   LEFT JOIN hours h ON h.hour_ts = sh.hour_ts
                   WHERE sh.summary_id = s.id
                     AND (h.version IS NULL OR h.version <> sh.version)
               ) THEN 0 ELSE 1 END AS valid
        FROM summaries s ORDER BY s.id
        """
    ).fetchall()
