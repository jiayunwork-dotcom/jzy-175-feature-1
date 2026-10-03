"""分期建设计划 HTTP 测试。

覆盖：创建/取回/幂等、确认顺序与乐观锁、并发一方静默覆盖防护、
调预算重排锁定前缀、必开站、不可行诚实报告、旧库接管（无 weight 字段）。
"""

import json
import threading

from conftest import make_project, start_solve, wait_job

from app.examples import (
    EXPECTED_PHASED_111_CUM_POP,
    EXPECTED_PHASED_111_NEW,
    EXPECTED_PHASED_21_CUM_POP,
    EXPECTED_PHASED_21_NEW,
    EXPECTED_PHASED_11_BLOCKED_PERIOD,
    EXPECTED_PHASED_11_SHORTAGE,
    EXPECTED_TOTAL_WEIGHT,
    RESIDENTS,
    RESIDENT_WEIGHTS,
    STATIONS,
)


def _weighted_payload():
    return {
        "residents": [{"id": i, "x": x, "y": y, "weight": RESIDENT_WEIGHTS[i]}
                      for i, x, y in RESIDENTS],
        "stations": [{"id": i, "x": x, "y": y} for i, x, y in STATIONS],
    }


def _version(client, pid, payload=None):
    r = client.post(f"/api/projects/{pid}/versions",
                    json=payload or _weighted_payload())
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_plan(client, pid, vid, budgets, radius=3.0, forced=None,
                 time_limit=None, expected=201):
    body = {"radius": radius, "budgets": budgets,
            "forced_station_ids": forced or []}
    if time_limit is not None:
        body["time_limit"] = time_limit
    r = client.post(f"/api/projects/{pid}/versions/{vid}/plans", json=body)
    assert r.status_code == expected, r.text
    return r.json()


def _get_plan(client, pid, vid, plan_id):
    r = client.get(f"/api/projects/{pid}/versions/{vid}/plans/{plan_id}")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 诊所带权基准：经完整 HTTP 路径
# ---------------------------------------------------------------------------

def test_create_weighted_clinic_plan_111(client):
    pid = make_project(client)
    vid = _version(client, pid)
    out = _create_plan(client, pid, vid, [1, 1, 1])
    assert out["reused"] is False and out["created"] is True
    p = out["plan"]
    assert p["status"] == "active"
    assert p["revision"] == 0
    assert p["objective"] == "lex_min_stations_then_lex_max_covered_population"
    plan = p["plan"]
    assert plan["proven_optimal"] is True
    new = [tuple(ph["new_station_ids"]) for ph in plan["phases"]]
    assert tuple(new) == EXPECTED_PHASED_111_NEW
    pops = [ph["cumulative_population"] for ph in plan["phases"]]
    assert tuple(pops) == EXPECTED_PHASED_111_CUM_POP
    assert plan["total_station_count"] == 3
    assert plan["unphased_min_station_count"] == 3
    assert plan["extra_over_unphased"] == 0
    assert plan["total_population"] == EXPECTED_TOTAL_WEIGHT


def test_create_weighted_clinic_plan_21(client):
    pid = make_project(client)
    vid = _version(client, pid)
    p = _create_plan(client, pid, vid, [2, 1])["plan"]["plan"]
    new = [tuple(ph["new_station_ids"]) for ph in p["phases"]]
    assert tuple(new) == EXPECTED_PHASED_21_NEW
    pops = [ph["cumulative_population"] for ph in p["phases"]]
    assert tuple(pops) == EXPECTED_PHASED_21_CUM_POP


def test_plan_monotone_population_and_nesting(client):
    """通用关系：累计人口逐期不减；每期集合包含上一期。"""
    pid = make_project(client)
    vid = _version(client, pid)
    p = _create_plan(client, pid, vid, [1, 2, 2])["plan"]["plan"]
    pops = [ph["cumulative_population"] for ph in p["phases"]]
    assert pops == sorted(pops)
    prev = set()
    for t, ph in enumerate(p["phases"]):
        cur = set(ph["cumulative_station_ids"])
        assert prev <= cur
        assert len(ph["new_station_ids"]) <= p["budgets"][t]
        prev = cur
    assert abs(pops[-1] - EXPECTED_TOTAL_WEIGHT) < 1e-9


