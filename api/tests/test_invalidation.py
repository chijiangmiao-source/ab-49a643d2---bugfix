"""失效裁决：级联、幂等重放、操作标识冲突。"""


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
