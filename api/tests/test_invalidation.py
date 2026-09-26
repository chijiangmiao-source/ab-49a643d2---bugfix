"""失效裁决：级联、幂等重放、操作标识冲突。"""

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import create_app


def _invalidate(client, record_id, op):
    return client.post(f"/api/records/{record_id}/invalidate", json={"operation_id": op})


def test_cascade_invalidation_single_transaction(client, make_raw, make_derived):
    r1 = make_raw()
    r2 = make_raw()
    a = make_derived([r1["id"]])
    b = make_derived([r1["id"], r2["id"]])
    c = make_derived([a["id"], b["id"]])
    d = make_derived([r2["id"]])

    resp = _invalidate(client, r1["id"], "op-cascade")
    assert resp.status_code == 200
    assert resp.headers["X-Idempotent-Replay"] == "false"
    body = resp.json()
    assert body["root"] == r1["id"]
    assert sorted(body["invalidated"]) == sorted([r1["id"], a["id"], b["id"], c["id"]])

    records = {r["id"]: r for r in client.get("/api/records").json()["records"]}
    for rid in (r1["id"], a["id"], b["id"], c["id"]):
        assert records[rid]["valid"] is False
        assert records[rid]["invalidation"]["root"] == r1["id"]
        assert records[rid]["invalidation"]["operation_id"] == "op-cascade"
    assert records[r2["id"]]["valid"] is True
    assert records[d["id"]]["valid"] is True


def test_repeated_adjudication_returns_first_result(client, make_raw, make_derived):
    r1 = make_raw()
    make_derived([r1["id"]])
    first = _invalidate(client, r1["id"], "op-replay").json()
    second_resp = _invalidate(client, r1["id"], "op-replay")
    assert second_resp.status_code == 200
    assert second_resp.headers["X-Idempotent-Replay"] == "true"
    assert second_resp.json() == first


def test_same_operation_id_different_target_conflicts(client, make_raw):
    r1 = make_raw()
    r2 = make_raw()
    _invalidate(client, r1["id"], "op-shared")
    resp = _invalidate(client, r2["id"], "op-shared")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "OPERATION_CONFLICT"
    assert body["error"]["details"]["existing_target"] == r1["id"]
    assert body["error"]["details"]["requested_target"] == r2["id"]
    # 冲突不改变状态
    assert client.get(f"/api/records/{r2['id']}").json()["valid"] is True


def test_invalidate_missing_record_404_and_op_not_consumed(client, make_raw):
    resp = _invalidate(client, "CAL-000099", "op-late")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "RECORD_NOT_FOUND"
    # 失败的裁决不消耗操作标识
    r1 = make_raw()
    ok = _invalidate(client, r1["id"], "op-late")
    assert ok.status_code == 200
    assert ok.json()["root"] == r1["id"]


def test_invalidate_already_invalid_record_records_operation(client, make_raw, make_derived):
    r1 = make_raw()
    d1 = make_derived([r1["id"]])
    _invalidate(client, r1["id"], "op-first")
    resp = _invalidate(client, r1["id"], "op-second")
    assert resp.status_code == 200
    body = resp.json()
    assert body["invalidated"] == []
    assert sorted(body["affected"]) == sorted([r1["id"], d1["id"]])
    # 失效来源保持首次裁决，稳定不被覆盖
    rec = client.get(f"/api/records/{d1['id']}").json()
    assert rec["invalidation"]["operation_id"] == "op-first"
    assert rec["invalidation"]["root"] == r1["id"]
    # 第二个操作标识同样可以幂等重放
    replay = _invalidate(client, r1["id"], "op-second")
    assert replay.json() == body


def test_operation_query_endpoint(client, make_raw):
    r1 = make_raw()
    done = _invalidate(client, r1["id"], "op-query").json()
    got = client.get("/api/operations/op-query")
    assert got.status_code == 200
    assert got.json() == done
    missing = client.get("/api/operations/op-absent")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "OPERATION_NOT_FOUND"


def test_lineage_endpoint(client, make_raw, make_derived):
    r1 = make_raw()
    a = make_derived([r1["id"]])
    b = make_derived([a["id"]])
    lin = client.get(f"/api/records/{a['id']}/lineage").json()
    assert lin["ancestors"] == [r1["id"]]
    assert lin["descendants"] == [b["id"]]
    assert lin["direct_dependencies"] == [r1["id"]]
    assert lin["direct_dependents"] == [b["id"]]
    assert client.get("/api/records/CAL-000099/lineage").status_code == 404


