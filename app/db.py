"""SQLite 连接与建库。

设计要点：
- versions 只插入、不更新、不删除 —— 历史不可变；当前生效状态查询时推导。
- 每个规程的版本号在规程内递增；supersedes_version 指向被更正的基版。
- valid_start/valid_end 表示该版本正文声明生效的半开区间 [start, end)。
- published_at 为发布事务时间（知悉时间），仅服务端写入。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import DB_TIMEOUT, DB_PATH

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS procedures (
    procedure_no     TEXT PRIMARY KEY,
    created_publish  TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS versions (
    procedure_no        TEXT NOT NULL REFERENCES procedures(procedure_no),
    version_no          INTEGER NOT NULL,
    valid_start         TEXT NOT NULL,
    valid_end           TEXT NOT NULL,
    body                TEXT NOT NULL,
    published_at        TEXT NOT NULL,
    supersedes_version  INTEGER,
    PRIMARY KEY (procedure_no, version_no),
    FOREIGN KEY (procedure_no, supersedes_version)
        REFERENCES versions (procedure_no, version_no)
);

CREATE INDEX IF NOT EXISTS idx_versions_lookup
    ON versions (procedure_no, valid_start, valid_end, published_at);
"""


def connect(db_path: str | None = None, timeout: float = DB_TIMEOUT) -> sqlite3.Connection:
    path = db_path or DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = %d" % int(timeout * 1000))
    return conn


def init_db(db_path: str | None = None) -> None:
    """幂等建表：首次启动 / docker entrypoint 均会调用。"""
    conn = connect(db_path, timeout=max(DB_TIMEOUT, 30))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启 IMMEDIATE 事务：立刻拿写锁。

    作用是把同一份历史上的并发写串行化，配合校验保证
    “冲突更正整体拒绝且不改历史”；任何异常都会回滚。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
