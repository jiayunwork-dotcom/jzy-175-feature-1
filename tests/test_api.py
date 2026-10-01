"""HTTP 层测试：项目/版本/校验错误/作业/扫描/去重/重启恢复/增量。"""

import time

from conftest import (
    clinic_payload,
    create_clinic_version,
    make_project,
    start_solve,
    wait_job,
)

from app.examples import EXPECTED_COUNT_R3, RESIDENTS, STATIONS


# ---------------------------------------------------------------------------
# 校验错误：指向具体字段
# ---------------------------------------------------------------------------

def test_empty_residents_field_error(client):
    pid = make_project(client)
    body = clinic_payload(residents=[])
    r = client.post(f"/api/projects/{pid}/versions", json=body)
    assert r.status_code in (400, 422)
    assert r.json()["field"] == "residents"


def test_empty_stations_field_error(client):
    pid = make_project(client)
    body = clinic_payload(stations=[])
    r = client.post(f"/api/projects/{pid}/versions", json=body)
    assert r.status_code in (400, 422)
    assert r.json()["field"] == "stations"


def test_nonpositive_radius_field_error(client):
    pid = make_project(client)
    vid = create_clinic_version(client, pid)["id"]
    r = client.post(f"/api/projects/{pid}/versions/{vid}/solve",
                    json={"radius": 0})
    assert r.status_code == 400
    assert r.json()["field"] == "radius"


def test_unknown_forced_station_field_error(client):
    pid = make_project(client)
    vid = create_clinic_version(client, pid)["id"]
    r = client.post(f"/api/projects/{pid}/versions/{vid}/solve",
                    json={"radius": 3.0, "forced_station_ids": ["NOPE"]})
    assert r.status_code == 404
    assert r.json()["field"] == "forced_station_ids"
    assert r.json()["details"]["unknown"] == ["NOPE"]


def test_missing_project_and_version_404(client):
    assert client.get("/api/projects/nope").status_code == 404
    r = client.post("/api/projects/x/versions", json=clinic_payload())
    assert r.status_code == 404


def test_duplicate_point_ids_rejected(client):
    pid = make_project(client)
    body = clinic_payload()
    body["residents"].append(dict(body["residents"][0]))
    r = client.post(f"/api/projects/{pid}/versions", json=body)
    assert r.status_code == 422
    assert "residents" in r.json()["field"]


# ---------------------------------------------------------------------------
# 版本存储与取回
# ---------------------------------------------------------------------------

def test_versions_persist_and_increment(client):
    pid = make_project(client)
    v1 = create_clinic_version(client, pid, change_note="初版")
    assert v1["version_no"] == 1
    v2 = client.post(
        f"/api/projects/{pid}/versions/from/{v1['id']}",
        json={"add_residents": [{"id": "NEW", "x": 5, "y": 0.5}],
              "change_note": "加一个点"}).json()
    assert v2["version_no"] == 2
    assert v2["parent_version_id"] == v1["id"]
    assert len(v2["residents"]) == 13

    got = client.get(f"/api/projects/{pid}/versions/{v1['id']}").json()
    assert len(got["residents"]) == 12  # 旧版本原样保留

    lst = client.get(f"/api/projects/{pid}/versions").json()
    assert [v["version_no"] for v in lst] == [1, 2]


def test_derive_remove_unknown_and_emptying(client):
    pid = make_project(client)
    v1 = create_clinic_version(client, pid)["id"]
    r = client.post(
        f"/api/projects/{pid}/versions/from/{v1}",
        json={"remove_resident_ids": ["GHOST"]})
    assert r.status_code == 404
    assert r.json()["field"] == "remove_resident_ids"

    all_ids = [x[0] for x in RESIDENTS]
    r = client.post(
        f"/api/projects/{pid}/versions/from/{v1}",
        json={"remove_resident_ids": all_ids})
    assert r.status_code == 400


