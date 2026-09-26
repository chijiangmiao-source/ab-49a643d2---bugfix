"""SQLite 持久化访问层。

设计要点：
- 进程内单一连接 + 可重入锁，所有读写都被串行化；
- 写事务使用 ``BEGIN IMMEDIATE``，在提交前独占数据库文件，
  因此“创建推导记录”与“失效裁决”两个事务绝不会交错；
- 创建推导记录时，依据的存在性/有效性校验与依赖边插入在同一
  写事务内完成（直接读取最新已提交行，不做任何进程内缓存），
  竞争不变量（有效记录不得依赖失效记录）由串行化天然保证：
  推导先于裁决则被级联失效，后于裁决则在事务内被拒绝；
- 所有变更（记录、依赖边、失效标记、操作流水）都在同一个
  持久化提交中落盘，重启后状态可完整恢复。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id                      TEXT PRIMARY KEY,
    seq                     INTEGER NOT NULL UNIQUE,
    kind                    TEXT NOT NULL CHECK (kind IN ('raw', 'derived')),
    detector                TEXT NOT NULL,
    summary                 TEXT NOT NULL,
    reading_mk              REAL,
    valid                   INTEGER NOT NULL DEFAULT 1,
    invalidation_root       TEXT,
    invalidated_by_operation TEXT,
    invalidated_at          TEXT,
    created_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dependencies (
    record_id     TEXT NOT NULL REFERENCES records(id),
    depends_on_id TEXT NOT NULL REFERENCES records(id),
    PRIMARY KEY (record_id, depends_on_id)
);

CREATE INDEX IF NOT EXISTS idx_dependencies_on ON dependencies(depends_on_id);

CREATE TABLE IF NOT EXISTS operations (
    operation_id     TEXT PRIMARY KEY,
    action           TEXT NOT NULL,
    target_record_id TEXT NOT NULL,
    status           TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
"""


class Database:
    """串行化访问的 SQLite 封装。"""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=15000")
        with self._lock:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """独占写事务：提交前任何其他读写都无法进入。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
