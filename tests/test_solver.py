"""求解器单元测试：暴力对拍、下界合法性、重复坐标、增量、诊所基准。"""

import itertools
import random
import threading

import pytest

from app import examples as ex
from app.coverage import Coverage, Point, build_coverage
from app.incremental import PriorSolution, solve_incremental
from app.solver import SolveRequest, solve


def _points(rows):
    return [Point(x, y) for _, x, y in rows]


def brute_force(residents, stations, radius, forced=set()):
    """枚举返回最少站数；无解返回 None。"""
    cov = build_coverage(_points(residents), _points(stations), radius)
    n = len(residents)
    full = (1 << n) - 1
    m = len(stations)
    for k in range(m + 1):
        for combo in itertools.combinations(range(m), k):
            if not forced <= set(combo):
                continue
            mask = 0
            for j in combo:
                mask |= cov.by_station[j]
            if (mask & full) == full:
                return k
    return None


def clinic_coverage(radius):
    return build_coverage(_points(ex.RESIDENTS), _points(ex.STATIONS), radius)


# ---------------------------------------------------------------------------
# 诊所回归基准（手工验算）
# ---------------------------------------------------------------------------

def test_clinic_hand_checked_r3():
    cov = clinic_coverage(3.0)
    res = solve(SolveRequest(coverage=cov))
    assert res.status == "optimal"
    assert res.proven_optimal is True
    assert res.station_count == ex.EXPECTED_COUNT_R3
    chosen = tuple(ex.STATIONS[j][0] for j in res.chosen)
    assert chosen == ex.EXPECTED_OPTIMAL_R3
    assert res.lower_bound == 3 and res.gap == 0
    # 全覆盖且无遗漏
    covered = 0
    for j in res.chosen:
        covered |= cov.by_station[j]
    assert covered == (1 << 12) - 1


def test_clinic_greedy_not_marked_optimal_by_accident():
    # 该例贪心从无强制出发也能给出 3，但 proven 必须来自完整树证明
    cov = clinic_coverage(3.0)
    res = solve(SolveRequest(coverage=cov))
    assert res.proven_optimal is True
    # 从结构上：根下界与最优相等也应明确 proven，且站数正确
    assert res.station_count == 3


def test_clinic_radius_up_does_not_increase_count():
    c3 = solve(SolveRequest(coverage=clinic_coverage(3.0)))
    c4 = solve(SolveRequest(coverage=clinic_coverage(4.0)))
    assert c3.station_count == 3
    assert c4.station_count == 2
    assert c4.station_count <= c3.station_count


def test_clinic_r2_infeasible_lists_points():
    res = solve(SolveRequest(coverage=clinic_coverage(2.0)))
    assert res.status == "infeasible"
    assert res.station_count is None
    assert res.proven_optimal is False
    ids = tuple(ex.RESIDENTS[i][0] for i in res.uncovered)
    assert ids == ex.EXPECTED_UNCOVERED_R2


def test_clinic_force_nonoptimal_station_count_not_less():
    res = solve(SolveRequest(coverage=clinic_coverage(3.0),
                             forced=frozenset({3})))  # C4
    assert res.status == "optimal"
    assert res.station_count == ex.EXPECTED_FORCED_C4_COUNT  # 4
    assert 3 in res.chosen  # C3 仍被迫出现
    chosen_names = {ex.STATIONS[j][0] for j in res.chosen}
    assert "C4" in chosen_names


def test_clinic_remove_critical_station_is_infeasible():
    # 移除所有最优解都离不开的 C1：直接从站点列表删除
    stations = [s for s in ex.STATIONS if s[0] != "C1"]
    cov = build_coverage(_points(ex.RESIDENTS), _points(stations), 3.0)
    res = solve(SolveRequest(coverage=cov))
    assert res.status == "infeasible"
    ids = tuple(ex.RESIDENTS[i][0] for i in res.uncovered)
    assert ids == ex.EXPECTED_REMOVE_C1_UNCOVERED