# ---------------------------------------------------------------------------
# 幂等：同版本同参数不产生重复记录；不同参数分开
# ---------------------------------------------------------------------------

def test_duplicate_plan_params_return_same_record(client):
    pid = make_project(client)
    vid = _version(client, pid)
    a = _create_plan(client, pid, vid, [1, 1, 1])
    b = _create_plan(client, pid, vid, [1, 1, 1], expected=200)
    assert b["reused"] is True
    assert b["plan"]["id"] == a["plan"]["id"]
    # 预算不同是另一份计划
    c = _create_plan(client, pid, vid, [2, 1])
    assert c["plan"]["id"] != a["plan"]["id"]
    # 必开集合不同也是另一份计划；同必开再请求则幂等
    f1 = _create_plan(client, pid, vid, [2, 2], forced=["C4"])
    f2 = _create_plan(client, pid, vid, [2, 2], forced=["C4"],
                      expected=200)
    assert f2["plan"]["id"] == f1["plan"]["id"]
    # 列表能取回全部计划
    lst = client.get(f"/api/projects/{pid}/versions/{vid}/plans").json()
    assert {x["id"] for x in lst} == {
        a["plan"]["id"], c["plan"]["id"], f1["plan"]["id"]}


# ---------------------------------------------------------------------------
# 校验错误
# ---------------------------------------------------------------------------

def test_plan_validation_errors(client):
    pid = make_project(client)
    vid = _version(client, pid)
    r = client.post(f"/api/projects/{pid}/versions/{vid}/plans",
                    json={"radius": 0, "budgets": [1, 1]})
    assert r.status_code == 400 and r.json()["field"] == "radius"

    r = client.post(f"/api/projects/{pid}/versions/{vid}/plans",
                    json={"radius": 3.0, "budgets": []})
    assert r.status_code == 422 and "budgets" in r.json()["field"]

    r = client.post(f"/api/projects/{pid}/versions/{vid}/plans",
                    json={"radius": 3.0, "budgets": [1, -1]})
    assert r.status_code == 422

    r = client.post(f"/api/projects/{pid}/versions/{vid}/plans",
                    json={"radius": 3.0, "budgets": [1, 1],
                          "forced_station_ids": ["GHOST"]})
    assert r.status_code == 404
    assert r.json()["field"] == "forced_station_ids"


# ---------------------------------------------------------------------------
# 不可行：预算收不了口 / 全开够不着 —— 都不许报成功
# ---------------------------------------------------------------------------

def test_budget_infeasible_reported_honestly(client):
    pid = make_project(client)
    vid = _version(client, pid)
    out = _create_plan(client, pid, vid, [1, 1])
    p = out["plan"]
    assert p["status"] == "infeasible"
    body = p["plan"]
    assert body["feasible"] is False
    assert body["proven_optimal"] is False
    assert body["blocked_at_period"] == EXPECTED_PHASED_11_BLOCKED_PERIOD
    assert body["budget_shortage"] == EXPECTED_PHASED_11_SHORTAGE
    assert body["total_station_count"] is None
    assert body["infeasible_reason"]
    # 不可行计划不能确认
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{p['id']}/confirm",
        json={"revision": 0, "period": 0})
    assert r.status_code == 409


def test_unreachable_infeasible_lists_points(client):
    pid = make_project(client)
    vid = _version(client, pid)
    p = _create_plan(client, pid, vid, [3, 3], radius=2.0)["plan"]
    assert p["status"] == "infeasible"
    body = p["plan"]
    assert body["uncovered_resident_ids"] == ["R01", "R02"]
    assert body["blocked_at_period"] is None


# ---------------------------------------------------------------------------
# 确认流程：逐期、顺序、状态推进
# ---------------------------------------------------------------------------

