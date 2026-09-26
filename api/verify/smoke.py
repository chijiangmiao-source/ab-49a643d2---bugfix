"""针对运行中服务的 API/HTTP 冒烟。

复现业务关键场景：级联失效、幂等重放、操作标识冲突、可定位错误反馈，
以及“新推导与失效裁决竞争后不得存在有效记录依赖失效记录”的并发不变量。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, cond: bool, info: str = "") -> bool:
        suffix = f" | {info}" if info and not cond else ""
        print(f"[smoke] {'PASS' if cond else 'FAIL'}: {name}{suffix}", flush=True)
        if not cond:
            self.failures.append(name)
        return cond


def wait_for_health(api_base: str, timeout_s: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{api_base}/health", timeout=3.0)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
    return False


def _get_with_retry(url: str, attempts: int = 5, delay: float = 2.0) -> httpx.Response:
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            return httpx.get(url, timeout=10.0, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def run_smoke(api_base: str, web_base: str | None) -> bool:
    ck = Checker()
    if not ck.check("等待 api 健康端点就绪", wait_for_health(api_base)):
        return False

    suffix = str(int(time.time() * 1000))  # 保证重复验收时操作标识不冲突

    def op(name: str) -> str:
        return f"smoke-{name}-{suffix}"

    with httpx.Client(base_url=api_base, timeout=15.0) as c:
        # --- 建模：原始记录 + 多级推导链 ---
        r1 = c.post("/api/records", json={
            "kind": "raw", "detector": "TES-01", "summary": "100mK 基底读数", "reading_mk": 100.0,
        })
        r2 = c.post("/api/records", json={
            "kind": "raw", "detector": "TES-02", "summary": "参考读数", "reading_mk": 87.5,
        })
        ck.check("创建原始记录", r1.status_code == 201 and r2.status_code == 201,
                 f"{r1.status_code} {r2.status_code}")
        R1, R2 = r1.json()["id"], r2.json()["id"]
        ck.check("稳定编号格式", R1.startswith("CAL-") and R2.startswith("CAL-"),
                 f"{R1} {R2}")

        d1 = c.post("/api/records", json={
            "kind": "derived", "detector": "TES-01", "summary": "噪声标定", "depends_on": [R1],
        })
        d2 = c.post("/api/records", json={
            "kind": "derived", "detector": "TES-01", "summary": "响应度标定",
            "depends_on": [R1, R2],
        })
        ck.check("创建推导记录（多前序）",
                 d1.status_code == 201 and d2.status_code == 201,
                 f"{d1.status_code} {d2.status_code}")
        D1, D2 = d1.json()["id"], d2.json()["id"]
        d3 = c.post("/api/records", json={
            "kind": "derived", "detector": "TES-01", "summary": "综合结论",
            "depends_on": [D1, D2],
        })
        D3 = d3.json()["id"]
        ck.check("推导记录返回直接依据", d3.json()["depends_on"] == [D1, D2])

        # --- 可定位错误反馈 ---
        missing = c.post("/api/records", json={
            "kind": "derived", "detector": "TES-01", "summary": "x", "depends_on": ["CAL-999999"],
        })
        mbody = missing.json()
        ck.check("引用不存在记录被拒绝且可定位",
                 missing.status_code == 422
                 and mbody["error"]["code"] == "DEPENDENCY_NOT_FOUND"
                 and mbody["error"]["details"]["missing"] == ["CAL-999999"],
                 missing.text)

        # --- 级联失效：同一持久化提交 ---
        inv = c.post(f"/api/records/{R1}/invalidate", json={"operation_id": op("cascade")})
        ck.check("失效裁决返回 200", inv.status_code == 200, inv.text)
        body = inv.json()
        ck.check("级联覆盖全部可达下游",
                 sorted(body["invalidated"]) == sorted([R1, D1, D2, D3]), str(body))
        records = {r["id"]: r for r in c.get("/api/records").json()["records"]}
        ck.check("失效来源稳定",
                 all(records[x]["invalidation"]["root"] == R1
                     and records[x]["invalidation"]["operation_id"] == op("cascade")
                     for x in (R1, D1, D2, D3)))
        ck.check("未受影响记录保持有效", records[R2]["valid"] is True)

        # --- 幂等重放 ---
        replay = c.post(f"/api/records/{R1}/invalidate", json={"operation_id": op("cascade")})
        ck.check("重复裁决返回首次结果",
                 replay.status_code == 200 and replay.json() == body
                 and replay.headers.get("X-Idempotent-Replay") == "true")

        # --- 操作标识冲突 ---
        conflict = c.post(f"/api/records/{R2}/invalidate", json={"operation_id": op("cascade")})
        ck.check("同一操作标识改换目标返回 409",
                 conflict.status_code == 409
                 and conflict.json()["error"]["code"] == "OPERATION_CONFLICT",
                 conflict.text)
        ck.check("冲突不改变状态", c.get(f"/api/records/{R2}").json()["valid"] is True)

        # --- 引用已失效记录 ---
        depinv = c.post("/api/records", json={
            "kind": "derived", "detector": "TES-01", "summary": "x", "depends_on": [R1],
        })
        ck.check("引用已失效记录被拒绝",
                 depinv.status_code == 422
                 and depinv.json()["error"]["code"] == "DEPENDENCY_INVALID",
                 depinv.text)

        # --- 自引用（预测下一编号） ---
        seqs = [r["seq"] for r in c.get("/api/records").json()["records"]]
        nxt = f"CAL-{max(seqs) + 1:06d}"
        selfref = c.post("/api/records", json={
            "kind": "derived", "detector": "TES-01", "summary": "x", "depends_on": [nxt],
        })
        ck.check("自引用被拒绝",
                 selfref.status_code == 422
                 and selfref.json()["error"]["code"] == "SELF_REFERENCE",
                 selfref.text)

        # --- 谱系与操作流水查询 ---
        lin = c.get(f"/api/records/{D3}/lineage")
        ck.check("谱系可查询",
                 lin.status_code == 200
                 and set(lin.json()["ancestors"]) == {R1, R2, D1, D2},
                 lin.text)
        opget = c.get(f"/api/operations/{op('cascade')}")
        ck.check("操作重放结果可查询", opget.status_code == 200 and opget.json() == body)

        # --- 并发竞争：新推导 vs 失效裁决 ---
        race_raw = c.post("/api/records", json={
            "kind": "raw", "detector": "TES-RACE", "summary": "竞争目标", "reading_mk": 42.0,
        }).json()
        target = race_raw["id"]
        barrier = threading.Barrier(10)

        def create_i(i: int):
            with httpx.Client(base_url=api_base, timeout=15.0) as cc:
                barrier.wait()
                return cc.post("/api/records", json={
                    "kind": "derived", "detector": "TES-RACE",
                    "summary": f"竞争推导-{i}", "depends_on": [target],
                })

        def invalidate_i(_: int):
            with httpx.Client(base_url=api_base, timeout=15.0) as cc:
                barrier.wait()
                return cc.post(f"/api/records/{target}/invalidate",
                               json={"operation_id": op("race")})

        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(create_i, i) for i in range(8)]
            futs += [pool.submit(invalidate_i, i) for i in range(2)]
            results = [f.result() for f in futs]
        creates, invs = results[:8], results[8:]

        ck.check("并发裁决均成功且结果一致",
                 all(r.status_code == 200 for r in invs)
                 and all(r.json() == invs[0].json() for r in invs))
        records = {r["id"]: r for r in c.get("/api/records").json()["records"]}
        race_ok = records[target]["valid"] is False
        for resp in creates:
            if resp.status_code == 201:
                race_ok &= records[resp.json()["id"]]["valid"] is False
            else:
                race_ok &= (resp.status_code == 422
                            and resp.json()["error"]["code"] == "DEPENDENCY_INVALID")
        ck.check("竞争后不存在有效记录依赖失效记录", race_ok)
        invariant_ok = all(
            records[d]["valid"]
            for rec in records.values() if rec["valid"] for d in rec["depends_on"]
        )
        ck.check("全局不变量：有效记录不依赖失效记录", invariant_ok)

    # --- 页面与同源代理冒烟 ---
    if web_base:
        try:
            page = _get_with_retry(f"{web_base}/")
            ck.check("页面可访问", page.status_code == 200 and "标定" in page.text,
                     f"status={page.status_code}")
            proxied = _get_with_retry(f"{web_base}/api/records")
            ck.check("页面同源代理 /api 可用",
                     proxied.status_code == 200 and "records" in proxied.json())
            health = _get_with_retry(f"{web_base}/health")
            ck.check("健康端点可经页面端口访问",
                     health.status_code == 200 and health.json()["status"] == "ok")
        except Exception as exc:  # noqa: BLE001
            ck.check("页面冒烟", False, repr(exc))

    print(f"[smoke] 完成，失败 {len(ck.failures)} 项", flush=True)
    return not ck.failures