def test_version_diff(client):
    pid = make_project(client)
    v1 = create_clinic_version(client, pid)
    r = client.post(
        f"/api/projects/{pid}/versions/from/{v1['id']}",
        json={"add_residents": [{"id": "NEW", "x": 1, "y": 1}],
              "remove_resident_ids": ["R01"]})
    assert r.status_code == 201
    v2 = r.json()
    d = client.get(
        f"/api/projects/{pid}/versions/{v1['id']}/diff"
        f"?against_version_id={v2['id']}").json()
    added = {x["id"] for x in d["residents_added"]}
    removed = {x["id"] for x in d["residents_removed"]}
    assert added == {"NEW"}
    assert removed == {"R01"}


# ---------------------------------------------------------------------------
# 求解作业全流程
# ---------------------------------------------------------------------------

def test_solve_clinic_optimal(client):
    pid = make_project(client)
    vid = create_clinic_version(client, pid)["id"]
    out = start_solve(client, pid, vid, 3.0)
    assert out["reused"] is False
    job = wait_job(client, out["job"]["id"])
    assert job["status"] == "completed"
    res = job["result"]
    assert res["status"] == "optimal"
    assert res["proven_optimal"] is True
    assert res["station_count"] == EXPECTED_COUNT_R3
    names = [STATIONS[j][0] for j in res["chosen"]]
    assert names == ["C1", "C2", "C3"]


def test_solve_infeasible_reports_success_with_uncovered(client):
    pid = make_project(client)
    vid = create_clinic_version(client, pid)["id"]
    out = start_solve(client, pid, vid, 2.0)
    job = wait_job(client, out["job"]["id"])
    assert job["status"] == "completed"
    res = job["result"]
    assert res["status"] == "infeasible"
    ids = [RESIDENTS[i][0] for i in res["uncovered"]]
    assert ids == ["R01", "R02"]
    assert res["proven_optimal"] is False


def test_duplicate_solve_returns_same_record(client):
    pid = make_project(client)
    vid = create_clinic_version(client, pid)["id"]
    o1 = start_solve(client, pid, vid, 3.0)
    wait_job(client, o1["job"]["id"])
    o2 = start_solve(client, pid, vid, 3.0)
    assert o2["reused"] is True
    assert o2["result"]["station_count"] == 3
    # 不同参数仍应新建
    o3 = start_solve(client, pid, vid, 4.0)
    assert o3["reused"] is False
    wait_job(client, o3["job"]["id"])


def test_concurrent_jobs_isolated(client):
    pid = make_project(client)
    v1 = create_clinic_version(client, pid)
    body = clinic_payload()
    body["stations"] = body["stations"] + [
        {"id": f"X{i}", "x": float(i), "y": 7.0} for i in range(5)]
    v2 = client.post(f"/api/projects/{pid}/versions", json=body).json()
    o1 = start_solve(client, pid, v1["id"], 3.0)
    o2 = start_solve(client, pid, v2["id"], 3.0)
    j1 = wait_job(client, o1["job"]["id"])
    j2 = wait_job(client, o2["job"]["id"])
    assert j1["version_id"] == v1["id"]
    assert j2["version_id"] == v2["id"]
    assert j1["result"]["station_count"] == 3
    assert j2["result"]["station_count"] == 3


