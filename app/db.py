"""SQLite 连接与建表。每个请求一条连接，事务在 logic 层显式控制。"""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS hours (
    hour_ts   INTEGER PRIMARY KEY,   -- UTC 整点，epoch 秒
    version   INTEGER NOT NULL,      -- 每次更正 +1
    row_count INTEGER NOT NULL       -- 该小时当前明细行数
);
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hour_ts     INTEGER NOT NULL,
    temperature REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_hour ON readings(hour_ts);
CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts    INTEGER NOT NULL,    -- 含
    end_ts      INTEGER NOT NULL,    -- 不含
    total_count INTEGER NOT NULL,
    over_count  INTEGER NOT NULL,    -- 温度 > 8℃ 的笔数
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS summary_hours (
    summary_id INTEGER NOT NULL REFERENCES summaries(id),
    hour_ts    INTEGER NOT NULL,
    version    INTEGER NOT NULL,     -- 生成摘要时该小时的版本快照
    PRIMARY KEY (summary_id, hour_ts)
);
-- 纸带核对：一次"试算"对应质检员按某版号明细提交的一组纸带标记
CREATE TABLE IF NOT EXISTS reconciliations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hour_ts      INTEGER NOT NULL,
    version      INTEGER NOT NULL,        -- 提交试算时明细的版号（计算与登记都须核对）
    feasible     INTEGER NOT NULL,        -- 1=存在可行排法；0=整组退回
    reading_count INTEGER NOT NULL,       -- 试算时该小时明细行数（来自同一已提交状态）
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recon_hour ON reconciliations(hour_ts);
-- 纸带上标出的分钟（每分钟最多一笔）及其同温要求
CREATE TABLE IF NOT EXISTS tape_marks (
    recon_id    INTEGER NOT NULL REFERENCES reconciliations(id),
    minute      INTEGER NOT NULL CHECK (minute BETWEEN 0 AND 59),
    temperature REAL NOT NULL,
    PRIMARY KEY (recon_id, minute)
);
-- 试算结果：按录入序号逐笔列出可行分钟；唯一可行时登记 minute_index
CREATE TABLE IF NOT EXISTS reconciliation_readings (
    recon_id       INTEGER NOT NULL REFERENCES reconciliations(id),
    entry_order    INTEGER NOT NULL,      -- 0 起，对应 readings 的录入顺序
    temperature    REAL NOT NULL,
    feasible_minutes TEXT NOT NULL,       -- JSON 数组，可能为空
    minute_index   INTEGER,               -- 仅当可行分钟恰有一个时非空
    PRIMARY KEY (recon_id, entry_order)
);
-- 核定登记：每个小时最多保留一条"上次核定"，登记失败（版号不符/无可行排法）不动它
CREATE TABLE IF NOT EXISTS approvals (
    hour_ts    INTEGER PRIMARY KEY,
    recon_id   INTEGER NOT NULL REFERENCES reconciliations(id),
    version    INTEGER NOT NULL,          -- 登记时刻核对过的明细版号
    created_at TEXT NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("COLDCHAIN_DB", "coldchain.db")


def connect() -> sqlite3.Connection:
    # isolation_level=None：自动提交，事务用显式 BEGIN/COMMIT 控制
    conn = sqlite3.connect(db_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode = WAL")  # 读写并发更稳，设置持久生效
    finally:
        conn.close()
