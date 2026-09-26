"""记录建模与校验反馈的测试。"""

from app.service import find_cycle


def test_create_raw_returns_stable_id_and_fields(client, make_raw):
    rec = make_raw(summary="TES-01 在 100mK 的基底读数")
    assert rec["id"] == "CAL-000001"
    assert rec["kind"] == "raw"
    assert rec["valid"] is True
    assert rec["depends_on"] == []
    assert rec["invalidation"] is None
    again = client.get(f"/api/records/{rec['id']}").json()
    assert again == rec


def test_create_derived_with_multiple_dependencies(client, make_raw, make_derived):
    r1 = make_raw()
    r2 = make_raw()
    d = make_derived([r1["id"], r2["id"]])
    assert d["depends_on"] == [r1["id"], r2["id"]]
    assert d["valid"] is True


def test_ids_are_stable_and_sequential(client, make_raw):
    ids = [make_raw()["id"] for _ in range(3)]
    assert ids == ["CAL-000001", "CAL-000002", "CAL-000003"]


def test_derived_requires_dependencies(client):
    r = client.post("/api/records", json={"kind": "derived", "detector": "TES-01", "summary": "x"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "MISSING_DEPENDENCIES"


def test_raw_cannot_depend(client, make_raw):
    r1 = make_raw()
    r = client.post("/api/records", json={
        "kind": "raw", "detector": "TES-01", "summary": "x", "depends_on": [r1["id"]],
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RAW_CANNOT_DEPEND"


def test_missing_dependency_is_locatable(client):
    r = client.post("/api/records", json={
        "kind": "derived", "detector": "TES-01", "summary": "x", "depends_on": ["CAL-000099"],
    })
    body = r.json()
    assert r.status_code == 422
    assert body["error"]["code"] == "DEPENDENCY_NOT_FOUND"
    assert body["error"]["details"]["missing"] == ["CAL-000099"]


def test_self_reference_is_rejected(client, make_raw):
    make_raw()  # CAL-000001，下一个编号将是 CAL-000002
    r = client.post("/api/records", json={
        "kind": "derived", "detector": "TES-01", "summary": "x", "depends_on": ["CAL-000002"],
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SELF_REFERENCE"
    assert r.json()["error"]["details"]["record_id"] == "CAL-000002"


def test_failed_create_preserves_existing_records(client, make_raw):
    r1 = make_raw()
    client.post("/api/records", json={
        "kind": "derived", "detector": "TES-01", "summary": "x", "depends_on": ["CAL-000099"],
    })
    records = client.get("/api/records").json()["records"]
    assert [r["id"] for r in records] == [r1["id"]]
    assert records[0]["valid"] is True


def test_validation_error_shape(client):
    r = client.post("/api/records", json={"kind": "raw"})
    body = r.json()
    assert r.status_code == 422
    assert body["error"]["code"] == "VALIDATION_ERROR"
    locs = [tuple(f["loc"]) for f in body["error"]["details"]["fields"]]
    assert ("body", "detector") in locs


def test_valid_filter(client, make_raw, make_derived):
    r1 = make_raw()
    d1 = make_derived([r1["id"]])
    client.post(f"/api/records/{r1['id']}/invalidate", json={"operation_id": "op-filter"})
    valid_ids = [r["id"] for r in client.get("/api/records?valid=true").json()["records"]]
    invalid_ids = [r["id"] for r in client.get("/api/records?valid=false").json()["records"]]
    assert valid_ids == []
    assert sorted(invalid_ids) == sorted([r1["id"], d1["id"]])


def test_find_cycle_detects_loop():
    edges = {"A": {"B"}, "B": {"C"}, "C": {"A"}}
    assert find_cycle(edges, "A") == ["A", "B", "C", "A"]


def test_find_cycle_none_for_dag():
    edges = {"A": {"B"}, "B": {"C"}}
    assert find_cycle(edges, "A") is None


def test_find_cycle_self_loop():
    assert find_cycle({"A": {"A"}}, "A") == ["A", "A"]
