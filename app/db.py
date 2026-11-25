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
