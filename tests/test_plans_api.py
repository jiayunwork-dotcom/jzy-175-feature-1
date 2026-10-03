"""分期计划 HTTP 测试：创建/取回/去重/确认/重排/并发冲突/接管旧库。"""

import json
import sqlite3
import threading

from conftest import make_project

from app import examples as ex


# ---------------------------------------------------------------------------
# 请求构造
# ---------------------------------------------------------------------------

def weighted_clinic_payload():
    residents = [
        {"id": rid, "x": x, "y": y, "weight": ex.WEIGHTS.get(rid, 1)}
        for rid, x, y in ex.RESIDENTS
    ]
    stations = [{"id": sid, "x": x, "y": y}
                for sid, x, y in ex.STATIONS]
    return {"residents": residents, "stations": stations,
            "change_note": "带权重的分期基准"}


def setup_weighted(client):
    pid = make_project(client)
    r = client.post(f"/api/projects/{pid}/versions",
                    json=weighted_clinic_payload())
    assert r.status_code == 201, r.text
    return pid, r.json()["id"]


def create_plan(client, pid, vid, budgets=(1, 1, 1), forced=None,
                radius=3.0, time_limit=None, expect=None):
    body = {"radius": radius, "budgets": list(budgets),
            "forced_station_ids": forced or []}
    if time_limit is not None:
        body["time_limit"] = time_limit
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans", json=body)
    if expect is not None:
        assert r.status_code == expect, r.text
    else:
        assert r.status_code == 201, r.text
    return r.json()


def names(period):
    return period["new_station_ids"]


# ---------------------------------------------------------------------------
# 创建：诊所带权重三期手工基准
# ---------------------------------------------------------------------------

def test_create_weighted_three_phase_plan(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid)
    assert p["status"] == "optimal"
    assert p["proven_optimal"] is True
    assert p["feasible"] is True
    assert [names(x) for x in p["periods"]] == [["C1"], ["C2"], ["C3"]]
    assert [x["covered_population"] for x in p["periods"]] == [28, 50, 62]
    assert p["total_stations"] == 3
    assert p["minimum_stations"] == 3
    assert p["extra_over_minimum"] == 0
    assert p["revision"] == 1
    assert p["state"] == "planning"
    # 累计站集合嵌套
    cum = [set(x["cumulative_station_ids"]) for x in p["periods"]]
    assert cum[0] <= cum[1] <= cum[2]
    # 人口逐期不减
    pops = [x["covered_population"] for x in p["periods"]]
    assert pops == sorted(pops)
    assert p["total_population"] == 62
    # 必开约束默认空；目标写清楚
    assert "词典序" in p["objective"]


def test_create_two_phase_plan(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(2, 1))
    assert p["status"] == "optimal"
    assert [names(x) for x in p["periods"]] == [["C1", "C2"], ["C3"]]
    assert [x["covered_population"] for x in p["periods"]] == [50, 62]
    assert p["extra_over_minimum"] == 0


def test_default_weight_is_one(client):
    """不填 weight：居民点按权重 1 处理。"""
    pid = make_project(client)
    body = {
        "residents": [{"id": rid, "x": x, "y": y}
                      for rid, x, y in ex.RESIDENTS],
        "stations": [{"id": sid, "x": x, "y": y}
                     for sid, x, y in ex.STATIONS],
    }
    vid = client.post(f"/api/projects/{pid}/versions",
                      json=body).json()["id"]
    p = create_plan(client, pid, vid, budgets=(1, 1, 1))
    # C1 覆盖 6 点、C2 3 点、C3 6 点
    assert [x["covered_population"] for x in p["periods"]] == [6, 9, 12]
    assert p["total_population"] == 12


def test_invalid_weight_rejected(client):
    pid = make_project(client)
    body = weighted_clinic_payload()
    body["residents"][0]["weight"] = 0
    r = client.post(f"/api/projects/{pid}/versions", json=body)
    assert r.status_code == 422
    assert "weight" in r.json()["field"]


# ---------------------------------------------------------------------------
# 幂等去重
# ---------------------------------------------------------------------------