# ---------------------------------------------------------------------------
# 暴力对拍：200 组随机小实例
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trial", range(200))
def test_random_matches_brute_force(trial):
    rng = random.Random(1000 + trial)
    n = rng.randint(1, 8)
    m = rng.randint(1, 6)
    pts = [Point(round(rng.random() * 10, 2), round(rng.random() * 10, 2))
           for _ in range(n)]
    spts = [Point(round(rng.random() * 10, 2), round(rng.random() * 10, 2))
            for _ in range(m)]
    radius = round(rng.uniform(1.0, 4.5), 2)
    forced = frozenset(rng.sample(range(m), rng.randint(0, min(2, m - 1)))) \
        if m >= 2 else frozenset()
    cov = build_coverage(pts, spts, radius)
    res = solve(SolveRequest(coverage=cov, forced=forced))
    expected = brute_force(
        [(i, p.x, p.y) for i, p in enumerate(pts)],
        [(j, p.x, p.y) for j, p in enumerate(spts)],
        radius, set(forced))

    if expected is None:
        assert res.status == "infeasible"
        reach = 0
        for cm in cov.by_station:
            reach |= cm
        assert tuple(sorted(res.uncovered)) == tuple(
            i for i in range(n) if not (reach >> i) & 1)
    else:
        assert res.status == "optimal"
        assert res.proven_optimal is True
        assert res.station_count == expected
        covered = 0
        for j in res.chosen:
            covered |= cov.by_station[j]
        assert covered == (1 << n) - 1
        assert set(forced) <= set(res.chosen)


@pytest.mark.parametrize("trial", range(50))
def test_duplicate_coordinates_do_not_change_count(trial):
    rng = random.Random(5000 + trial)
    n = rng.randint(2, 7)
    m = rng.randint(2, 5)
    base = [(rng.random() * 8, rng.random() * 8) for _ in range(n)]
    coords = base + [rng.choice(base) for _ in range(rng.randint(1, 3))]
    radius = round(rng.uniform(1.5, 4.0), 2)
    spts = [Point(rng.random() * 8, rng.random() * 8) for _ in range(m)]
    cov1 = build_coverage([Point(x, y) for x, y in base], spts, radius)
    cov2 = build_coverage([Point(x, y) for x, y in coords], spts, radius)
    a = solve(SolveRequest(coverage=cov1))
    b = solve(SolveRequest(coverage=cov2))
    assert a.station_count == b.station_count
    assert a.status == b.status


def test_forced_station_out_of_range_rejected():
    cov = clinic_coverage(3.0)
    with pytest.raises(ValueError):
        solve(SolveRequest(coverage=cov, forced=frozenset({99})))


# ---------------------------------------------------------------------------
# 超时 / 取消：构造纯组合难例（掩码直构，坐标保持唯一）
# 难例参数与构造统一放在 testutils.py
# ---------------------------------------------------------------------------

from testutils import HARD, comb_coverage  # noqa: E402


@pytest.fixture(scope="module")
def hard_optimal():
    cov = comb_coverage(**HARD)
    res = solve(SolveRequest(coverage=cov, time_limit=120.0))
    assert res.proven_optimal
    return res.station_count


def test_timeout_reports_valid_bounds_and_best(hard_optimal):
    cov = comb_coverage(**HARD)
    res = solve(SolveRequest(coverage=cov, time_limit=0.1))
    assert res.status == "timeout"
    assert res.proven_optimal is False
    # 下界不大于真实最优，当前最好不小于真实最优
    assert res.lower_bound <= hard_optimal <= res.station_count
    assert res.gap == res.station_count - res.lower_bound
    covered = 0
    for j in res.chosen:
        covered |= cov.by_station[j]
    assert covered == (1 << HARD["n"]) - 1  # 手上解必须真全覆盖


def test_near_zero_time_limit_still_returns_feasible():
    cov = comb_coverage(46, 46, 3, 6, 1)
    res = solve(SolveRequest(coverage=cov, time_limit=1e-12))
    assert res.status == "timeout"
    assert res.station_count is not None
    assert res.lower_bound <= res.station_count


def test_cancel_mid_run():
    cov = comb_coverage(**HARD)
    event = threading.Event()
    box = {}

    def go():
        box["r"] = solve(SolveRequest(coverage=cov, cancel_event=event))

    t = threading.Thread(target=go)
    t.start()
    event.set()
    t.join(timeout=30)
    res = box["r"]
    assert res.status == "cancelled"
    assert res.proven_optimal is False


def test_progress_callback_explored_advances():
    cov = comb_coverage(**HARD)
    seen = []
    res = solve(SolveRequest(
        coverage=cov, time_limit=0.2, progress_every=64,
        progress_cb=lambda p: seen.append(
            (p.explored_nodes, p.best_count, p.lower_bound))))
    assert res.status == "timeout"
    assert seen and seen[-1][0] > 0
    for _, best, lb in seen:
        assert lb <= (best if best is not None else 10 ** 9)


# ---------------------------------------------------------------------------
# 增量重解：与全量对拍
# ---------------------------------------------------------------------------