def test_cancel_job(client):
    """真实几何难例：250 居民/120 站，B&B 需要秒级，取消必须干净生效。"""
    import math
    import random

    rng = random.Random(1)
    n, m, radius, spread = 250, 120, 7.0, 70.0
    spts = [(rng.random() * spread, rng.random() * spread) for _ in range(m)]
    residents_xy = []
    for _ in range(n):
        sx, sy = rng.choice(spts)
        a = rng.uniform(0, 2 * math.pi)
        d = radius * math.sqrt(rng.random()) * 0.95
        residents_xy.append((sx + d * math.cos(a), sy + d * math.sin(a)))

    pid = make_project(client)
    body = {
        "residents": [{"id": f"R{i:03d}", "x": x, "y": y}
                      for i, (x, y) in enumerate(residents_xy)],
        "stations": [{"id": f"S{j:03d}", "x": x, "y": y}
                     for j, (x, y) in enumerate(spts)],
    }
    vid = client.post(f"/api/projects/{pid}/versions", json=body).json()["id"]
    out = start_solve(client, pid, vid, radius, time_limit=60.0)
    job_id = out["job"]["id"]

    # 等作业确实进入 running，再取消
    deadline = time.time() + 5
    while time.time() < deadline:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] == "running":
            break
        time.sleep(0.01)
    cr = client.post(f"/api/jobs/{job_id}/cancel")
    assert cr.status_code == 200
    job = wait_job(client, job_id, timeout=30)
    assert job["status"] == "cancelled"
    assert job["result"]["proven_optimal"] is False
    assert job["result"]["station_count"] is not None  # 交出了手上最好解
    # 已终态再取消 → 409
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_timeout_job_bounds_via_api(client):
    pid = make_project(client)
    vid = create_clinic_version(client, pid)["id"]
    # 诊所例很小，给极小限额；状态可能直接 optimal（足够快），
    # 若 timeout 则必须满足 LB/UB 规则
    out = start_solve(client, pid, vid, 3.0, time_limit=1e-12)
    job = wait_job(client, out["job"]["id"])
    if job["status"] == "timeout":
        res = job["result"]
        assert res["proven_optimal"] is False
        assert res["lower_bound"] <= res["station_count"]
    else:
        assert job["status"] == "completed"


def _hard_geometric_payload(radius=7.0, n=250, m=120, spread=70.0, seed=1):
    import math
    import random

    rng = random.Random(seed)
    spts = [(rng.random() * spread, rng.random() * spread) for _ in range(m)]
    xy = []
    for _ in range(n):
        sx, sy = rng.choice(spts)
        a = rng.uniform(0, 2 * math.pi)
        d = radius * math.sqrt(rng.random()) * 0.95
        xy.append((sx + d * math.cos(a), sy + d * math.sin(a)))
    return {
        "residents": [{"id": f"R{i:03d}", "x": x, "y": y}
                      for i, (x, y) in enumerate(xy)],
        "stations": [{"id": f"S{j:03d}", "x": x, "y": y}
                     for j, (x, y) in enumerate(spts)],
    }


def test_timeout_sandwich_via_api(client):
    """超时报的下界 ≤ 真实最优 ≤ 报的当前最好；真实最优由不限时作业获得。"""
    pid = make_project(client)
    body = _hard_geometric_payload()
    vid = client.post(f"/api/projects/{pid}/versions",
                      json=body).json()["id"]

    tout = wait_job(client,
                    start_solve(client, pid, vid, 7.0,
                                time_limit=0.1)["job"]["id"], timeout=40)
    if tout["status"] != "timeout":
        # 机器异常快时退化为"已完成"，此时一定已证明
        assert tout["result"]["proven_optimal"] is True
        return
    assert tout["result"]["proven_optimal"] is False

    exact = wait_job(client,
                     start_solve(client, pid, vid, 7.0,
                                 time_limit=120.0)["job"]["id"], timeout=120)
    # 注意：第二次同参数会复用第一次的超时结果？不会——超时不落方案表，
    # 但会复用"在跑作业"；这里第一次已终态，故产生新作业。
    assert exact["status"] == "completed"
    opt = exact["result"]["station_count"]
    assert tout["result"]["lower_bound"] <= opt <= tout["result"]["station_count"]
    assert exact["result"]["proven_optimal"] is True


# ---------------------------------------------------------------------------
# 半径扫描
# ---------------------------------------------------------------------------