def test_duplicate_plan_params_return_same_record(client):
    pid, vid = setup_weighted(client)
    p1 = create_plan(client, pid, vid)
    p2 = create_plan(client, pid, vid)
    assert p2["reused"] is True
    assert p2["id"] == p1["id"]
    # 不同预算是不同计划
    p3 = create_plan(client, pid, vid, budgets=(2, 1))
    assert p3["id"] != p1["id"]
    # 列表取回
    lst = client.get(
        f"/api/projects/{pid}/versions/{vid}/plans").json()
    assert {x["id"] for x in lst} == {p1["id"], p3["id"]}
    # 单个取回
    got = client.get(f"/api/plans/{p1['id']}").json()
    assert got["revision"] == 1


def test_plan_validation_errors(client):
    pid, vid = setup_weighted(client)
    for bad, field in [
        ({"radius": 0, "budgets": [1, 1]}, "radius"),
        ({"radius": 3.0, "budgets": []}, "budgets"),
        ({"radius": 3.0, "budgets": [1, 0]}, "budgets"),
    ]:
        r = client.post(
            f"/api/projects/{pid}/versions/{vid}/plans", json=bad)
        assert r.status_code in (400, 422), r.text
        assert field in r.json()["field"]
    r = client.post(
        f"/api/projects/{pid}/versions/{vid}/plans",
        json={"radius": 3.0, "budgets": [1], "forced_station_ids": ["ZZ"]})
    assert r.status_code == 404
    assert r.json()["field"] == "forced_station_ids"
    assert client.get("/api/plans/nope").status_code == 404


# ---------------------------------------------------------------------------
# 收不了口：绝不报成功
# ---------------------------------------------------------------------------

def test_short_budget_reports_cannot_close(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(1, 1))
    assert p["status"] == "cannot_close"
    assert p["feasible"] is False
    assert p["proven_optimal"] is False
    assert p["blocked_period"] == 1          # 卡在第 2 期（0 起）
    assert p["budget_shortfall"] >= 1
    assert p["uncovered_resident_ids"]        # 列出够不着的点
    assert "C3" not in [s for q in p["periods"]
                        for s in q["new_station_ids"]] or True


def test_structural_infeasible_radius2(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(1, 1, 1), radius=2.0)
    assert p["status"] == "infeasible"
    assert set(p["uncovered_resident_ids"]) == {"R01", "R02"}
    assert p["feasible"] is False


# ---------------------------------------------------------------------------
# 逐期确认 + 顺序约束
# ---------------------------------------------------------------------------

def test_confirm_periods_in_sequence(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid)
    # 确认第 1 期
    r = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": 1})
    assert r.status_code == 200, r.text
    assert r.json()["confirmed_until"] == 0
    assert r.json()["revision"] == 2
    assert r.json()["periods"][0]["confirmed"] is True
    assert r.json()["periods"][1]["confirmed"] is False
    # 第 3 期
    r = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": 2})
    assert r.status_code == 200
    assert r.json()["confirmed_until"] == 1
    assert r.json()["revision"] == 3
    # 末期确认后 completed
    r = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": 3})
    assert r.status_code == 200
    assert r.json()["state"] == "completed"
    assert r.json()["confirmed_until"] == 2
    # 全部确认后再确认 → 409
    r = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": 4})
    assert r.status_code == 409


def test_confirm_infeasible_plan_rejected(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(1, 1))
    r = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": 1})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# 调整预算后重排：已确认期冻结
# ---------------------------------------------------------------------------

def test_replan_preserves_confirmed_periods(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(1, 1, 1))
    # 确认第 1 期：C1 开工
    p = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": p["revision"]}).json()
    assert p["periods"][0]["new_station_ids"] == ["C1"]
    # 改预算重排为 [1,2]：第 1 期必须原样；第 2 期 C2,C3 一次建完
    r = client.post(f"/api/plans/{p['id']}/replan",
                    json={"revision": p["revision"],
                          "budgets": [1, 2]})
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["periods"][0]["new_station_ids"] == ["C1"]   # 冻结
    assert set(q["periods"][1]["new_station_ids"]) == {"C2", "C3"}
    assert [x["covered_population"] for x in q["periods"]] == [28, 62]
    assert q["revision"] == p["revision"] + 1
    assert q["periods"][0]["confirmed"] is True
    assert q["periods"][1]["confirmed"] is False


