"""重启持久化：谱系、失效状态与操作重放结果在重启后仍可查询。"""

from fastapi.testclient import TestClient

from app.main import create_app


def test_state_survives_restart(tmp_path):
    db_file = str(tmp_path / "persist.db")

    app1 = create_app(db_file)
    c1 = TestClient(app1)
    r1 = c1.post("/api/records", json={
        "kind": "raw", "detector": "TES-01", "summary": "原始", "reading_mk": 100.0,
    }).json()
    d1 = c1.post("/api/records", json={
        "kind": "derived", "detector": "TES-01", "summary": "推导", "depends_on": [r1["id"]],
    }).json()
    first = c1.post(f"/api/records/{r1['id']}/invalidate",
                    json={"operation_id": "op-restart"}).json()
    app1.state.db.close()

    # “重启”：同一数据文件创建新的应用实例
    app2 = create_app(db_file)
    c2 = TestClient(app2)

    records = {r["id"]: r for r in c2.get("/api/records").json()["records"]}
    assert records[r1["id"]]["valid"] is False
    assert records[d1["id"]]["valid"] is False
    assert records[d1["id"]]["invalidation"]["root"] == r1["id"]
    assert records[d1["id"]]["invalidation"]["operation_id"] == "op-restart"

    lineage = c2.get(f"/api/records/{d1['id']}/lineage").json()
    assert lineage["ancestors"] == [r1["id"]]
    assert lineage["direct_dependencies"] == [r1["id"]]

    replay = c2.post(f"/api/records/{r1['id']}/invalidate",
                     json={"operation_id": "op-restart"})
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json() == first

    assert c2.get("/api/operations/op-restart").json() == first

    # 编号序列不回退、不重用
    nxt = c2.post("/api/records", json={
        "kind": "raw", "detector": "TES-02", "summary": "重启后", "reading_mk": 50.0,
    }).json()
    assert nxt["id"] == "CAL-000003"
    app2.state.db.close()
