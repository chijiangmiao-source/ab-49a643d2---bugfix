"""标定记录业务逻辑：建模、级联失效、幂等裁决、谱系查询。

所有错误都以 ``ApiError`` 抛出，携带可定位的错误码与结构化细节，
由 HTTP 层统一映射为 ``{"error": {"code", "message", "details"}}``。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .db import Database


class ApiError(Exception):
    """可直接映射为 HTTP 响应的业务错误。"""

    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# 图结构辅助
# ---------------------------------------------------------------------------

def _dependency_edges(conn) -> dict[str, set[str]]:
    """record_id -> 其直接依据集合。"""
    edges: dict[str, set[str]] = {}
    for row in conn.execute("SELECT record_id, depends_on_id FROM dependencies"):
        edges.setdefault(row["record_id"], set()).add(row["depends_on_id"])
    return edges


def _dependents_adjacency(conn) -> dict[str, set[str]]:
    """depends_on_id -> 直接依赖它的记录集合（下游方向）。"""
    adj: dict[str, set[str]] = {}
    for row in conn.execute("SELECT record_id, depends_on_id FROM dependencies"):
        adj.setdefault(row["depends_on_id"], set()).add(row["record_id"])
    return adj


def find_cycle(edges: dict[str, set[str]], start: str) -> list[str] | None:
    """若 ``start`` 沿 depends_on 边能回到自身，返回环路径 [start, ..., start]。

    正常情况下依赖图是只增的 DAG（依据必须先存在），本检查是防御性的：
    任何经过新节点的环都会在提交前被拦截。
    """
    path = [start]
    on_path = {start}
    stack = [(start, iter(sorted(edges.get(start, ()))))]
    while stack:
        _, it = stack[-1]
        advanced = False
        for nxt in it:
            if nxt == start:
                return path + [start]
            if nxt in on_path:
                continue
            on_path.add(nxt)
            path.append(nxt)
            stack.append((nxt, iter(sorted(edges.get(nxt, ())))))
            advanced = True
            break
        if not advanced:
            stack.pop()
            on_path.discard(path.pop())
    return None


def _reach(start: str, adj: dict[str, set[str]]) -> list[str]:
    """从 start 出发沿邻接表可达的全部节点（不含自身），排序返回。"""
    seen: set[str] = set()
    queue = [start]
    while queue:
        cur = queue.pop(0)
        for nxt in sorted(adj.get(cur, ())):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return sorted(seen)


# ---------------------------------------------------------------------------
# 记录序列化
# ---------------------------------------------------------------------------

def _record_dict(conn, row) -> dict[str, Any]:
    deps = [
        r["depends_on_id"]
        for r in conn.execute(
            "SELECT depends_on_id FROM dependencies WHERE record_id = ? ORDER BY depends_on_id",
            (row["id"],),
        )
    ]
    invalidation = None
    if not row["valid"]:
        invalidation = {
            "root": row["invalidation_root"],
            "operation_id": row["invalidated_by_operation"],
            "at": row["invalidated_at"],
        }
    return {
        "id": row["id"],
        "seq": row["seq"],
        "kind": row["kind"],
        "detector": row["detector"],
        "summary": row["summary"],
        "reading_mk": row["reading_mk"],
        "valid": bool(row["valid"]),
        "depends_on": deps,
        "invalidation": invalidation,
        "created_at": row["created_at"],
    }


def _fetch_record(conn, record_id: str):
    return conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()


def _validate_dependencies(db: Database, deps: list[str]) -> None:
    if not deps:
        return
    existing = db.dependency_statuses(deps)
    missing = [dep for dep in deps if dep not in existing]
    if missing:
        raise ApiError(
            422, "DEPENDENCY_NOT_FOUND",
            "引用的前序记录不存在",
            {"missing": missing},
        )
    invalid = [dep for dep in deps if not existing[dep][0]]
    if invalid:
        raise ApiError(
            422, "DEPENDENCY_INVALID",
            "引用的前序记录已失效，不能作为依据",
            {"invalid": [
                {"id": dep, "invalidation_root": existing[dep][1]}
                for dep in invalid
            ]},
        )


# ---------------------------------------------------------------------------
# 创建记录
# ---------------------------------------------------------------------------

def create_record(
    db: Database,
    *,
    kind: str,
    detector: str,
    summary: str,
    reading_mk: float | None,
    depends_on: list[str],
) -> dict[str, Any]:
    deps = list(dict.fromkeys(depends_on))  # 去重并保持顺序
    if kind == "raw" and deps:
        raise ApiError(
            422, "RAW_CANNOT_DEPEND",
            "原始记录不依赖任何前序记录，不应携带 depends_on",
            {"depends_on": deps},
        )
    if kind == "derived" and not deps:
        raise ApiError(
            422, "MISSING_DEPENDENCIES",
            "推导记录必须选择至少一条当前有效的前序记录",
            {},
        )

    _validate_dependencies(db, deps)

    with db.write() as conn:
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM records").fetchone()["s"]
        record_id = f"CAL-{seq:06d}"

        # 自引用：依赖列表中出现即将分配的编号（客户端可能预测下一编号）。
        if record_id in deps:
            raise ApiError(
                422, "SELF_REFERENCE",
                "记录不能以自身作为依据（自引用）",
                {"record_id": record_id},
            )

        # 防御性环检测：新边加入后若形成经过新节点的环则拒绝。
        edges = _dependency_edges(conn)
        edges[record_id] = set(deps)
        cycle = find_cycle(edges, record_id)
        if cycle:
            raise ApiError(
                422, "CYCLE_DETECTED",
                "引用关系会形成环",
                {"cycle": cycle},
            )

        now = _now()
        conn.execute(
            "INSERT INTO records (id, seq, kind, detector, summary, reading_mk, valid, created_at)"
            " VALUES (?,?,?,?,?,?,1,?)",
            (record_id, seq, kind, detector, summary, reading_mk, now),
        )
        conn.executemany(
            "INSERT INTO dependencies (record_id, depends_on_id) VALUES (?,?)",
            [(record_id, d) for d in deps],
        )
        return _record_dict(conn, _fetch_record(conn, record_id))


# ---------------------------------------------------------------------------
# 失效裁决
# ---------------------------------------------------------------------------

def invalidate_record(db: Database, record_id: str, operation_id: str) -> tuple[dict[str, Any], bool]:
    """对 record_id 执行携带 operation_id 的失效裁决。

    返回 ``(响应体, 是否重放)``。同一持久化提交中完成：
    目标及全部可达下游记录的失效标记 + 操作流水登记。
    """
    with db.write() as conn:
        op = conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if op is not None:
            if op["target_record_id"] != record_id:
                raise ApiError(
                    409, "OPERATION_CONFLICT",
                    "该操作标识已用于另一条记录的失效裁决",
                    {
                        "operation_id": operation_id,
                        "existing_target": op["target_record_id"],
                        "requested_target": record_id,
                    },
                )
            # 幂等重放：原样返回首次裁决结果，不改变任何状态。
            return json.loads(op["result_json"]), True

        rec = _fetch_record(conn, record_id)
        if rec is None:
            raise ApiError(
                404, "RECORD_NOT_FOUND",
                "目标记录不存在",
                {"record_id": record_id},
            )

        adj = _dependents_adjacency(conn)
        affected: list[str] = []
        seen = {record_id}
        queue = [record_id]
        while queue:
            cur = queue.pop(0)
            affected.append(cur)
            for nxt in sorted(adj.get(cur, ())):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)

        now = _now()
        newly: list[str] = []
        for rid in affected:
            row = conn.execute("SELECT valid FROM records WHERE id = ?", (rid,)).fetchone()
            if row["valid"]:
                # 仅首次失效时写入失效来源，保证失效来源稳定不被后续裁决覆盖。
                conn.execute(
                    "UPDATE records SET valid = 0, invalidation_root = ?,"
                    " invalidated_by_operation = ?, invalidated_at = ? WHERE id = ?",
                    (record_id, operation_id, now, rid),
                )
                newly.append(rid)

        body = {
            "operation_id": operation_id,
            "action": "invalidate",
            "root": record_id,
            "invalidated": newly,
            "affected": affected,
            "at": now,
        }
        conn.execute(
            "INSERT INTO operations (operation_id, action, target_record_id, status, result_json, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (operation_id, "invalidate", record_id, "applied",
             json.dumps(body, ensure_ascii=False), now),
        )
        return body, False


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def list_records(db: Database, valid: str | None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM records"
    if valid == "true":
        sql += " WHERE valid = 1"
    elif valid == "false":
        sql += " WHERE valid = 0"
    sql += " ORDER BY seq"
    with db.read() as conn:
        return [_record_dict(conn, r) for r in conn.execute(sql)]


def get_record(db: Database, record_id: str) -> dict[str, Any]:
    with db.read() as conn:
        row = _fetch_record(conn, record_id)
        if row is None:
            raise ApiError(404, "RECORD_NOT_FOUND", "记录不存在", {"record_id": record_id})
        return _record_dict(conn, row)


def get_lineage(db: Database, record_id: str) -> dict[str, Any]:
    with db.read() as conn:
        row = _fetch_record(conn, record_id)
        if row is None:
            raise ApiError(404, "RECORD_NOT_FOUND", "记录不存在", {"record_id": record_id})
        edges = _dependency_edges(conn)
        adj = _dependents_adjacency(conn)
        return {
            "id": record_id,
            "direct_dependencies": sorted(edges.get(record_id, ())),
            "direct_dependents": sorted(adj.get(record_id, ())),
            "ancestors": _reach(record_id, edges),
            "descendants": _reach(record_id, adj),
        }


def get_operation(db: Database, operation_id: str) -> dict[str, Any]:
    with db.read() as conn:
        op = conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if op is None:
            raise ApiError(
                404, "OPERATION_NOT_FOUND", "操作标识不存在", {"operation_id": operation_id}
            )
        return json.loads(op["result_json"])