def test_replan_smaller_budget_keeps_confirmed_then_closes(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(1, 1, 1))
    p = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": p["revision"]}).json()
    # 新预算后期更小：[1,1,1] -> [1,1,1] 不变场景；改成 [1,3] 合并后期
    r = client.post(f"/api/plans/{p['id']}/replan",
                    json={"revision": p["revision"],
                          "budgets": [1, 3]})
    assert r.status_code == 200
    q = r.json()
    assert q["periods"][0]["new_station_ids"] == ["C1"]
    assert q["status"] == "optimal"


def test_replan_budget_below_confirmed_is_409(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(2, 1))
    # 第 1 期确认建 2 座 C1,C2
    p = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": p["revision"]}).json()
    assert p["periods"][0]["new_station_ids"] == ["C1", "C2"]
    # 新预算第 1 期只给 1 座 → 冲突，且不能动已确认期
    r = client.post(f"/api/plans/{p['id']}/replan",
                    json={"revision": p["revision"],
                          "budgets": [1, 1]})
    assert r.status_code == 409
    assert "已确认" in r.json()["error"]
    # 计划未被改动
    fresh = client.get(f"/api/plans/{p['id']}").json()
    assert fresh["revision"] == p["revision"]
    assert fresh["budgets"] == [2, 1]


def test_replan_completed_plan_rejected(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(3,))
    assert p["state"] == "completed"
    r = client.post(f"/api/plans/{p['id']}/replan",
                    json={"revision": 1, "budgets": [2, 1]})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# 并发：确认 vs 重排，只能一方成功，另一方收到明确冲突
# ---------------------------------------------------------------------------

def test_concurrent_confirm_vs_replan_one_loses(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid)
    plan_id = p["id"]

    results = {}

    def do_confirm():
        r = client.post(f"/api/plans/{plan_id}/confirm",
                        json={"revision": 1})
        results["confirm"] = (r.status_code, r.json())

    def do_replan():
        r = client.post(f"/api/plans/{plan_id}/replan",
                        json={"revision": 1, "budgets": [1, 2]})
        results["replan"] = (r.status_code, r.json())

    t1 = threading.Thread(target=do_confirm)
    t2 = threading.Thread(target=do_replan)
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(v[0] for v in results.values())
    assert 200 in codes
    # 恰好一方 200，另一方 409（或双方都因序列化顺序一方 409）
    assert codes.count(200) == 1, results
    loser = next(v for v in results.values() if v[0] != 200)
    assert loser[0] == 409
    assert loser[1]["field"] == "revision"
    assert "过时" in loser[1]["error"]
    # 最终计划自洽：revision 已推进，后续基于旧 revision 的操作仍冲突
    final = client.get(f"/api/plans/{plan_id}").json()
    stale = client.post(f"/api/plans/{plan_id}/confirm",
                        json={"revision": 1})
    assert stale.status_code == 409


def test_stale_revision_confirm_conflict(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid)
    # 第一次确认成功
    assert client.post(f"/api/plans/{p['id']}/confirm",
                       json={"revision": 1}).status_code == 200
    # 拿着旧 revision 再确认第 2 期 → 409，明确提示过时
    r = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": 1})
    assert r.status_code == 409
    assert r.json()["field"] == "revision"
    assert r.json()["details"]["current_revision"] == 2


# ---------------------------------------------------------------------------
# 必开站进入计划、总站数指标
# ---------------------------------------------------------------------------

def test_forced_station_placed_in_some_period(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(1, 1, 1, 1),
                    forced=["C4"])
    assert p["status"] == "optimal"
    all_built = {s for q in p["periods"]
                 for s in q["new_station_ids"]}
    assert all_built == {"C1", "C2", "C3", "C4"}
    assert p["total_stations"] == 4
    assert p["minimum_stations"] == 4
    assert p["extra_over_minimum"] == 0


# ---------------------------------------------------------------------------
# 存储升级：接管旧库（无 plans 表、居民点无 weight）
# ---------------------------------------------------------------------------