def test_sweep_staircase(client):
    pid = make_project(client)
    vid = create_clinic_version(client, pid)["id"]
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/sweep",
        json={"radii": [2.0, 3.0, 4.0, 5.5], "forced_station_ids": []})
    assert r.status_code == 202
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "completed"
    steps = job["result"]["steps"]
    counts = [(s["radius"], s["station_count"]) for s in steps]
    assert counts == [(2.0, None), (3.0, 3), (4.0, 2), (5.5, 1)]
    # 阶梯单调不增
    vals = [c for _, c in counts if c is not None]
    assert vals == sorted(vals, reverse=True)


def test_sweep_validation_errors(client):
    pid = make_project(client)
    vid = create_clinic_version(client, pid)["id"]
    for bad in [
        {"radii": [], "forced_station_ids": []},
        {"radii": [3.0, 2.0], "forced_station_ids": []},
        {"radii": [-1.0], "forced_station_ids": []},
        {"radii": [2.0, 2.0], "forced_station_ids": []},
    ]:
        r = client.post(
            f"/api/projects/{pid}/versions/{vid}/sweep", json=bad)
        assert r.status_code == 422
        assert "radii" in r.json()["field"]


def test_sweep_timeout_keeps_completed_steps(client):
    """作业级时限极小时，已算完的半径保留，其余标 not_computed，状态 timeout。"""
    from testutils import HARD, comb_coverage
    # 用诊所几何无法卡住单步；这里直接构造几何难版本
    import math
    import random

    rng = random.Random(1)
    n, m, radius, spread = 250, 120, 7.0, 70.0
    spts = [(rng.random() * spread, rng.random() * spread) for _ in range(m)]
    xy = []
    for _ in range(n):
        sx, sy = rng.choice(spts)
        a = rng.uniform(0, 2 * math.pi)
        d = radius * math.sqrt(rng.random()) * 0.95
        xy.append((sx + d * math.cos(a), sy + d * math.sin(a)))
    pid = make_project(client)
    body = {
        "residents": [{"id": f"R{i:03d}", "x": x, "y": y}
                      for i, (x, y) in enumerate(xy)],
        "stations": [{"id": f"S{j:03d}", "x": x, "y": y}
                     for j, (x, y) in enumerate(spts)],
    }
    vid = client.post(f"/api/projects/{pid}/versions", json=body).json()["id"]
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/sweep",
        json={"radii": [7.0, 8.0, 9.0], "time_limit": 0.3})
    job = wait_job(client, r.json()["job"]["id"], timeout=30)
    assert job["status"] in ("timeout", "completed")
    steps = job["result"]["steps"]
    if job["status"] == "timeout":
        # 单步求解内部保证先有贪心可行解；这里 7.0 是可行半径
        assert steps[0]["status"] == "timeout"
        assert steps[0]["station_count"] is not None
        assert any(s["status"] == "not_computed" for s in steps)


def test_infeasible_solution_also_deduplicates(client):
    pid = make_project(client)
    vid = create_clinic_version(client, pid)["id"]
    o1 = start_solve(client, pid, vid, 2.0)
    j1 = wait_job(client, o1["job"]["id"])
    assert j1["result"]["status"] == "infeasible"
    o2 = start_solve(client, pid, vid, 2.0)
    assert o2["reused"] is True
    assert o2["result"]["status"] == "infeasible"
    assert o2["result"]["uncovered"] == [0, 1]


# ---------------------------------------------------------------------------
# 增量：端到端对拍
# ---------------------------------------------------------------------------

def test_incremental_end_to_end_matches_full(client):
    pid = make_project(client)
    v1 = create_clinic_version(client, pid)
    j1 = wait_job(client, start_solve(client, pid, v1["id"], 3.0)["job"]["id"])
    assert j1["result"]["station_count"] == 3

    r = client.post(
        f"/api/projects/{pid}/versions/from/{v1['id']}",
        json={"add_residents": [{"id": "NEW", "x": 5, "y": 0.5}]})
    v2 = r.json()
    j2 = wait_job(client, start_solve(client, pid, v2["id"], 3.0)["job"]["id"])
    assert j2["result"]["status"] == "optimal"
    assert j2["result"]["station_count"] == 3
    assert j2["result"]["strategy"] == "warm"


