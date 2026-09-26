"""SQLite 持久化访问层。

设计要点：
- 进程内单一连接 + 可重入锁，所有读写都被串行化；
- 写事务使用 ``BEGIN IMMEDIATE``，在提交前独占数据库文件，
  因此“创建推导记录”与“失效裁决”两个事务绝不会交错，
  竞争不变量（有效记录不得依赖失效记录）由串行化天然保证；
- 依据有效性始终从事务内实时读取，不做跨事务缓存，失效裁决一旦
  提交，后续推导立即看到依据失效；
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

    def dependency_statuses(
        self, conn: sqlite3.Connection, record_ids: list[str]
    ) -> dict[str, tuple[bool, str | None]]:
        """在给定事务连接上读取依据记录的当前状态。

        状态直接来自当前事务可见的最新数据，不做跨事务缓存——
        失效裁决提交后，任何后续创建都必然读到 invalid，避免陈旧
        缓存导致“有效结论依赖失效记录”。
        """
        if not record_ids:
            return {}
        ids = list(dict.fromkeys(record_ids))
        marks = ",".join("?" for _ in ids)
        return {
            row["id"]: (bool(row["valid"]), row["invalidation_root"])
            for row in conn.execute(
                f"SELECT id, valid, invalidation_root FROM records WHERE id IN ({marks})", ids
            )
        }

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