def test_confirm_phases_in_order_and_completion(client):
    pid = make_project(client)
    vid = _version(client, pid)
    plan = _create_plan(client, pid, vid, [1, 1, 1])["plan"]
    pid_plan = plan["id"]

    # 不能跳期
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/confirm",
        json={"revision": 0, "period": 1})
    assert r.status_code == 409
    assert r.json()["field"] == "period"

    rev = 0
    for k, expected_group in enumerate(EXPECTED_PHASED_111_NEW):
        r = client.post(
            f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/confirm",
            json={"revision": rev, "period": k})
        assert r.status_code == 200, r.text
        updated = r.json()["plan"]
        assert updated["confirmed_periods"] == k + 1
        assert updated["locked_periods"][k] == list(expected_group)
        rev = updated["revision"]
        assert updated["status"] == ("completed" if k == 2 else "active")

    # 全部确认后再确认 / 重排都被拒
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/confirm",
        json={"revision": rev, "period": 0})
    assert r.status_code == 409
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/replan",
        json={"revision": rev, "budgets": [1, 1, 1]})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# 并发 / 乐观锁：一方确认、另一方拿旧 revision 重排 → 只有一个成功
# ---------------------------------------------------------------------------

def test_concurrent_confirm_and_replan_one_wins(client):
    pid = make_project(client)
    vid = _version(client, pid)
    plan = _create_plan(client, pid, vid, [1, 1, 1])["plan"]
    pid_plan = plan["id"]

    barrier = threading.Barrier(2)
    results = {}

    def do_confirm():
        barrier.wait()
        r = client.post(
            f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/confirm",
            json={"revision": 0, "period": 0})
        results["confirm"] = (r.status_code, r.json())

    def do_replan():
        barrier.wait()
        r = client.post(
            f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/replan",
            json={"revision": 0, "budgets": [2, 1, 1]})
        results["replan"] = (r.status_code, r.json())

    t1 = threading.Thread(target=do_confirm)
    t2 = threading.Thread(target=do_replan)
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)

    codes = {k: v[0] for k, v in results.items()}
    assert 200 in codes.values()
    assert 409 in codes.values()
    loser = "replan" if codes["confirm"] == 200 else "confirm"
    err = results[loser][1]
    assert err["field"] == "revision"
    assert "过时" in err["error"]
    assert err["details"]["current_revision"] >= 1

    final = _get_plan(client, pid, vid, pid_plan)
    # 赢的操作只推进了一次 revision，输的没有任何落库
    assert final["revision"] == 1
    if codes["confirm"] == 200:
        # 确认赢了：第一期锁死为重排前的 C2，预算仍是原 [1,1,1]
        assert final["budgets"] == [1, 1, 1]
        assert final["confirmed_periods"] == 1
        assert final["locked_periods"] == [["C2"]]
    else:
        # 重排赢了：新预算生效，仍未确认任何一期
        assert final["budgets"] == [2, 1, 1]
        assert final["confirmed_periods"] == 0
    # 无论谁赢，计划仍合法且未被"确认 + 重排"双重覆盖
    assert final["plan"]["total_station_count"] == 3
    assert final["status"] in ("active",)


def test_stale_revision_never_silently_overwrites(client):
    pid = make_project(client)
    vid = _version(client, pid)
    plan = _create_plan(client, pid, vid, [1, 1, 1])["plan"]
    pid_plan = plan["id"]
    # 成功确认第一期
    ok = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/confirm",
        json={"revision": 0, "period": 0})
    assert ok.status_code == 200
    # 拿着旧 revision 想确认第一期 → 明确冲突，确认状态未被改动
    stale = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/confirm",
        json={"revision": 0, "period": 0})
    assert stale.status_code == 409
    assert stale.json()["field"] == "revision"
    got = _get_plan(client, pid, vid, pid_plan)
    assert got["confirmed_periods"] == 1
    assert got["locked_periods"] == [["C2"]]


# ---------------------------------------------------------------------------
# 调整预算后重排：已确认期锁死；只许放宽已确认期预算
# ---------------------------------------------------------------------------

def test_replan_keeps_confirmed_period_and_changes_tail(client):
    pid = make_project(client)
    vid = _version(client, pid)
    plan = _create_plan(client, pid, vid, [1, 1, 1])["plan"]
    pid_plan = plan["id"]

    ok = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/confirm",
        json={"revision": 0, "period": 0})
    rev = ok.json()["plan"]["revision"]

    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/replan",
        json={"revision": rev, "budgets": [1, 2, 2]})
    assert r.status_code == 200, r.text
    p = r.json()["plan"]
    # 第一期锁死不变
    assert p["locked_periods"] == [["C2"]]
    assert p["confirmed_periods"] == 1
    assert p["plan"]["phases"][0]["new_station_ids"] == ["C2"]
    # 全部人口最后一期仍全覆盖；站数仍是 3，不多建
    assert p["plan"]["total_station_count"] == 3
    assert p["plan"]["extra_over_unphased"] == 0
    assert p["revision"] == rev + 1


