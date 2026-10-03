"""分期规划单元测试：诊所带权基准、暴力枚举对拍、重排锁前缀、不可行诊断。"""

import itertools
import random

import pytest

from app import examples as ex
from app.coverage import Point, build_coverage
from app.phasing import (
    OBJECTIVE,
    PhaseRequest,
    plan_phases,
    result_to_dict,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _clinic_weighted():
    residents = [{"id": i, "x": x, "y": y, "weight": ex.RESIDENT_WEIGHTS[i]}
                 for i, x, y in ex.RESIDENTS]
    stations = [{"id": i, "x": x, "y": y} for i, x, y in ex.STATIONS]
    return residents, stations


def _pop(mask, weights):
    return sum(weights[i] for i in range(len(weights)) if (mask >> i) & 1)


def brute_force_phasing(residents, stations, radius, budgets,
                        forced_ids=(), locked_groups=None,
                        unphased_baseline=None):
    """按声明的字典序目标独立暴力枚举。

    枚举每座站的期号 0..T（T 表示不建），要求：
    - 最后一期累计全覆盖；必开站都被建；锁定前缀逐期一致；
    - 每期新建数 ≤ 预算；总站数最小优先；
    - 同总站数下逐期累计覆盖人口字典序最大；再平局取站集合编号最小。
    返回 dict 或 None（不可行）。
    """
    ids = [s["id"] for s in stations]
    rids = [r["id"] for r in residents]
    weights = [float(r.get("weight", 1) or 1) for r in residents]
    pts = [Point(r["x"], r["y"]) for r in residents]
    spts = [Point(s["x"], s["y"]) for s in stations]
    cov = build_coverage(pts, spts, radius)
    cover = {ids[j]: cov.by_station[j] for j in range(len(ids))}
    full = (1 << len(rids)) - 1
    T = len(budgets)
    locked_groups = locked_groups or []
    locked_period = {}
    for t, g in enumerate(locked_groups):
        for s in g:
            locked_period[s] = t

    best = None  # (站数, 负人口序列..., 站集合, 分组)

    def rec(j, assign):
        nonlocal best
        if j == len(ids):
            groups = [[] for _ in range(T)]
            built = set()
            for sid, t in assign.items():
                groups[t].append(sid)
                built.add(sid)
            if not set(forced_ids) <= built:
                return
            for t, g in enumerate(locked_groups):
                if sorted(groups[t]) != sorted(g):
                    return
            if any(len(g) > budgets[t] for t, g in enumerate(groups)):
                return
            mask, pops = 0, []
            for t in range(T):
                for sid in groups[t]:
                    mask |= cover[sid]
                pops.append(_pop(mask, weights))
            if mask & full != full:
                return
            key = (len(built), tuple(-p for p in pops),
                   tuple(sorted(built)),
                   tuple(tuple(sorted(g)) for g in groups))
            if best is None or key < best[0]:
                best = (key, groups, pops)
            return
        sid = ids[j]
        if sid in locked_period:
            rec(j + 1, {**assign, sid: locked_period[sid]})
            return
        # 不建
        rec(j + 1, assign)
        # 建在第 t 期
        for t in range(T):
            rec(j + 1, {**assign, sid: t})

    rec(0, {})
    if best is None:
        return None
    _, groups, pops = best
    return {
        "new_groups": [sorted(g) for g in groups],
        "cum_population": pops,
        "total_station_count": len(best[0][2]),
        "unphased": unphased_baseline,
    }


# ---------------------------------------------------------------------------
# 诊所带权基准：手工可验算
# ---------------------------------------------------------------------------

def test_clinic_weighted_phased_111_hand_checked():
    residents, stations = _clinic_weighted()
    r = plan_phases(PhaseRequest(
        residents=residents, stations=stations, radius=3.0,
        budgets=list(ex.EXPECTED_PHASED_111_BUDGETS)))
    d = result_to_dict(r, list(ex.EXPECTED_PHASED_111_BUDGETS))
    assert r.status == "optimal"
    assert r.proven_optimal is True
    assert r.total_station_count == ex.EXPECTED_COUNT_R3
    assert r.extra_over_unphased == 0
    new = [tuple(p["new_station_ids"]) for p in d["phases"]]
    assert tuple(new) == ex.EXPECTED_PHASED_111_NEW
    pops = [p["cumulative_population"] for p in d["phases"]]
    assert tuple(pops) == ex.EXPECTED_PHASED_111_CUM_POP
    assert r.total_population == ex.EXPECTED_TOTAL_WEIGHT
    # 逐期包含、预算
    cum = set()
    for t, p in enumerate(d["phases"]):
        g = set(p["new_station_ids"])
        assert not (g & cum)
        assert len(g) <= ex.EXPECTED_PHASED_111_BUDGETS[t]
        cum |= g


def test_clinic_weighted_phased_21_hand_checked():
    residents, stations = _clinic_weighted()
    r = plan_phases(PhaseRequest(
        residents=residents, stations=stations, radius=3.0,
        budgets=list(ex.EXPECTED_PHASED_21_BUDGETS)))
    d = result_to_dict(r, list(ex.EXPECTED_PHASED_21_BUDGETS))
    assert r.proven_optimal is True
    new = [tuple(p["new_station_ids"]) for p in d["phases"]]
    assert tuple(new) == ex.EXPECTED_PHASED_21_NEW
    pops = [p["cumulative_population"] for p in d["phases"]]
    assert tuple(pops) == ex.EXPECTED_PHASED_21_CUM_POP
    assert r.total_station_count == 3 and r.extra_over_unphased == 0


def test_clinic_weighted_phased_budget_shortfall():
    residents, stations = _clinic_weighted()
    r = plan_phases(PhaseRequest(
        residents=residents, stations=stations, radius=3.0,
        budgets=list(ex.EXPECTED_PHASED_11_BUDGETS)))
    assert r.feasible is False
    assert r.status == "infeasible"
    assert r.blocked_at_period == ex.EXPECTED_PHASED_11_BLOCKED_PERIOD
    assert r.budget_shortage == ex.EXPECTED_PHASED_11_SHORTAGE
    # 不许报成成功
    assert r.total_station_count is None
    assert r.proven_optimal is False


def test_clinic_weighted_unreachable_lists_points():
    residents, stations = _clinic_weighted()
    r = plan_phases(PhaseRequest(
        residents=residents, stations=stations, radius=2.0,
        budgets=[2, 2, 2]))
    assert r.status == "infeasible" and r.feasible is False
    assert r.uncovered_ids == ["R01", "R02"]
    assert r.blocked_at_period is None


def test_weights_do_not_change_unphased_solver():
    """加权后无分期最少开站结果与无权基准逐位一致。"""
    from app.solver import SolveRequest, solve
    residents, stations = _clinic_weighted()
    pts = [Point(r["x"], r["y"]) for r in residents]
    spts = [Point(s["x"], s["y"]) for s in stations]
    cov = build_coverage(pts, spts, 3.0)
    res = solve(SolveRequest(cov))
    assert res.station_count == 3
    assert tuple(ex.STATIONS[j][0] for j in res.chosen) == \
        ex.EXPECTED_OPTIMAL_R3
    assert res.proven_optimal is True


# ---------------------------------------------------------------------------
# 随机小实例：凡标已证明最优的，必须和独立暴力枚举一致
# ---------------------------------------------------------------------------

def _random_instance(rng):
    n = rng.randint(1, 7)
    m = rng.randint(2, 5)
    pts = [(f"R{i}", round(rng.random() * 8, 2), round(rng.random() * 8, 2))
           for i in range(n)]
    spts = [(f"C{j}", round(rng.random() * 8, 2), round(rng.random() * 8, 2))
            for j in range(m)]
    radius = round(rng.uniform(1.5, 4.5), 2)
    residents = [{"id": i, "x": x, "y": y,
                  "weight": rng.randint(1, 9)} for i, x, y in pts]
    stations = [{"id": i, "x": x, "y": y} for i, x, y in spts]
    T = rng.randint(2, 3)
    budgets = [rng.randint(1, m) for _ in range(T)]
    forced = [spts[j][0] for j in
              rng.sample(range(m), rng.randint(0, min(1, m - 1)))]
    return residents, stations, radius, budgets, forced


@pytest.mark.parametrize("trial", range(60))
def test_random_proven_plans_match_brute_force(trial):
    rng = random.Random(20000 + trial)
    residents, stations, radius, budgets, forced = _random_instance(rng)
    r = plan_phases(PhaseRequest(
        residents=residents, stations=stations, radius=radius,
        budgets=budgets, forced_ids=forced))
    opt = brute_force_phasing(residents, stations, radius, budgets, forced)

    if r.status == "infeasible":
        assert opt is None
        # 预算型不可行必须给出卡点期；够不着型必须列点
        d = result_to_dict(r, budgets)
        assert (d["blocked_at_period"] is not None) or \
               (d["uncovered_resident_ids"])
        return

    assert r.feasible and opt is not None
    d = result_to_dict(r, budgets)
    # 基本合法性：逐期包含、预算、末期满覆盖
    cum = set()
    for t, p in enumerate(d["phases"]):
        g = set(p["new_station_ids"])
        assert not (g & cum)
        assert len(g) <= budgets[t]
        cum |= g
    assert abs(d["phases"][-1]["cumulative_population"]
               - r.total_population) < 1e-9
    assert r.total_station_count >= (r.unphased_count or 0)

    if r.proven_optimal:
        assert r.total_station_count == opt["total_station_count"]
        got = [p["cumulative_population"] for p in d["phases"]]
        assert got == opt["cum_population"]
        assert [p["new_station_ids"] for p in d["phases"]] == \
            opt["new_groups"]


def test_random_replan_locked_prefix_matches_brute_force():
    """枚举若干小实例，凡"初始计划已证明"的，锁第一期后重排也对拍。"""
    checked = 0
    for trial in range(60):
        rng = random.Random(30000 + trial)
        residents, stations, radius, budgets, forced = _random_instance(rng)
        r0 = plan_phases(PhaseRequest(
            residents=residents, stations=stations, radius=radius,
            budgets=budgets, forced_ids=forced))
        if r0.status != "optimal":
            continue
        # 锁第 0 期，放宽后续预算后重排
        locked = [list(r0.new_groups[0])]
        new_budgets = [max(budgets[0], len(locked[0]))]
        new_budgets += [b + 1 for b in budgets[1:]]
        r1 = plan_phases(PhaseRequest(
            residents=residents, stations=stations, radius=radius,
            budgets=new_budgets, forced_ids=forced, locked_groups=locked,
            unphased_baseline=r0.unphased_count))
        opt = brute_force_phasing(residents, stations, radius, new_budgets,
                                  forced, locked_groups=locked,
                                  unphased_baseline=r0.unphased_count)
        if r1.status == "infeasible":
            assert opt is None
            continue
        d = result_to_dict(r1, new_budgets)
        assert d["phases"][0]["new_station_ids"] == sorted(locked[0])
        if r1.proven_optimal:
            checked += 1
            assert opt is not None
            got = [p["cumulative_population"] for p in d["phases"]]
            assert got == opt["cum_population"]
    # 小实例里必须真的对拍到若干已证明的重排，防止测试空转
    assert checked >= 3


def test_clinic_replan_locked_first_period_matches_brute_force():
    """诊所基准锁死第一期 C2 后，[1,2,2] 重排结果与独立暴力枚举一致。"""
    residents, stations = _clinic_weighted()
    budgets = [1, 2, 2]
    locked = [["C2"]]
    r = plan_phases(PhaseRequest(
        residents=residents, stations=stations, radius=3.0,
        budgets=budgets, locked_groups=locked, unphased_baseline=3))
    assert r.status == "optimal" and r.proven_optimal
    opt = brute_force_phasing(residents, stations, 3.0, budgets, (),
                              locked_groups=locked, unphased_baseline=3)
    d = result_to_dict(r, budgets)
    assert d["phases"][0]["new_station_ids"] == ["C2"]
    got = [p["cumulative_population"] for p in d["phases"]]
    assert got == opt["cum_population"]
    assert r.total_station_count == 3 and r.extra_over_unphased == 0


def test_replan_extra_stations_when_lock_forces_larger_final_set():
    """已确认前缀逼得最终集合比 OPT 大时，extra 如实报告，仍与暴力一致。"""
    # 构造：锁一个"差"的首期站组，使后面必须多建才能收口。
    # 诊所例 r=3：锁第一期 C5（诱饵站），最终必须含 C1/C2/C3/C5 = 4 座。
    residents, stations = _clinic_weighted()
    budgets = [1, 2, 2]
    locked = [["C5"]]
    r = plan_phases(PhaseRequest(
        residents=residents, stations=stations, radius=3.0,
        budgets=budgets, forced_ids=[], locked_groups=locked,
        unphased_baseline=3))
    assert r.feasible
    d = result_to_dict(r, budgets)
    assert d["phases"][0]["new_station_ids"] == ["C5"]
    assert "C5" in d["phases"][-1]["cumulative_station_ids"]
    assert r.total_station_count == 4
    assert r.extra_over_unphased == 1
    opt = brute_force_phasing(residents, stations, 3.0, budgets, (),
                              locked_groups=locked, unphased_baseline=3)
    if r.proven_optimal:
        got = [p["cumulative_population"] for p in d["phases"]]
        assert got == opt["cum_population"]


def test_replan_budget_too_tight_after_lock_reports_infeasible():
    """锁第一期 C2 后，后续预算合计装不下另外两座必建站 → 收不了口。"""
    residents, stations = _clinic_weighted()
    r = plan_phases(PhaseRequest(
        residents=residents, stations=stations, radius=3.0,
        budgets=[1, 0, 1], locked_groups=[["C2"]], unphased_baseline=3))
    assert r.status == "infeasible" and r.feasible is False
    assert r.blocked_at_period is not None and r.budget_shortage is not None
    assert r.total_station_count is None


def test_objective_name_stable():
    assert OBJECTIVE == "lex_min_stations_then_lex_max_covered_population"


def test_forced_station_must_be_built():
    """必开站必须出现在最终累计集合里，即使它不在无权最优组合中。"""
    residents, stations = _clinic_weighted()
    r = plan_phases(PhaseRequest(
        residents=residents, stations=stations, radius=3.0,
        budgets=[2, 2], forced_ids=["C4"]))
    d = result_to_dict(r, [2, 2])
    assert r.feasible
    assert "C4" in d["phases"][-1]["cumulative_station_ids"]
    assert r.total_station_count == 4  # 无分期强制 C4 时为 4 座
    assert r.extra_over_unphased == 0  # 对比口径同样含必开 C4


def test_truncation_marks_unproven_but_keeps_feasible_plan():
    """极小枚举额度触发截断：交可行计划但不标已证明。"""
    residents, stations = _clinic_weighted()
    r = plan_phases(PhaseRequest(
        residents=residents, stations=stations, radius=3.0,
        budgets=[1, 1, 1],
        cover_enum_cap=1, alloc_node_cap=1))
    assert r.feasible is True
    assert r.proven_optimal is False
    assert r.status == "best_feasible"
    assert r.total_station_count == 3
    assert abs(r.cum_population[-1] - r.total_population) < 1e-9
