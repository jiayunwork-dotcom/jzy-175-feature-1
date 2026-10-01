import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    app = create_app(db_path=db_path, max_workers=2)
    with TestClient(app) as c:
        yield c


def make_project(client, name="某街道选址"):
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def clinic_payload(**overrides):
    from app.examples import RESIDENTS, STATIONS
    payload = {
        "residents": [{"id": i, "x": x, "y": y} for i, x, y in RESIDENTS],
        "stations": [{"id": i, "x": x, "y": y} for i, x, y in STATIONS],
    }
    payload.update(overrides)
    return payload


def create_clinic_version(client, pid, **overrides):
    body = clinic_payload(**overrides)
    r = client.post(f"/api/projects/{pid}/versions", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def wait_job(client, job_id, timeout=30.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200
        job = r.json()
        if job["status"] in ("completed", "timeout", "cancelled",
                             "interrupted", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def start_solve(client, pid, vid, radius, forced=None, time_limit=None):
    body = {"radius": radius,
            "forced_station_ids": forced or []}
    if time_limit is not None:
        body["time_limit"] = time_limit
    r = client.post(f"/api/projects/{pid}/versions/{vid}/solve", json=body)
    assert r.status_code == 202, r.text
    return r.json()