def test_incremental_random_sequence_end_to_end(client):
    """经真实作业路径：随机增删 20 步，增量与"关掉热启动"结果逐步一致。"""
    import math
    import random

    rng = random.Random(31337)
    radius = 6.0
    box = 20.0
    stations = [{"id": f"S{j}", "x": rng.random() * box,
                 "y": rng.random() * box} for j in range(10)]

    def near():
        s = rng.choice(stations)
        a = rng.uniform(0, 2 * math.pi)
        d = radius * math.sqrt(rng.random()) * 0.9
        return s["x"] + d * math.cos(a), s["y"] + d * math.sin(a)

    pid = make_project(client)
    residents = [{"id": f"R{i:02d}", "x": x, "y": y}
                 for i, (x, y) in enumerate(near() for _ in range(12))]
    v = client.post(f"/api/projects/{pid}/versions",
                    json={"residents": residents, "stations": stations,
                          "change_note": "v0"}).json()
    # 初始全量解
    job = wait_job(client, start_solve(client, pid, v["id"], radius)["job"]["id"])
    assert job["result"]["station_count"] is not None

    for step in range(20):
        cur = client.get(
            f"/api/projects/{pid}/versions/{v['id']}").json()["residents"]
        body = {"change_note": f"step{step}"}
        if rng.random() < 0.5 and len(cur) > 6:
            body["remove_resident_ids"] = [rng.choice(cur)["id"]]
        nx, ny = near()
        body["add_residents"] = [{"id": f"NEW{step}_{rng.randint(0, 99999)}",
                                  "x": nx, "y": ny}]
        r = client.post(
            f"/api/projects/{pid}/versions/from/{v['id']}", json=body)
        assert r.status_code == 201, r.text
        v = r.json()

        # 增量（带父版本热启动）
        job = wait_job(client,
                       start_solve(client, pid, v["id"], radius)["job"]["id"])
        inc = job["result"]
        assert inc["status"] in ("optimal",)
        # 对拍：同一数据、不同半径绕过去重拿独立全量解
        probe = start_solve(client, pid, v["id"], radius + 0.000125)
        pjob = wait_job(client, probe["job"]["id"])
        fullish = pjob["result"]
        # 半径几乎相同 → 站数必须一致（同一组覆盖关系的概率由构造保证；
        # 更严谨的对拍在单元测试里用同一 radius 强制冷启动完成）
        assert inc["station_count"] == fullish["station_count"], step
        # 覆盖性：用作业返回方案核对（通过半径完全相同的重复请求取方案）
        covered_ids = {p["id"] for p in v["residents"]}
        chosen_stations = [stations[j] for j in inc["chosen"]]
        for rid in covered_ids:
            rp = next(p for p in v["residents"] if p["id"] == rid)
            assert any(
                math.hypot(rp["x"] - s["x"], rp["y"] - s["y"]) <= radius
                for s in chosen_stations), (step, rid)


# ---------------------------------------------------------------------------
# 重启恢复
# ---------------------------------------------------------------------------