def test_used_basis_invalidated_then_derivation_rejected(client, make_raw, make_derived):
    # 场景：原始读数 → 已被引用的推导 → 失效裁决（级联）→ 再次推导必须被拒绝。
    raw = make_raw()
    first = make_derived([raw["id"]])
    count_before = len(client.get("/api/records").json()["records"])

    inv = _invalidate(client, raw["id"], "op-used-basis")
    assert inv.status_code == 200
    assert inv.json()["invalidated"] == [raw["id"], first["id"]]

    resp = client.post("/api/records", json={
        "kind": "derived", "detector": "TES-01",
        "summary": "裁决后再次引用失效读数", "depends_on": [raw["id"]],
    })
    # 拒绝并给出可定位的失效依据
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "DEPENDENCY_INVALID"
    assert err["details"]["invalid"] == [{
        "id": raw["id"],
        "invalidation_root": raw["id"],
        "invalidated_by_operation": "op-used-basis",
    }]

    # 不生成任何新记录
    records = client.get("/api/records").json()["records"]
    assert len(records) == count_before
    by_id = {r["id"]: r for r in records}
    # 不生成任何新依赖边：失效读数的直接下游仍只有第一条推导
    lin = client.get(f"/api/records/{raw['id']}/lineage").json()
    assert lin["direct_dependents"] == [first["id"]]
    # 编号序列未被失败的创建消耗
    after = make_raw(detector="TES-02", summary="裁决后的新读数")
    assert after["id"] == "CAL-000003"
    # 列表与谱系中不存在“有效记录依赖失效记录”
    for rec in by_id.values():
        if rec["valid"]:
            for dep in rec["depends_on"]:
                assert by_id[dep]["valid"]
    assert not by_id[raw["id"]]["valid"] and not by_id[first["id"]]["valid"]


def test_interleaved_derivation_either_cascades_or_is_rejected(tmp_path):
    # 推导与裁决先后交错：先完成的推导随裁决级联失效；裁决后的推导被拒绝。
    app = create_app(str(tmp_path / "interleave.db"))
    c = TestClient(app)
    raw = c.post("/api/records", json={
        "kind": "raw", "detector": "TES-01", "summary": "交错目标", "reading_mk": 1.0,
    }).json()
    target = raw["id"]

    # 裁决之前完成的推导
    before = c.post("/api/records", json={
        "kind": "derived", "detector": "TES-01", "summary": "裁决前推导",
        "depends_on": [target],
    })
    assert before.status_code == 201
    before_id = before.json()["id"]

    assert _invalidate(c, target, "op-interleave").status_code == 200

    # 裁决之后提交的推导
    after = c.post("/api/records", json={
        "kind": "derived", "detector": "TES-01", "summary": "裁决后推导",
        "depends_on": [target],
    })
    assert after.status_code == 422
    assert after.json()["error"]["code"] == "DEPENDENCY_INVALID"

    records = {r["id"]: r for r in c.get("/api/records").json()["records"]}
    assert records[before_id]["valid"] is False  # 先完成者被级联失效
    assert records[target]["valid"] is False
    assert "裁决后推导" not in {r["summary"] for r in records.values()}  # 未落库
    for rec in records.values():
        if rec["valid"]:
            assert all(records[d]["valid"] for d in rec["depends_on"])
    app.state.db.close()


def test_concurrent_derivation_versus_invalidation_never_contradicts(tmp_path):
    # 并发：任意交错下要么被级联失效，要么被拒绝，绝不出现有效依赖失效。
    app = create_app(str(tmp_path / "concurrent.db"))
    setup = TestClient(app)
    raw = setup.post("/api/records", json={
        "kind": "raw", "detector": "TES-C", "summary": "并发目标", "reading_mk": 7.0,
    }).json()
    target = raw["id"]

    total = 10
    barrier = threading.Barrier(total)

    def do_create(i):
        cl = TestClient(app)
        barrier.wait()
        return cl.post("/api/records", json={
            "kind": "derived", "detector": "TES-C",
            "summary": f"并发推导-{i}", "depends_on": [target],
        })

    def do_invalidate():
        cl = TestClient(app)
        barrier.wait()
        return cl.post(f"/api/records/{target}/invalidate",
                       json={"operation_id": "op-concurrent"})

    with ThreadPoolExecutor(max_workers=total) as pool:
        creates = [pool.submit(do_create, i) for i in range(total - 1)]
        invalidation = pool.submit(do_invalidate)
        create_resps = [f.result() for f in creates]
        inv_resp = invalidation.result()

    assert inv_resp.status_code == 200
    final = TestClient(app)
    records = {r["id"]: r for r in final.get("/api/records").json()["records"]}
    assert records[target]["valid"] is False
    for resp in create_resps:
        if resp.status_code == 201:
            assert records[resp.json()["id"]]["valid"] is False
        else:
            assert resp.status_code == 422
            assert resp.json()["error"]["code"] == "DEPENDENCY_INVALID"
    for rec in records.values():
        if rec["valid"]:
            assert all(records[d]["valid"] for d in rec["depends_on"])
    # 裁决流水可查询且可幂等重放
    assert final.get("/api/operations/op-concurrent").status_code == 200
    replay = final.post(f"/api/records/{target}/invalidate",
                        json={"operation_id": "op-concurrent"})
    assert replay.headers["X-Idempotent-Replay"] == "true"
    app.state.db.close()