def test_incremental_matches_full_on_random_edits():
    rng = random.Random(777)
    radius = 6.0  # 居民点围绕候选站生成，任何增删序列始终全开可行
    box = 15.0
    stations = [(f"S{j}", rng.random() * box, rng.random() * box)
                for j in range(12)]

    def near_point():
        import math
        sx, sy = rng.choice(stations)[1:]
        a = rng.uniform(0, 2 * math.pi)
        d = radius * math.sqrt(rng.random()) * 0.9
        return sx + d * math.cos(a), sy + d * math.sin(a)

    residents = [(f"R{i}", *near_point()) for i in range(14)]

    cov0 = build_coverage(_points(residents), _points(stations), radius)
    base = solve(SolveRequest(coverage=cov0))
    assert base.status == "optimal"
    prior = PriorSolution(
        radius=radius,
        chosen_station_ids=tuple(base.chosen),
        residents=tuple(residents),
        stations=tuple(stations),
        forced=(),
    )

    current = list(residents)
    next_id = 1000
    for step in range(60):
        action = rng.choice(["add", "remove", "both"])
        if action in ("remove", "both") and len(current) > 5:
            current = [r for r in current
                       if r[0] not in {rng.choice(current)[0]}]
        if action in ("add", "both"):
            # 新点落在随机站附近（0.9 半径内），保证任何增删序列始终可行
            import math
            sx, sy = rng.choice(stations)[1:]
            a = rng.uniform(0, 2 * math.pi)
            d = radius * math.sqrt(rng.random()) * 0.9
            current.append((f"NEW{next_id}", sx + d * math.cos(a),
                            sy + d * math.sin(a)))
            next_id += 1

        cov = build_coverage(_points(current), _points(stations), radius)
        inc = solve_incremental(
            prior, cov, forced=frozenset(),
            current_radius=radius,
            current_residents=current,
            current_stations=stations,
        )
        full = solve(SolveRequest(coverage=cov))
        # 硬性要求：站数一致、状态一致
        assert inc.result.station_count == full.station_count, step
        assert inc.result.status == full.status, step
        if full.status == "optimal":
            assert inc.result.proven_optimal == full.proven_optimal
            covered = 0
            for j in inc.result.chosen:
                covered |= cov.by_station[j]
            assert covered == (1 << len(current)) - 1
            prior = PriorSolution(
                radius=radius,
                chosen_station_ids=tuple(inc.result.chosen),
                residents=tuple(current),
                stations=tuple(stations),
                forced=(),
            )
        else:
            assert False, f"半径 {radius} 下不应出现无解，step={step}"


def test_incremental_falls_back_to_cold_when_radius_changes():
    cov = clinic_coverage(3.0)
    base = solve(SolveRequest(coverage=cov))
    prior = PriorSolution(
        radius=3.0, chosen_station_ids=tuple(base.chosen),
        residents=tuple((r[0], r[1], r[2]) for r in ex.RESIDENTS),
        stations=tuple((s[0], s[1], s[2]) for s in ex.STATIONS),
        forced=())
    cov4 = clinic_coverage(4.0)
    out = solve_incremental(
        prior, cov4, forced=frozenset(), current_radius=4.0,
        current_residents=[(r[0], r[1], r[2]) for r in ex.RESIDENTS],
        current_stations=[(s[0], s[1], s[2]) for s in ex.STATIONS])
    assert out.strategy == "cold"
    assert out.result.station_count == 2


def test_incremental_warm_seed_used_when_compatible():
    cov = clinic_coverage(3.0)
    base = solve(SolveRequest(coverage=cov))
    prior = PriorSolution(
        radius=3.0, chosen_station_ids=tuple(base.chosen),
        residents=tuple((r[0], r[1], r[2]) for r in ex.RESIDENTS),
        stations=tuple((s[0], s[1], s[2]) for s in ex.STATIONS),
        forced=())
    # 加一个 C3 旁边的点
    residents = [(r[0], r[1], r[2]) for r in ex.RESIDENTS] + [("NEW", 5, 0.5)]
    cov2 = build_coverage(_points2(residents), _points2(ex.STATIONS), 3.0)
    out = solve_incremental(
        prior, cov2, forced=frozenset(), current_radius=3.0,
        current_residents=residents,
        current_stations=[(s[0], s[1], s[2]) for s in ex.STATIONS])
    assert out.strategy == "warm"
    assert out.result.status == "optimal"
    assert out.result.station_count == 3


def _points2(rows):
    return [Point(x, y) for _, x, y in rows]