def test_replan_shrinking_confirmed_budget_rejected(client):
    pid = make_project(client)
    vid = _version(client, pid)
    plan = _create_plan(client, pid, vid, [1, 1, 1])["plan"]
    pid_plan = plan["id"]
    ok = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/confirm",
        json={"revision": 0, "period": 0})
    rev = ok.json()["plan"]["revision"]
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{pid_plan}/replan",
        json={"revision": rev, "budgets": [0, 2, 2]})
    assert r.status_code == 409
    assert r.json()["field"] == "budgets.0"
    # 计划未被改动
    got = _get_plan(client, pid, vid, pid_plan)
    assert got["budgets"] == [1, 1, 1]
    assert got["revision"] == rev


def test_replan_period_count_cannot_change(client):
    pid = make_project(client)
    vid = _version(client, pid)
    plan = _create_plan(client, pid, vid, [1, 1, 1])["plan"]
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{plan['id']}/replan",
        json={"revision": 0, "budgets": [2, 2]})
    assert r.status_code == 400
    assert r.json()["field"] == "budgets"


def test_replan_to_infeasible_budget_marks_plan_infeasible(client):
    pid = make_project(client)
    vid = _version(client, pid)
    # 第 0 期建 1 座后，把后面两期预算压到合计 1（还缺 2 座才能收口）
    plan = _create_plan(client, pid, vid, [1, 1, 1])["plan"]
    ok = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{plan['id']}/confirm",
        json={"revision": 0, "period": 0})
    rev = ok.json()["plan"]["revision"]
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{plan['id']}/replan",
        json={"revision": rev, "budgets": [1, 0, 1]})
    assert r.status_code == 200, r.text
    p = r.json()["plan"]
    assert p["status"] == "infeasible"
    assert p["plan"]["feasible"] is False
    assert p["plan"]["blocked_at_period"] is not None
    # 已确认的第一期仍原样保留
    assert p["locked_periods"] == [["C2"]]
    # 放宽预算再重排：infeasible 状态允许自救回 active
    r2 = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{plan['id']}/replan",
        json={"revision": p["revision"], "budgets": [1, 2, 2]})
    assert r2.status_code == 200, r2.text
    p2 = r2.json()["plan"]
    assert p2["status"] == "active"
    assert p2["plan"]["feasible"] is True
    assert p2["locked_periods"] == [["C2"]]


def test_replan_into_existing_plans_budgets_conflicts(client):
    """把 A 计划重排成 B 计划已有的参数 → 409，不做覆盖/重复记录。"""
    pid = make_project(client)
    vid = _version(client, pid)
    a = _create_plan(client, pid, vid, [1, 1, 1])["plan"]
    b = _create_plan(client, pid, vid, [2, 1, 1])["plan"]
    assert a["id"] != b["id"]
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans/{a['id']}/replan",
        json={"revision": 0, "budgets": [2, 1, 1]})
    assert r.status_code == 409
    assert r.json()["field"] == "budgets"
    assert r.json()["details"]["existing_plan_id"] == b["id"]
    # A 计划没被动过
    got = _get_plan(client, pid, vid, a["id"])
    assert got["budgets"] == [1, 1, 1] and got["revision"] == 0


# ---------------------------------------------------------------------------
# 必开站
# ---------------------------------------------------------------------------

def test_forced_station_appears_in_plan(client):
    pid = make_project(client)
    vid = _version(client, pid)
    p = _create_plan(client, pid, vid, [2, 2], forced=["C4"])["plan"]["plan"]
    assert p["feasible"]
    assert "C4" in p["phases"][-1]["cumulative_station_ids"]
    assert p["total_station_count"] == 4


# ---------------------------------------------------------------------------
# 权重默认 1：不传 weight 的版本照样能出计划，人口按点数算
# ---------------------------------------------------------------------------