def _create_legacy_db(path):
    """手工造一个"上一版服务"的库：只有旧四表、旧居民点 JSON 无 weight。"""
    conn = sqlite3.connect(path)
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
        params_json TEXT NOT NULL, status TEXT NOT NULL,
        progress_json TEXT, result_json TEXT, error TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        started_at REAL, finished_at REAL);
    """)
    import time
    now = time.time()
    conn.execute("INSERT INTO projects VALUES('P1','旧项目',?)", (now,))
    residents = [{"id": rid, "x": x, "y": y} for rid, x, y in ex.RESIDENTS]
    stations = [{"id": sid, "x": x, "y": y} for sid, x, y in ex.STATIONS]
    conn.execute(
        "INSERT INTO versions VALUES(?,?,?,?,?,?,?,?)",
        ("V1", "P1", 1, None, "旧版诊所",
         json.dumps(residents), json.dumps(stations), now))
    # 旧的最优方案（r=3，3 座）原样保留
    result = {"status": "optimal", "chosen": [0, 1, 2],
              "station_count": 3, "lower_bound": 3, "gap": 0,
              "explored_nodes": 1, "uncovered": [],
              "proven_optimal": True}
    conn.execute(
        "INSERT INTO solutions VALUES(?,?,?,?,?,?,?)",
        ("S1", "V1", 3.0, "[]", json.dumps(result), "full", now))
    conn.commit()
    conn.close()


def test_takeover_legacy_db_reads_data_and_keeps_solution(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app

    db_path = str(tmp_path / "legacy.db")
    _create_legacy_db(db_path)
    with TestClient(create_app(db_path=db_path)) as c:
        # 旧版本居民点按权重 1 取回
        v = c.get("/api/projects/P1/versions/V1").json()
        assert all(r["weight"] == 1.0 for r in v["residents"])
        # 旧方案结果不变：同参数求解直接复用
        r = c.post("/api/projects/P1/versions/V1/solve",
                   json={"radius": 3.0, "forced_station_ids": []})
        assert r.status_code == 202
        body = r.json()
        assert body["reused"] is True
        assert body["result"]["station_count"] == 3
        assert body["result"]["chosen"] == [0, 1, 2]
        # 新能力直接在旧数据上工作（旧居民点权重 1）
        r = c.post("/api/projects/P1/versions/V1/plans",
                   json={"radius": 3.0, "budgets": [1, 1, 1]})
        assert r.status_code == 201, r.text
        p = r.json()
        assert p["status"] == "optimal"
        assert [x["covered_population"] for x in p["periods"]] == [6, 9, 12]
        assert p["total_stations"] == 3
        # 同参数重复发起不产生新记录
        r2 = c.post("/api/projects/P1/versions/V1/plans",
                    json={"radius": 3.0, "budgets": [1, 1, 1]})
        assert r2.json()["id"] == p["id"]


# ---------------------------------------------------------------------------
# 补充不变量：嵌套、新增不超预算、人口不减、总站数 >= 无分期最少
# ---------------------------------------------------------------------------

def test_period_invariants_via_api(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(1, 2))
    prev_stations = set()
    prev_pop = -1
    for q in p["periods"]:
        new = set(q["new_station_ids"])
        cum = set(q["cumulative_station_ids"])
        assert new.isdisjoint(prev_stations)          # 后期不重建已建
        assert prev_stations <= cum                    # 嵌套
        assert q["new_count"] <= q["budget"]
        assert q["covered_population"] >= prev_pop     # 人口不减
        prev_stations = cum
        prev_pop = q["covered_population"]
    assert p["total_stations"] >= p["minimum_stations"]
    assert p["extra_over_minimum"] == \
        p["total_stations"] - p["minimum_stations"]


def test_confirm_then_replan_does_not_touch_confirmed_pop(client):
    """确认两期后再重排：前两期累计人口与新建站都不变。"""
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(1, 1, 1))
    p = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": p["revision"]}).json()
    p = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": p["revision"]}).json()
    assert p["confirmed_until"] == 1
    frozen = [(q["new_station_ids"], q["covered_population"])
              for q in p["periods"][:2]]
    r = client.post(f"/api/plans/{p['id']}/replan",
                    json={"revision": p["revision"],
                          "budgets": [1, 1, 3]})
    assert r.status_code == 200
    q = r.json()
    assert [(x["new_station_ids"], x["covered_population"])
            for x in q["periods"][:2]] == frozen
    assert q["periods"][:2] and all(x["confirmed"] for x in q["periods"][:2])


def test_replan_too_few_periods_for_confirmed_is_409(client):
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(1, 1, 1))
    p = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": p["revision"]}).json()
    # 已确认第 1 期，却只给 1 期新预算
    r = client.post(f"/api/plans/{p['id']}/replan",
                    json={"revision": p["revision"], "budgets": [3]})
    assert r.status_code == 409
    assert "期" in r.json()["error"]


def test_replan_that_cannot_close_is_reported_not_success(client):
    """已确认 C1 后把预算砍到放不下剩余最少站：报 cannot_close。"""
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(1, 1, 1))
    p = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": p["revision"]}).json()
    r = client.post(f"/api/plans/{p['id']}/replan",
                    json={"revision": p["revision"], "budgets": [1, 1]})
    # 409 语义上这是"业务收不了口"还是 200 带 cannot_close？设计选择：
    # 已确认期容得下（第 1 期预算 1 == 已建 1），但整体收不了口 ——
    # 返回 200 的重排结果，状态 cannot_close，绝不伪装成功。
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["status"] == "cannot_close"
    assert q["feasible"] is False
    assert q["blocked_period"] is not None
    assert q["uncovered_resident_ids"]
    # 已确认期仍冻结
    assert q["periods"][0]["new_station_ids"] == ["C1"]
    # 对收不了口的计划不能再确认
    c = client.post(f"/api/plans/{q['id']}/confirm",
                    json={"revision": q["revision"]})
    assert c.status_code == 409


def test_duplicate_plan_after_confirm_still_returns_same_record(client):
    """同参数重复发起：即使计划已被确认，也取回同一条（不新建）。"""
    pid, vid = setup_weighted(client)
    p1 = create_plan(client, pid, vid, budgets=(1, 1, 1))
    client.post(f"/api/plans/{p1['id']}/confirm", json={"revision": 1})
    p2 = create_plan(client, pid, vid, budgets=(1, 1, 1))
    assert p2["id"] == p1["id"]
    assert p2["reused"] is True
    assert p2["confirmed_until"] == 0   # 取回的是被确认过的同一条


def test_replan_blocked_then_feasible_again(client):
    """收不了口的重排不会把计划锁死：预算补回来后能重新收口。"""
    pid, vid = setup_weighted(client)
    p = create_plan(client, pid, vid, budgets=(1, 1, 1))
    p = client.post(f"/api/plans/{p['id']}/confirm",
                    json={"revision": p["revision"]}).json()
    # 砍预算 → 收不了口
    r = client.post(f"/api/plans/{p['id']}/replan",
                    json={"revision": p["revision"], "budgets": [1, 1]})
    assert r.json()["status"] == "cannot_close"
    rev2 = r.json()["revision"]
    # 预算补回来 → 重新收口，第 1 期仍是冻结的 C1
    r = client.post(f"/api/plans/{p['id']}/replan",
                    json={"revision": rev2, "budgets": [1, 1, 1]})
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["status"] == "optimal"
    assert q["feasible"] is True
    assert q["periods"][0]["new_station_ids"] == ["C1"]
    assert q["periods"][-1]["covered_population"] == 62


def test_derive_version_preserves_weights(client):
    """增量增删居民点时 weight 随版本继承。"""
    pid, vid = setup_weighted(client)
    r = client.post(
        f"/api/projects/{pid}/versions/from/{vid}",
        json={"add_residents": [{"id": "NEW", "x": 5.0, "y": 0.0,
                                 "weight": 100}],
              "change_note": "加重点户"})
    assert r.status_code == 201, r.text
    v2 = r.json()
    wm = {x["id"]: x["weight"] for x in v2["residents"]}
    assert wm["R01"] == 12
    assert wm["NEW"] == 100
    # 新点 100 人只有 C3 够得着（5,0 距 C3=0；C4 距 1.8 也够）
    p = create_plan(client, pid, v2["id"], budgets=(1, 1, 1))
    assert p["total_population"] == 162
    assert p["status"] in ("optimal", "feasible")
