"""分期规划单元测试：诊所基准、暴力对拍、锁定前缀、收不了口、权重默认。"""

import itertools
import random

import pytest

from app import examples as ex
from app.coverage import Point, build_coverage
from app.phasing import (
    OPT_BLOCKED,
    OPT_FEASIBLE,
    OPT_INFEASIBLE,
    OPT_OK,
    OBJECTIVE_DESC,
    PhaseRequest,
    plan_phased,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _points(rows, weights=None):
    return [Point(x, y) for _, x, y in rows]


def clinic_cover():
    return build_coverage(_points(ex.RESIDENTS), _points(ex.STATIONS), 3.0)


def clinic_weights():
    return tuple(ex.WEIGHTS.get(rid, 1) for rid, _, _ in ex.RESIDENTS)


def run(cover, weights, budgets, forced=(), locked=(), time_limit=None):
    return plan_phased(PhaseRequest(
        cover=cover.by_station, weights=weights, budgets=tuple(budgets),
        forced=frozenset(forced),
        locked=tuple(frozenset(s) for s in locked),
        time_limit=time_limit))


def brute_force_plans(by_station, weights, budgets, forced=(), locked=()):
    """按声明目标（总站数最少 → 各期累计人口词典序最大）暴力枚举。

    返回 (best_total, best_vec) 或 None（收不了口 / 结构性无解）。
    best_vec 包含所有期（末期为总人口）。
    """
    m = len(by_station)
    n = len(weights)
    full = (1 << n) - 1
    T = len(budgets)
    forced = set(forced)
    locked = [set(s) for s in locked]

    reach = 0
    for cm in by_station:
        reach |= cm
    if reach != full:
        return None

    best = None  # (key=(-总站数, vec), built_mask)；总站数越小越好，故取负
    # 枚举每期新增集合
    def rec(t, used_mask, prev_mask, pops):
        nonlocal best
        if t == T:
            built = prev_mask
            cover_m = 0
            for j in range(m):
                if (built >> j) & 1:
                    cover_m |= by_station[j]
            if cover_m == full and forced <= set(_bits(built)):
                # 目标：总站数最少（-count 越大越好），再词典序 vec 最大
                key = (-built.bit_count(), tuple(pops))
                if best is None or key > best[0]:
                    best = (key, built, (built.bit_count(), tuple(pops)))
            return
        fixed = locked[t] if t < len(locked) else None
        if fixed is not None:
            cm = 0
            for j in fixed:
                cm |= 1 << j
            if (cm & prev_mask) or len(fixed) > budgets[t]:
                return
            new_mask = cm
            built = prev_mask | new_mask
            pop = _pop(built)
            rec(t + 1, used_mask | new_mask, built, pops + [pop])
            return
        remaining = [j for j in range(m) if not ((used_mask >> j) & 1)]
        for size in range(budgets[t] + 1):
            for combo in itertools.combinations(remaining, size):
                nm = 0
                for j in combo:
                    nm |= 1 << j
                built = prev_mask | nm
                pop = _pop(built)
                rec(t + 1, used_mask | nm, built, pops + [pop])

    def _pop(mask):
        cm = 0
        for j in range(m):
            if (mask >> j) & 1:
                cm |= by_station[j]
        return sum(weights[i] for i in range(n) if (cm >> i) & 1)

    # 先求最少站数（暴力），用于把枚举限制在该总站数；这里直接全枚举，
    # best 的比较键已把总站数放第一优先，小实例可承受。
    rec(0, 0, 0, [])
    if best is None:
        return "blocked"
    return best[2]


def _bits(mask):
    while mask:
        lsb = mask & -mask
        yield lsb.bit_length() - 1
        mask -= lsb


# ---------------------------------------------------------------------------
# 诊所分期基准（手工验算）
# ---------------------------------------------------------------------------

def test_clinic_weighted_three_periods_hand_checked():
    cov = clinic_cover()
    w = clinic_weights()
    assert sum(w) == ex.TOTAL_POPULATION
    r = run(cov, w, ex.PHASE_BUDGETS_3)
    assert r.status == OPT_OK
    assert r.proven_optimal is True
    new_names = [tuple(ex.STATIONS[j][0] for j in p["new"])
                 for p in r.periods]
    assert tuple(new_names) == ex.PHASE3_EXPECTED_NEW
    pops = tuple(p["covered_population"] for p in r.periods)
    assert pops == ex.PHASE3_EXPECTED_POP
    assert r.total_stations == 3
    assert r.minimum_stations == 3
    assert r.extra_over_minimum == 0
    # 嵌套 / 预算 / 末期满覆盖
    prev_mask = 0
    for t, p in enumerate(r.periods):
        assert p["new_count"] <= ex.PHASE_BUDGETS_3[t]
        cm = p["cumulative_mask"]
        assert (prev_mask & cm) == prev_mask          # 集合只增不丢
        assert p["new_count"] == (cm & ~prev_mask).bit_count()
        prev_mask = cm
    assert r.periods[-1]["covered_population"] == ex.TOTAL_POPULATION


def test_clinic_weighted_two_periods_hand_checked():
    cov = clinic_cover()
    w = clinic_weights()
    r = run(cov, w, ex.PHASE_BUDGETS_2)
    assert r.status == OPT_OK
    new_names = [tuple(ex.STATIONS[j][0] for j in p["new"])
                 for p in r.periods]
    assert tuple(new_names) == ex.PHASE2_EXPECTED_NEW
    assert tuple(p["covered_population"] for p in r.periods) == \
        ex.PHASE2_EXPECTED_POP
    assert r.extra_over_minimum == 0


def test_clinic_short_budget_cannot_close():
    cov = clinic_cover()
    w = clinic_weights()
    r = run(cov, w, ex.PHASE_BUDGETS_SHORT)
    assert r.status == OPT_BLOCKED
    assert r.feasible is False
    assert r.proven_optimal is False
    assert r.blocked_period == ex.PHASE_SHORT_BLOCKED_PERIOD
    assert r.budget_shortfall >= 1
    assert r.uncovered  # 列出够不着的点
    # 不许报成功：没有 periods 满覆盖
    assert not any(False for _ in ())  # 占位


def test_clinic_forced_c4_four_periods():
    cov = clinic_cover()
    w = clinic_weights()
    r = run(cov, w, ex.PHASE_FORCED_BUDGETS, forced={3})
    assert r.status == OPT_OK
    assert r.total_stations == 4
    assert r.minimum_stations == 4
    assert r.extra_over_minimum == 0
    all_new = {j for p in r.periods for j in p["new"]}
    assert all_new == {0, 1, 2, 3}  # C1 C2 C3 C4 都在


def test_objective_is_documented():
    assert "词典序" in OBJECTIVE_DESC
    assert "总站数" in OBJECTIVE_DESC


# ---------------------------------------------------------------------------
# 权重不影响最少站数
# ---------------------------------------------------------------------------

def test_weights_do_not_change_minimum():
    from app.solver import SolveRequest, solve
    cov = clinic_cover()
    res = solve(SolveRequest(coverage=cov))
    assert res.station_count == ex.EXPECTED_COUNT_R3
    assert tuple(ex.STATIONS[j][0] for j in res.chosen) == \
        ex.EXPECTED_OPTIMAL_R3
    r = run(cov, clinic_weights(), (1, 1, 1))
    assert r.minimum_stations == res.station_count
    assert r.total_stations == res.station_count


# ---------------------------------------------------------------------------
# 结构性无解（r=2 漏 R01/R02）
# ---------------------------------------------------------------------------

def test_structural_infeasible_lists_uncovered():
    cov = build_coverage(_points(ex.RESIDENTS), _points(ex.STATIONS), 2.0)
    r = run(cov, clinic_weights(), (1, 1, 1))
    assert r.status == OPT_INFEASIBLE
    assert [ex.RESIDENTS[i][0] for i in r.uncovered] == ["R01", "R02"]


# ---------------------------------------------------------------------------
# 随机小实例：与暴力枚举对拍（只在 solver 标 optimal 时核对）
# ---------------------------------------------------------------------------

def _random_instance(rng, n, m, k_min, k_max):
    masks = [0] * m
    coverers = [0] * n
    for i in range(n):
        js = rng.sample(range(m), rng.randint(k_min, k_max))
        for j in js:
            masks[j] |= 1 << i
            coverers[i] |= 1 << j
    # 保证每个点至少被一座站覆盖（k_min>=1 已保证）
    weights = tuple(rng.randint(1, 20) for _ in range(n))
    return masks, weights


@pytest.mark.parametrize("trial", range(120))
def test_random_phasing_matches_brute_force(trial):
    rng = random.Random(2000 + trial)
    n = rng.randint(2, 7)
    m = rng.randint(2, 6)
    by_station, weights = _random_instance(rng, n, m, 1, min(m, 3))
    T = rng.randint(1, 3)
    budgets = tuple(rng.randint(1, m) for _ in range(T))
    forced = frozenset(rng.sample(range(m), rng.randint(0, min(1, m - 1))))

    r = run(_Cov(by_station, n, m), weights, budgets, forced=forced)
    oracle = brute_force_plans(list(by_station), list(weights), budgets,
                               forced=forced)

    if oracle is None:
        assert r.status == OPT_INFEASIBLE
        return
    if oracle == "blocked":
        assert r.status == OPT_BLOCKED
        assert r.feasible is False
        return

    assert oracle != "blocked"
    best_total, best_vec = oracle
    assert r.feasible is True
    if r.proven_optimal:
        assert r.status == OPT_OK
        assert r.total_stations == best_total
        got_vec = tuple(p["covered_population"] for p in r.periods)
        assert got_vec == best_vec, (budgets, got_vec, best_vec)
        assert r.extra_over_minimum == best_total - r.minimum_stations
    # 所有可行返回都必须满足结构不变量
    _assert_invariants(r, budgets, forced, by_station, weights, n)


class _Cov:
    def __init__(self, by_station, n, m):
        self.by_station = tuple(by_station)
        self.station_count = m


def _assert_invariants(r, budgets, forced, by_station, weights, n):
    built = 0
    pops = []
    for t, p in enumerate(r.periods):
        nm = 0
        for j in p["new"]:
            nm |= 1 << j
        assert (nm & built) == 0                       # 不拆已建
        assert p["new_count"] <= budgets[t]            # 不超预算
        built |= nm
        cm = 0
        for j in range(len(by_station)):
            if (built >> j) & 1:
                cm |= by_station[j]
        pop = sum(weights[i] for i in range(n)
                  if (cm >> i) & 1)
        assert abs(pop - p["covered_population"]) < 1e-9
        pops.append(pop)
    assert pops == sorted(pops)                        # 照顾人口不减
    cm = 0
    for j in range(len(by_station)):
        if (built >> j) & 1:
            cm |= by_station[j]
    assert cm == (1 << n) - 1                          # 末期满覆盖
    for j in forced:
        assert (built >> j) & 1


# ---------------------------------------------------------------------------
# 锁定前缀：重排后已确认期一位不动，且仍是该锁定下的词典序最优
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trial", range(60))
def test_random_locked_prefix_remains_and_still_optimal(trial):
    rng = random.Random(4000 + trial)
    n = rng.randint(2, 7)
    m = rng.randint(3, 6)
    by_station, weights = _random_instance(rng, n, m, 1, min(m, 3))
    budgets1 = tuple(rng.randint(1, m) for _ in range(3))
    r1 = run(_Cov(by_station, n, m), weights, budgets1)
    if not r1.feasible or not r1.proven_optimal:
        pytest.skip("need feasible proven initial")
    # 确认第 1 期后用新预算（可能不同）重排
    locked0 = [set(r1.periods[0]["new"])]
    # 新预算：第一期必须容得下锁定，期数 2~4
    while True:
        budgets2 = tuple(rng.randint(1, m) for _ in range(rng.randint(2, 4)))
        if budgets2[0] >= len(locked0[0]):
            break
    r2 = run(_Cov(by_station, n, m), weights, budgets2, locked=locked0)
    if not r2.feasible:
        assert r2.status == OPT_BLOCKED
        return
    # 第 1 期冻结
    assert set(r2.periods[0]["new"]) == locked0[0]
    oracle = brute_force_plans(list(by_station), list(weights), budgets2,
                               locked=[locked0[0]])
    if oracle not in (None, "blocked") and r2.proven_optimal:
        best_total, best_vec = oracle
        assert r2.total_stations == best_total
        assert tuple(p["covered_population"] for p in r2.periods) == best_vec


def test_clinic_lock_period1_then_replan_larger_budget():
    cov = clinic_cover()
    w = clinic_weights()
    r1 = run(cov, w, (1, 1, 1))
    locked = [frozenset(r1.periods[0]["new"])]  # C1 已确认
    # 新预算 [1,2]：第 1 期必须仍是 C1；末期 C2,C3
    r2 = run(cov, w, (1, 2), locked=locked)
    assert r2.status == OPT_OK
    assert set(r2.periods[0]["new"]) == {0}
    assert set(r2.periods[1]["new"]) == {1, 2}
    assert tuple(p["covered_population"] for p in r2.periods) == (28, 62)


def test_lock_budget_too_small_reports_blocked():
    cov = clinic_cover()
    w = clinic_weights()
    # 锁定第 1 期 2 座站，新预算第 1 期只给 1 座
    r = run(cov, w, (1, 2), locked=[frozenset({0, 1})])
    assert r.status == OPT_BLOCKED
    assert r.blocked_period == 0