def test_missing_weight_defaults_to_one(client):
    pid = make_project(client)
    payload = {
        "residents": [{"id": i, "x": x, "y": y} for i, x, y in RESIDENTS],
        "stations": [{"id": i, "x": x, "y": y} for i, x, y in STATIONS],
    }
    vid = _version(client, pid, payload)
    p = _create_plan(client, pid, vid, [1, 1, 1])["plan"]["plan"]
    assert p["total_population"] == 12
    # 取回版本时居民点带 weight=1
    got = client.get(f"/api/projects/{pid}/versions/{vid}").json()
    assert all(r["weight"] == 1.0 for r in got["residents"])


def test_invalid_weight_rejected(client):
    pid = make_project(client)
    body = _weighted_payload()
    body["residents"][0]["weight"] = 0
    r = client.post(f"/api/projects/{pid}/versions", json=body)
    assert r.status_code == 422
    assert "weight" in r.json()["field"]


# ---------------------------------------------------------------------------
# 旧服务数据库直接接管：无 plans 表、居民点无 weight 字段
# ---------------------------------------------------------------------------

def test_service_takes_over_legacy_database(tmp_path):
    import sqlite3

    from fastapi.testclient import TestClient
    from app.main import create_app

    db_path = str(tmp_path / "legacy.db")
    # 用最小旧结构手工建库（与 storage 旧版一致：无 plans 表、无 weight）
    conn = sqlite3.connect(db_path)
    conn.executescript("""
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
        created_at REAL NOT NULL);
    CREATE TABLE versions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
        version_no INTEGER NOT NULL, parent_version_id TEXT,
        change_note TEXT, residents TEXT NOT NULL, stations TEXT NOT NULL,
        created_at REAL NOT NULL, UNIQUE(project_id, version_no));
    CREATE TABLE solutions (id TEXT PRIMARY KEY, version_id TEXT NOT NULL,
        radius REAL NOT NULL, forced TEXT NOT NULL, result_json TEXT NOT NULL,
        strategy TEXT NOT NULL DEFAULT 'full', updated_at REAL NOT NULL,
        UNIQUE(version_id, radius, forced));
    CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL,
        project_id TEXT NOT NULL, version_id TEXT NOT NULL,
        params_json TEXT NOT NULL, status TEXT NOT NULL, progress_json TEXT,
        result_json TEXT, error TEXT, created_at REAL NOT NULL,
        updated_at REAL NOT NULL, started_at REAL, finished_at REAL);
    """)
    conn.execute("INSERT INTO projects VALUES ('P1','旧项目',1.0)")
    residents = [{"id": i, "x": x, "y": y} for i, x, y in RESIDENTS]
    stations = [{"id": i, "x": x, "y": y} for i, x, y in STATIONS]
    conn.execute(
        "INSERT INTO versions VALUES "
        "('V1','P1',1,NULL,'旧版',?,?,2.0)",
        (json.dumps(residents, ensure_ascii=False),
         json.dumps(stations, ensure_ascii=False)))
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path)) as c:
        # 旧版本照常读取，居民点按权重 1
        v = c.get("/api/projects/P1/versions/V1").json()
        assert len(v["residents"]) == 12
        assert all(r["weight"] == 1.0 for r in v["residents"])
        # 旧求解结果照常（先跑一次落方案）
        job = wait_job(c, start_solve(c, "P1", "V1", 3.0)["job"]["id"])
        assert job["result"]["station_count"] == 3
        again = start_solve(c, "P1", "V1", 3.0)
        assert again["reused"] is True
        # 新能力在旧库上直接可用（迁移补出了 plans 表）
        out = c.post("/api/projects/P1/versions/V1/plans",
                     json={"radius": 3.0, "budgets": [1, 1, 1]})
        assert out.status_code in (200, 201), out.text
        p = out.json()["plan"]
        assert p["plan"]["total_population"] == 12
        assert p["plan"]["total_station_count"] == 3

    # 再重启一次：plans 表与记录仍在
    with TestClient(create_app(db_path=db_path)) as c2:
        lst = c2.get("/api/projects/P1/versions/V1/plans").json()
        assert len(lst) == 1
        assert lst[0]["plan"]["total_station_count"] == 3
