"""回归：依据被失效裁决后，再次引用它创建推导必须被拒绝。

精确复现事故序列：
原始读数 → 引用它的推导（已使用依据）→ 对原始读数失效裁决（推导被
级联失效）→ 再次创建引用同一原始读数的推导。

正确行为：返回可定位的 DEPENDENCY_INVALID，不生成任何新记录或依赖边，
系统中绝不出现“有效记录直接依赖失效记录”。
"""


def _invalidate(client, record_id, op):
    return client.post(f"/api/records/{record_id}/invalidate", json={"operation_id": op})


def _all_records(client):
    return client.get("/api/records").json()["records"]


def test_rederive_after_invalidation_is_rejected_without_new_record_or_edge(
    client, make_raw, make_derived
):
    raw = make_raw(summary="100mK 基底读数")
    first = make_derived([raw["id"]], summary="第一版标定结论")

    inv = _invalidate(client, raw["id"], "op-retire-reading")
    assert inv.status_code == 200
    # 先完成的推导随裁决级联失效
    assert sorted(inv.json()["invalidated"]) == sorted([raw["id"], first["id"]])

    before = _all_records(client)
    before_ids = {r["id"] for r in before}
    assert len(before) == 2

    # 再次创建引用同一（已失效）原始读数的推导
    resp = client.post("/api/records", json={
        "kind": "derived", "detector": "TES-01",
        "summary": "失效后再次推导", "depends_on": [raw["id"]],
    })
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "DEPENDENCY_INVALID"
    # 失效依据可定位：编号 + 失效裁决根源
    assert err["details"]["invalid"] == [
        {"id": raw["id"], "invalidation_root": raw["id"]}
    ]

    # 不得生成任何新记录
    after = _all_records(client)
    assert {r["id"] for r in after} == before_ids
    assert len(after) == 2
    # 不得生成任何依赖边：失效原始读数的直接被引仍只有第一条推导
    lin = client.get(f"/api/records/{raw['id']}/lineage").json()
    assert lin["direct_dependents"] == [first["id"]]
    # 编号序列也不得被失败的创建消耗（下一条仍接在既有记录之后）
    nxt = client.post("/api/records", json={
        "kind": "raw", "detector": "TES-02", "summary": "新读数", "reading_mk": 50.0,
    })
    assert nxt.status_code == 201
    assert nxt.json()["id"] == "CAL-000003"


def test_partially_invalid_dependencies_are_all_reported(client, make_raw, make_derived):
    good = make_raw(detector="TES-A", summary="仍然有效")
    bad = make_raw(detector="TES-B", summary="已被裁决失真")
    make_derived([bad["id"]], summary="先使用待失效依据")
    _invalidate(client, bad["id"], "op-partial")

    resp = client.post("/api/records", json={
        "kind": "derived", "detector": "TES-A",
        "summary": "混合依据", "depends_on": [good["id"], bad["id"]],
    })
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "DEPENDENCY_INVALID"
    assert err["details"]["invalid"] == [
        {"id": bad["id"], "invalidation_root": bad["id"]}
    ]
    # 有效依据也不能让部分失效的推导落库
    ids = {r["id"] for r in _all_records(client)}
    assert good["id"] in ids and bad["id"] in ids
    assert len(ids) == 3  # 两条原始 + 一条级联失效的推导，失败创建不落库


def test_no_valid_record_ever_depends_on_invalid_one(client, make_raw, make_derived):
    r1 = make_raw()
    r2 = make_raw()
    make_derived([r1["id"]], summary="链上推导")
    _invalidate(client, r1["id"], "op-global")

    # 引用失效记录的各种形式都必须被拒绝
    for deps in ([r1["id"]], [r1["id"], r2["id"]]):
        resp = client.post("/api/records", json={
            "kind": "derived", "detector": "TES-01", "summary": "x", "depends_on": deps,
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "DEPENDENCY_INVALID"

    records = {r["id"]: r for r in _all_records(client)}
    for rec in records.values():
        if rec["valid"]:
            for dep in rec["depends_on"]:
                assert records[dep]["valid"], f"{rec['id']} 有效却依赖失效记录 {dep}"


def test_cascaded_derivation_stays_invalid_and_new_derivation_on_other_reading_ok(
    client, make_raw, make_derived
):
    r1 = make_raw(summary="失效读数")
    r2 = make_raw(summary="完好读数")
    cascaded = make_derived([r1["id"]], summary="随裁决失效")
    _invalidate(client, r1["id"], "op-selective")

    assert client.get(f"/api/records/{cascaded['id']}").json()["valid"] is False
    # 引用仍然有效的其他读数不受影响
    ok = client.post("/api/records", json={
        "kind": "derived", "detector": "TES-01",
        "summary": "基于完好读数", "depends_on": [r2["id"]],
    })
    assert ok.status_code == 201
    assert ok.json()["valid"] is True
    assert ok.json()["depends_on"] == [r2["id"]]