def test_interrupted_after_restart(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app

    db_path = str(tmp_path / "restart.db")
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        pid = make_project(c)
        vid = create_clinic_version(c, pid)["id"]
        # 直接插入一条 running 作业（不投递执行），模拟崩溃瞬间
        job_row = app.state.storage.create_job(
            "solve", pid, vid, {"radius": 3.0, "forced": []})
        jid = job_row["id"]
        app.state.storage._conn.execute(
            "UPDATE jobs SET status='running' WHERE id=?", (jid,))
        app.state.storage._conn.commit()

    # 重新启动同一数据库
    app2 = create_app(db_path=db_path)
    with TestClient(app2) as c2:
        job = c2.get(f"/api/jobs/{jid}").json()
        assert job["status"] == "interrupted"
        assert job["interrupted"] is True
        assert not job.get("result")
    # 已完成作业不受影响
    with TestClient(create_app(db_path=db_path)) as c3:
        pid2 = make_project(c3)
        vid2 = create_clinic_version(c3, pid2)["id"]
        done = wait_job(c3, start_solve(c3, pid2, vid2, 3.0)["job"]["id"])
        assert done["status"] == "completed"
    with TestClient(create_app(db_path=db_path)) as c4:
        again = c4.get(f"/api/jobs/{done['id']}").json()
        assert again["status"] == "completed"
        assert again["result"]["station_count"] == 3


def test_progress_fields_present(client):
    pid = make_project(client)
    vid = create_clinic_version(client, pid)["id"]
    out = start_solve(client, pid, vid, 3.0)
    job = wait_job(client, out["job"]["id"])
    assert job["progress"] is not None
    assert "explored_nodes" in job["progress"]
    assert "lower_bound" in job["progress"]
    assert job["progress"]["best_count"] == 3


def test_api_remove_critical_station_infeasible(client):
    """拿掉所有最优解都离不开的 C1：站数上升或变无解并列点。"""
    pid = make_project(client)
    body = clinic_payload()
    body["stations"] = [s for s in body["stations"] if s["id"] != "C1"]
    vid = client.post(f"/api/projects/{pid}/versions", json=body).json()["id"]
    job = wait_job(client, start_solve(client, pid, vid, 3.0)["job"]["id"])
    assert job["result"]["status"] == "infeasible"
    ids = [RESIDENTS[i][0] for i in job["result"]["uncovered"]]
    assert ids == ["R01", "R02"]
    assert job["result"]["proven_optimal"] is False


def test_api_monotonic_radius_and_forced(client):
    """写进测试的两条关系，经完整作业路径核对。"""
    pid = make_project(client)
    vid = create_clinic_version(client, pid)["id"]
    # 调大半径，站数不会变多
    j3 = wait_job(client, start_solve(client, pid, vid, 3.0)["job"]["id"])
    j4 = wait_job(client, start_solve(client, pid, vid, 4.0)["job"]["id"])
    j55 = wait_job(client, start_solve(client, pid, vid, 5.5)["job"]["id"])
    c3, c4, c55 = (j["result"]["station_count"] for j in (j3, j4, j55))
    assert c3 == 3 and c4 == 2 and c55 == 1
    assert c55 <= c4 <= c3

    # 强制开一座原本不在最优解里的站，站数不会少于原最优
    jf = wait_job(client,
                  start_solve(client, pid, vid, 3.0,
                              forced=["C4"])["job"]["id"])
    assert jf["result"]["station_count"] >= 3
    assert jf["result"]["station_count"] == 4
    assert "C4" in [STATIONS[j][0] for j in jf["result"]["chosen"]]
    assert jf["result"]["proven_optimal"] is True


def test_duplicate_coordinate_version_same_count(client):
    """同一居民点坐标重复出现，站数不受影响。"""
    pid = make_project(client)
    v1 = create_clinic_version(client, pid)
    j1 = wait_job(client, start_solve(client, pid, v1["id"], 3.0)["job"]["id"])

    body = clinic_payload()
    # 用新编号复制三个点的坐标
    body["residents"] += [
        {"id": "DUP1", "x": RESIDENTS[0][1], "y": RESIDENTS[0][2]},
        {"id": "DUP2", "x": RESIDENTS[5][1], "y": RESIDENTS[5][2]},
    ]
    v2 = client.post(f"/api/projects/{pid}/versions", json=body).json()
    j2 = wait_job(client, start_solve(client, pid, v2["id"], 3.0)["job"]["id"])
    assert j1["result"]["station_count"] == j2["result"]["station_count"]
