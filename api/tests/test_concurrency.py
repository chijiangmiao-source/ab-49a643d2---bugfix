"""并发竞争不变量：新推导与失效裁决竞争后，不得存在有效记录依赖失效记录。"""

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import create_app


def test_race_between_derivation_and_invalidation(tmp_path):
    app = create_app(str(tmp_path / "race.db"))
    setup = TestClient(app)
    raw = setup.post("/api/records", json={
        "kind": "raw", "detector": "TES-RACE", "summary": "竞争目标", "reading_mk": 42.0,
    }).json()
    target = raw["id"]

    creators, invalidators = 8, 2
    barrier = threading.Barrier(creators + invalidators)

    def do_create(i):
        client = TestClient(app)
        barrier.wait()
        return client.post("/api/records", json={
            "kind": "derived", "detector": "TES-RACE",
            "summary": f"竞争推导-{i}", "depends_on": [target],
        })

    def do_invalidate(_):
        client = TestClient(app)
        barrier.wait()
        return client.post(f"/api/records/{target}/invalidate",
                           json={"operation_id": "op-race"})

    with ThreadPoolExecutor(max_workers=creators + invalidators) as pool:
        create_futs = [pool.submit(do_create, i) for i in range(creators)]
        inv_futs = [pool.submit(do_invalidate, i) for i in range(invalidators)]
        creates = [f.result() for f in create_futs]
        invalids = [f.result() for f in inv_futs]

    # 同一操作标识的并发裁决：只应用一次，所有请求得到一致结果
    assert all(r.status_code == 200 for r in invalids)
    bodies = [r.json() for r in invalids]
    assert all(b == bodies[0] for b in bodies)

    final = TestClient(app)
    records = {r["id"]: r for r in final.get("/api/records").json()["records"]}
    assert records[target]["valid"] is False

    # 创建若成功，则其提交必在裁决之前，因而必被级联失效；
    # 创建若在裁决之后，则必须被拒绝。
    for resp in creates:
        if resp.status_code == 201:
            rid = resp.json()["id"]
            assert records[rid]["valid"] is False, f"{rid} 有效却依赖已失效的 {target}"
        else:
            assert resp.status_code == 422
            assert resp.json()["error"]["code"] == "DEPENDENCY_INVALID"

    # 全局不变量
    for rec in records.values():
        if rec["valid"]:
            for dep in rec["depends_on"]:
                assert records[dep]["valid"], f"{rec['id']} 有效却依赖失效记录 {dep}"

    # 操作结果可查询
    op = final.get("/api/operations/op-race")
    assert op.status_code == 200
    assert op.json() == bodies[0]
