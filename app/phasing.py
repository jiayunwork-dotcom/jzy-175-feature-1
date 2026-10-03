"""带人口权重的分期建设精确/启发式规划。

一份分期计划回答的问题：钱分 T 期拨（第 t 期最多新建 b_t 座站，
b_0..b_{T-1}），已建的不会拆（S_0 ⊆ S_1 ⊆ … ⊆ S_{T-1}），最后一期
必须覆盖全部居民点、必开站必须在 S_{T-1} 里——每期建哪几座，才能
在总站数尽量少的前提下，让早期覆盖的人口尽量多？

声明的优化目标（接口与文档里同名）
----------------------------------
**字典序：(总站数最少, 第 0 期累计人口最多, 第 1 期最多, …, 第 T-1 期最多)**

1. 第一优先永远是总站数 |S_{T-1}| 最小：这就是同一半径、同一必开约束下
   的无分期最少开站数 OPT（权重不改变覆盖关系，所以 OPT 与无权求解器
   完全一致，调用现有精确求解器得到）。我们**绝不会**为了前几年好看
   多建站；正常发起的计划 extra = 总站数 − OPT = 0。
2. 总站数打平时（通常就是 OPT 座），再按时间先后逐期最大化累计照顾
   人口（第 t 期人口 = S_t 覆盖的居民点权重之和）。"先照顾更多人"
   的诉求只在这一层发挥作用，且它**不能**突破第一层的总站数。
3. 仍有平局时取编号（字典序）最小的站集合，保证结果确定、可回归。

代价与适用范围：
- "先锁定最少站数"意味着：某个能早一期多盖一大片人的规划若需要
  OPT+1 座站，本服务不会选它；多建站只可能在"重排时前期已确认的站
  锁住了前缀、总预算又被迫大于 OPT"时出现，此时 extra>0 会如实报告。
- 小实例（组合数在阈值内）：候选最终集合与每期排布都做完整枚举，
  可标 proven_optimal；规模过大或撞上时限：交出手上最好的可行计划，
  标 proven_optimal=False，绝不冒充证明。

一个关键结构性质：预算是"最多建几座"而不是"必须建满"。提前放站
只会让累计覆盖不减，所以一座站要么尽早放在它第一次能带来新增人口的
期，要么（留给后续期仍能收口时）干脆往后放——枚举时用"剩余容量必须
容得下未排期站"和"当期累计人口上界追不上现任"两层剪枝，不必遍历
全部 (T+1)^m 种期号指派。

重排（确认若干期后调预算）：已确认各期的站组被锁死，搜索只在
"锁定前缀 + 自由后缀"上进行。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

from .coverage import Point, build_coverage
from .solver import SolveRequest, solve

OBJECTIVE = "lex_min_stations_then_lex_max_covered_population"

# 候选"最终站集合"枚举的最大组合数；超过则只采用求解器给出的最优解，
# 不再枚举同样站数的其它最终集合（计划仍可行，但不标已证明）。
DEFAULT_COVER_ENUM_CAP = 500_000
# 单次排布 DFS 的最大分支尝试数（枚举的"期×站组合"数；真正的热点是
# 同一层上 C(m,k) 个组合的展开，而不是递归深度）；超过则该最终集合
# 退回贪心排布，整个计划不标已证明。
DEFAULT_ALLOC_NODE_CAP = 200_000

STATUS_OPTIMAL = "optimal"            # 在声明目标下完整证明
STATUS_BEST = "best_feasible"         # 时限/规模截断，手上最好可行计划
STATUS_INCONCLUSIVE = "inconclusive"  # 时限内连可行计划都没能交出
STATUS_INFEASIBLE = "infeasible"      # 全开够不着 或 预算注定收不了口


# ---------------------------------------------------------------------------
# 请求 / 结果
# ---------------------------------------------------------------------------

@dataclass
class PhaseRequest:
    residents: list[dict]              # [{"id","x","y","weight"}]
    stations: list[dict]               # [{"id","x","y"}]
    radius: float
    budgets: list[int]                 # 每期最多新建站数
    forced_ids: list[str] = field(default_factory=list)
    time_limit: float | None = None
    # 已确认前缀（重排时给出）：每期新建站编号；长度 ≤ len(budgets)
    locked_groups: list[list[str]] = field(default_factory=list)
    # 同半径、同必开（不含锁定站）的无分期最少站数；重排时由外部传入，
    # 用于计算 extra_over_unphased。None 则按"含锁定求解值"报告。
    unphased_baseline: int | None = None
    cover_enum_cap: int = DEFAULT_COVER_ENUM_CAP
    alloc_node_cap: int = DEFAULT_ALLOC_NODE_CAP


@dataclass
class PhaseResult:
    status: str
    feasible: bool
    new_groups: list[list[str]]        # 每期新建站编号
    cum_groups: list[list[str]]        # 每期累计站编号
    cum_population: list[float]        # 每期累计照顾人口
    total_station_count: int | None
    total_population: float
    unphased_count: int | None         # 同约束无分期最少站数
    extra_over_unphased: int | None
    proven_optimal: bool
    objective: str
    infeasible_reason: str | None = None
    uncovered_ids: list[str] = field(default_factory=list)
    blocked_at_period: int | None = None   # 第几期（0 起）预算卡死
    budget_shortage: int | None = None     # 该期累计缺口多少座
    note: str | None = None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _make_weight_sum(weights: list[float]):
    """构造快速的掩码人口函数：按 8 位块分块查表。

    每 8 个居民点一张 256 项的块表（n 个居民点约 32n 个浮点，很省）；
    掩码逐块求和，避免热点路径上的逐位 Python 循环。
    """
    n = len(weights)
    blocks = []
    for base in range(0, n, 8):
        w = weights[base:base + 8]
        size = len(w)
        table = [0.0] * 256
        # 最后一个块可能不足 8 个居民点：高位权重按 0 处理（掩码里这些位
        # 本来也永远不会出现，这里只为把 256 项表填满）。
        for b in range(1, 256):
            lsb = b & -b
            idx = lsb.bit_length() - 1
            table[b] = table[b - lsb] + (w[idx] if idx < size else 0.0)
        blocks.append(table)

    def weight_sum(mask: int) -> float:
        s = 0.0
        for table in blocks:
            s += table[mask & 255]
            mask >>= 8
        return s

    return weight_sum


def _weight_sum(mask: int, weights: list[float]) -> float:
    s = 0.0
    while mask:
        lsb = mask & -mask
        s += weights[lsb.bit_length() - 1]
        mask -= lsb
    return s


def _comb_within_cap(n: int, k: int, cap: int) -> tuple[bool, int]:
    """C(n,k) 是否不超过 cap；连乘途中超了就提前返回 False。"""
    if k < 0 or k > n:
        return True, 0
    k = min(k, n - k)
    v = 1
    for i in range(1, k + 1):
        v = v * (n - k + i) // i
        if v > cap:
            return False, v
    return True, v


# ---------------------------------------------------------------------------
# 排布：最终站集合 F 已定，把各站排到各期（锁定前缀固定）
#
# 每座自由站要么在第 0..T-1 期之一新建，且第 t 期新建数 ≤ 预算_t。
# 注意：预算是"最多"，不是"必须花完"——若提前放站对当期人口毫无增益
# （能盖的人已被盖完），最优排布会把它留到后面的期。枚举时做两层剪枝：
#   1. 容量：剩余各期预算总和必须容得下还没排期的站；
#   2. 字典序下界：当期累计人口不可能再超过现任值时剪枝。
# ---------------------------------------------------------------------------

def best_allocation(final_ids: list[str], cover_of: dict[str, int],
                    weights: list[float], capacities: list[int],
                    locked_groups: list[list[str]],
                    node_cap: int, deadline: float | None
                    ) -> tuple[list[list[str]] | None, list[float], bool]:
    """固定最终集合 F 上的字典序最优排布。

    返回 (各期新建站组, 各期累计人口, 是否在 F 上完整证明)。
    访问节点数超 node_cap 或撞 deadline → 第三项 False（退回贪心）。
    """
    T = len(capacities)
    L = len(locked_groups)
    locked_set = {s for g in locked_groups for s in g}
    if not locked_set <= set(final_ids):
        return None, [], False
    free = [s for s in final_ids if s not in locked_set]

    # 各期还能放几座自由站（锁定前缀已占用对应期预算）
    free_caps = [capacities[t] - (len(locked_groups[t]) if t < L else 0)
                 for t in range(T)]
    # 锁定前缀必须容得下已锁定站
    if any(c < 0 for c in free_caps[:L]):
        return None, [], False
    suffix_cap = [0] * (T + 1)
    for t in range(T - 1, -1, -1):
        suffix_cap[t] = suffix_cap[t + 1] + free_caps[t]
    if suffix_cap[0] < len(free):
        return None, [], False

    weight_sum = _make_weight_sum(weights)

    best_groups: list[list[str]] | None = None
    best_pops: list[float] = []
    attempts = 0
    truncated = False

    def budget_exhausted() -> bool:
        nonlocal truncated
        if deadline is not None and time.perf_counter() >= deadline:
            truncated = True
            return True
        return False

    def pops_of(groups: list[list[str]]) -> list[float]:
        covered, pops = 0, []
        for g in groups:
            for s in g:
                covered |= cover_of[s]
            pops.append(weight_sum(covered))
        return pops

    def dfs(t: int, remaining: list[str], groups: list[list[str]],
            pops: list[float], covered: int):
        nonlocal best_groups, best_pops, attempts, truncated
        if budget_exhausted():
            return

        if t < L:
            # 已确认期：站组固定；自由站不允许出现在锁定前缀
            if remaining:
                # 容量剪枝：后面期数必须容得下所有剩余站
                if suffix_cap[L] < len(remaining):
                    return
            group = list(locked_groups[t])
            groups.append(group)
            for s in group:
                covered |= cover_of[s]
            pops.append(weight_sum(covered))
            if best_pops and pops[t] < best_pops[t] - 1e-12:
                groups.pop()
                pops.pop()
                return
            dfs(t + 1, remaining, groups, pops, covered)
            groups.pop()
            pops.pop()
            return

        if t == T:
            if remaining:
                return  # 还有站没排期（容量不足，剪枝兜底）
            sg = [sorted(g) for g in groups]
            if (best_groups is None or pops > best_pops
                    or (pops == best_pops
                        and sg < [sorted(g) for g in best_groups])):
                best_groups = [list(g) for g in groups]
                best_pops = list(pops)
            return

        # 容量剪枝：剩余站必须能被第 t..T-1 期装下
        if len(remaining) > suffix_cap[t]:
            return
        # 字典序上界剪枝：当期最多再放入 free_caps[t] 座边际人口最大的站，
        # 若这样累计人口仍追不上现任第 t 期，此分支无希望。
        if best_pops:
            prev_pop = pops[-1] if pops else 0.0
            gains = sorted(
                (weight_sum(cover_of[s] & ~covered) for s in remaining),
                reverse=True)
            ub_add = sum(gains[:free_caps[t]])
            if prev_pop + ub_add < best_pops[t] - 1e-12:
                return

        # 这一期新建 k 座，k = 0..min(预算, 剩余站数)；少建优先枚举，
        # 因为人口无增益时"往后放"是字典序平局下的编号最优方向。
        max_k = min(free_caps[t], len(remaining))
        # 还要保证选完 k 座后，剩余站能塞进 t+1..T-1 期
        min_k = max(0, len(remaining) - suffix_cap[t + 1])
        cand = sorted(remaining,
                      key=lambda s: (-weight_sum(cover_of[s] & ~covered), s))
        for k in range(min_k, max_k + 1):
            for combo in itertools.combinations(cand, k):
                attempts += 1
                if attempts > node_cap:
                    truncated = True
                    return
                if budget_exhausted():
                    return
                new_covered = covered
                for s in combo:
                    new_covered |= cover_of[s]
                new_pop = weight_sum(new_covered)
                if best_pops and new_pop < best_pops[t] - 1e-12:
                    continue
                groups.append(list(combo))
                pops.append(new_pop)
                nxt = [s for s in remaining if s not in combo]
                dfs(t + 1, nxt, groups, pops, new_covered)
                groups.pop()
                pops.pop()

    dfs(0, free, [], [], 0)
    if best_groups is None or truncated:
        return None, [], False
    return best_groups, best_pops, True


def greedy_allocation(final_ids: list[str], cover_of: dict[str, int],
                      weights: list[float], capacities: list[int],
                      locked_groups: list[list[str]]
                      ) -> list[list[str]]:
    """未证明路径的排布启发式：能提前增加人口的站才提前建。

    每期至多建预算那么多，但只在"还有正边际人口、且留给后续期足够容量
    收口"时才建；无增益的站留到后面。平局取编号最小者（确定性）。
    """
    T = len(capacities)
    L = len(locked_groups)
    groups = [list(g) for g in locked_groups]
    locked_set = {s for g in locked_groups for s in g}
    remaining = [s for s in final_ids if s not in locked_set]
    covered = 0
    for g in locked_groups:
        for s in g:
            covered |= cover_of[s]

    suffix_free_cap = [0] * (T + 1)
    for t in range(T - 1, L - 1, -1):
        suffix_free_cap[t] = suffix_free_cap[t + 1] + capacities[t]

    for t in range(L, T):
        group: list[str] = []
        max_now = min(capacities[t], len(remaining))
        # 至少要建几座，才能保证剩余站还能塞进 t+1..T-1 期
        must_now = max(0, len(remaining) - suffix_free_cap[t + 1])
        avail = list(remaining)
        while len(group) < max_now:
            useful = [s for s in avail
                      if (cover_of[s] & ~covered) != 0]
            # 无正增益的站：除容量所迫必须现在建外，一律留给后面
            if not useful and len(group) >= must_now:
                break
            pool = useful if useful else avail
            s = min(pool,
                    key=lambda x: (-_weight_sum(cover_of[x] & ~covered,
                                                weights), x))
            group.append(s)
            covered |= cover_of[s]
            avail.remove(s)
        for s in group:
            remaining.remove(s)
        groups.append(group)
    return groups


def cumulative_of(new_groups: list[list[str]], cover_of: dict[str, int],
                  weights: list[float]) -> tuple[list[list[str]], list[float]]:
    cum_groups, cum_pops, covered = [], [], 0
    seen: set[str] = set()
    for g in new_groups:
        seen |= set(g)
        for s in g:
            covered |= cover_of[s]
        cum_groups.append(sorted(seen))
        cum_pops.append(_weight_sum(covered, weights))
    return cum_groups, cum_pops


# ---------------------------------------------------------------------------
# 不可行结果构造
# ---------------------------------------------------------------------------

def _empty_periods(n: int) -> tuple[list, list, list]:
    return [[] for _ in range(n)], [[] for _ in range(n)], [0.0] * n


def _infeasible_unreachable(unreachable: list[str], T: int,
                            total_pop: float) -> PhaseResult:
    ng, cg, cp = _empty_periods(T)
    return PhaseResult(
        STATUS_INFEASIBLE, False, ng, cg, cp, None, total_pop,
        None, None, False, OBJECTIVE,
        infeasible_reason="全开所有候选站仍有居民点在服务半径之外",
        uncovered_ids=unreachable)


def _infeasible_budget(period: int, shortage: int, T: int,
                       total_pop: float,
                       unphased_count: int | None) -> PhaseResult:
    ng, cg, cp = _empty_periods(T)
    return PhaseResult(
        STATUS_INFEASIBLE, False, ng, cg, cp, None, total_pop,
        unphased_count, None, False, OBJECTIVE,
        infeasible_reason="按给定各期预算，无论怎么排最后一期都收不了口",
        blocked_at_period=period, budget_shortage=shortage)


def _blocking_period(budgets: list[int], need: int) -> tuple[int, int]:
    """累计预算第一次够不着 need 座的期（0 起）与当时缺口。"""
    cum = 0
    for t, b in enumerate(budgets):
        cum += b
        if cum < need:
            return t, need - cum
    return len(budgets) - 1, max(0, need - cum)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def plan_phases(req: PhaseRequest) -> PhaseResult:
    ids = [s["id"] for s in req.stations]
    id_set = set(ids)
    rids = [r["id"] for r in req.residents]
    weights = [float(r.get("weight", 1) or 1) for r in req.residents]
    total_pop = float(sum(weights))
    budgets = list(req.budgets)
    T = len(budgets)
    deadline = (time.perf_counter() + req.time_limit
                if req.time_limit and req.time_limit > 0 else None)

    def timed_out() -> bool:
        return deadline is not None and time.perf_counter() >= deadline

    # --- 0. 输入校验（业务级编号校验通常已在 HTTP 层做过，这里兜底）----
    if T <= 0:
        raise ValueError("budgets must be non-empty")
    if any(b < 0 for b in budgets):
        raise ValueError("budgets must be non-negative")
    unknown = [f for f in req.forced_ids if f not in id_set]
    if unknown:
        raise ValueError(f"unknown forced station ids: {unknown}")
    index = {sid: j for j, sid in enumerate(ids)}
    L = len(req.locked_groups)
    if L > T:
        raise ValueError("locked groups exceed number of periods")
    locked_ids = [s for g in req.locked_groups for s in g]
    bad = [s for s in locked_ids if s not in id_set]
    if bad:
        raise ValueError(f"unknown locked station ids: {bad}")
    if len(locked_ids) != len(set(locked_ids)):
        raise ValueError("a station appears in two locked periods")
    for t, g in enumerate(req.locked_groups):
        if len(g) > budgets[t]:
            return _infeasible_budget(t, len(g) - budgets[t], T, total_pop,
                                      None)

    # --- 1. 覆盖关系（权重只影响人口统计，不影响覆盖）------------------
    pts = [Point(r["x"], r["y"]) for r in req.residents]
    spts = [Point(s["x"], s["y"]) for s in req.stations]
    cov = build_coverage(pts, spts, req.radius)
    cover_of = {ids[j]: cov.by_station[j] for j in range(len(ids))}
    full_mask = (1 << len(rids)) - 1

    unreachable = [rids[i] for i in range(len(rids))
                   if cov.coverers[i] == 0]
    if unreachable:
        return _infeasible_unreachable(unreachable, T, total_pop)

    # --- 2. 无分期最少站数 OPT（权重完全不参与）------------------------
    forced_idx = frozenset(index[f] for f in req.forced_ids)
    locked_idx = frozenset(index[s] for s in locked_ids)
    sol = solve(SolveRequest(coverage=cov, forced=forced_idx | locked_idx,
                             time_limit=req.time_limit))
    total_budget = sum(budgets)
    if sol.status == "infeasible":
        chosen_mask = 0
        for j in forced_idx | locked_idx:
            chosen_mask |= cov.by_station[j]
        unc = [rids[i] for i in range(len(rids)) if not (chosen_mask >> i) & 1]
        ng, cg, cp = _empty_periods(T)
        return PhaseResult(
            STATUS_INFEASIBLE, False, ng, cg, cp, None, total_pop,
            None, None, False, OBJECTIVE,
            infeasible_reason="必开站/已确认站约束下不存在全覆盖方案",
            uncovered_ids=unc)

    # 无分期基准：全新计划即本次求解的最少站数；重排计划沿用创建时
    # 锁定的同约束最少站数（锁定站不参与"最少"的口径）。
    baseline = (req.unphased_baseline if req.unphased_baseline is not None
                else (sol.station_count if L == 0 else None))

    # 已证明最优且总预算注定不够 → 确定性收不了口
    if sol.proven_optimal and sol.station_count > total_budget:
        t, shortage = _blocking_period(budgets, sol.station_count)
        return _infeasible_budget(t, shortage, T, total_pop, baseline)

    # 没证明，手上界又超预算：下界能证明不可行就如实报，否则不可结论
    if sol.station_count is None or sol.station_count > total_budget:
        if sol.lower_bound > total_budget:
            t, shortage = _blocking_period(budgets, sol.lower_bound)
            return _infeasible_budget(t, shortage, T, total_pop, baseline)
        ng, cg, cp = _empty_periods(T)
        return PhaseResult(
            STATUS_INCONCLUSIVE, False, ng, cg, cp, None, total_pop,
            None, None, False, OBJECTIVE,
            note="时限内既未证明可行也未证明不可行，未交出计划")

    K = sol.station_count

    # --- 3. 枚举候选最终集合（含必开站 + 锁定站，大小恰为 K）----------
    mandatory = sorted(forced_idx | locked_idx)
    optional = [j for j in range(len(ids)) if j not in set(mandatory)]
    need = K - len(mandatory)
    within_cap, _ = _comb_within_cap(len(optional), need,
                                     req.cover_enum_cap)
    # 只有：求解器完整证明 + 同规模集合枚举量可控 + 没超时，才可能证最优
    exhaustive = bool(sol.proven_optimal and within_cap
                      and need >= 0 and not timed_out())

    best: dict | None = None  # {groups, pops, cum, ids}
    any_alloc_unproven = False

    def lex_key(groups: list[list[str]], pops: list[float]):
        return (tuple(pops[:-1]) if T > 1 else tuple(pops),
                tuple(sorted(s for g in groups for s in g)))

    def consider(final_idx: list[int]):
        nonlocal best, exhaustive, any_alloc_unproven
        if timed_out():
            exhaustive = False
        cover_mask = 0
        for j in final_idx:
            cover_mask |= cov.by_station[j]
        if cover_mask & full_mask != full_mask:
            return
        fids = [ids[j] for j in final_idx]
        groups, pops, exact = best_allocation(
            fids, cover_of, weights, budgets, req.locked_groups,
            req.alloc_node_cap, deadline)
        if groups is None:
            # 排布枚举超量/超时：退回贪心，且这一最终集合的排布未证明——
            # 它的真实最优排布有可能更好，故整个计划不能标已证明。
            exact = False
            groups = greedy_allocation(fids, cover_of, weights, budgets,
                                       req.locked_groups)
        if len(groups) != T:
            return
        flat = [s for g in groups for s in g]
        if len(flat) != len(set(flat)) or set(flat) != set(fids):
            return
        if any(len(g) > budgets[t] for t, g in enumerate(groups)):
            return
        if any(sorted(groups[t]) != sorted(g)
               for t, g in enumerate(req.locked_groups)):
            return
        cum, pops = cumulative_of(groups, cover_of, weights)
        key = lex_key(groups, pops)
        if not exact:
            any_alloc_unproven = True
            # 一旦某个覆盖集合的排布无法完整证明，整份计划就不可能再
            # 标已证明：没有必要再枚举其余同规模集合（只保留手上最优）。
            exhaustive = False
        if best is None or key < lex_key(best["groups"], best["pops"]):
            best = {"groups": groups, "pops": pops, "cum": cum,
                    "ids": tuple(sorted(flat))}

    # 3a. 求解器的最优可行集合总是候选
    consider(sorted(sol.chosen))

    # 3b. 同规模其它集合
    if exhaustive and not timed_out():
        for combo in itertools.combinations(optional, need):
            if timed_out():
                exhaustive = False
                break
            consider(mandatory + list(combo))

    if best is None:
        ng, cg, cp = _empty_periods(T)
        return PhaseResult(
            STATUS_INCONCLUSIVE, False, ng, cg, cp, None, total_pop,
            baseline, None, False, OBJECTIVE,
            note="未能在候选最终集合上构造出合法排布")

    if timed_out():
        exhaustive = False

    proven = bool(exhaustive and sol.proven_optimal
                  and not any_alloc_unproven)

    return PhaseResult(
        STATUS_OPTIMAL if proven else STATUS_BEST,
        True,
        [sorted(g) for g in best["groups"]],
        best["cum"],
        best["pops"],
        len(best["ids"]),
        total_pop,
        baseline,
        len(best["ids"]) - baseline if baseline is not None else None,
        proven,
        OBJECTIVE,
        note=None if proven else
        "组合规模或时限内未完成完整枚举：这是手上最好的可行计划，"
        "未证明为声明目标下的最优")


def result_to_dict(r: PhaseResult, budgets: list[int]) -> dict:
    return {
        "status": r.status,
        "feasible": r.feasible,
        "objective": r.objective,
        "periods": len(budgets),
        "budgets": list(budgets),
        "phases": [
            {"period": t,
             "new_station_ids": r.new_groups[t] if r.new_groups else [],
             "cumulative_station_ids": r.cum_groups[t] if r.cum_groups else [],
             "cumulative_population": (r.cum_population[t]
                                       if r.cum_population else 0.0)}
            for t in range(len(budgets))
        ],
        "total_station_count": r.total_station_count,
        "total_population": r.total_population,
        "unphased_min_station_count": r.unphased_count,
        "extra_over_unphased": r.extra_over_unphased,
        "proven_optimal": r.proven_optimal,
        "infeasible_reason": r.infeasible_reason,
        "uncovered_resident_ids": r.uncovered_ids,
        "blocked_at_period": r.blocked_at_period,
        "budget_shortage": r.budget_shortage,
        "note": r.note,
    }
