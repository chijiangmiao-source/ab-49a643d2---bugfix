from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    return TestClient(app)


@pytest.fixture()
def make_raw(client):
    def _make(detector: str = "TES-01", summary: str = "原始读数", reading_mk: float = 100.0):
        r = client.post("/api/records", json={
            "kind": "raw", "detector": detector, "summary": summary, "reading_mk": reading_mk,
        })
        assert r.status_code == 201, r.text
        return r.json()

    return _make


@pytest.fixture()
def make_derived(client):
    def _make(deps, detector: str = "TES-01", summary: str = "推导结论"):
        r = client.post("/api/records", json={
            "kind": "derived", "detector": detector, "summary": summary, "depends_on": deps,
        })
        assert r.status_code == 201, r.text
        return r.json()

    return _make
