"""分期建设规划（phased construction）。

在一次性最少开站解之上，回答"钱分年拨"的问题：给定 T 期预算
（每期最多新建几座站），把站安排到各期，使得

  S_0 ⊆ S_1 ⊆ … ⊆ S_{T-1}，且 |S_t \\ S_{t-1}| ≤ budget[t]，

末期满覆盖全部居民点、必开站全部出现，并让早期累计照顾人口尽量大。

声明的优化目标（词典序；同时写进返回结果 objective 字段）
----------------------------------------------------------------
1. **总站数第一优先**：最终建成的站数必须等于（有已确认期时为
   "在已确认站不可更改前提下"的）最少站数，绝不为了前期好看而
   多建站。"每期贪当期最大"的纯贪心可能选出无法收进最少站数的
   站、从而多建站，本模块不走那条路。
2. **在总站数最少的前提下，按时间顺序词典序最大化各期累计照顾
   人口**：先最大化第 1 期，再在第 1 期最优下最大化第 2 期……
   早期人口具有词典序优先权。末期恒为总人口，不参与比较。

代价（明确写出）：与"每期独立贪心、允许超建"相比，本目标可能让
某一期的照顾人口小于贪心方案 —— 贪心多照顾的人口靠的是多建站；
本模块选择把"不多花总站数"放在前面。

正确性
------
- 最少站数直接复用 app.solver 的同一套分支定界（无权；权重不参与
  最少站数求解，因此加上权重后最少站数结果一个字不变）。
- 排顺序用本模块内的精确枚举（DFS + "可延展成最少覆盖"剪枝 +
  词典序乐观上界剪枝），搜完整棵树才把 proven_optimal 置 True；
  到时限只交出手上最好的可行计划并标未证明，绝不误标。
- 任何返回为可行的计划，每期新增集合、嵌套关系与末期满覆盖都在
  返回前用位掩码核验。

模块不依赖几何，只吃覆盖掩码；几何覆盖由 app.coverage 构建后传入。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from itertools import combinations

from .coverage import Coverage, Point
from .solver import (
    STATUS_INFEASIBLE,
    STATUS_OPTIMAL,
    SolveRequest,
    solve,
)

# 计划整体状态
OPT_OK = "optimal"             # 词典序最优且已完整证明
OPT_FEASIBLE = "feasible"      # 到时限交出的可行计划，未证明
OPT_INFEASIBLE = "infeasible"  # 全开也够不着（结构性无解，已证明）
OPT_BLOCKED = "cannot_close"   # 给定预算无论怎么排末期都收不了口

INF = 1 << 30

OBJECTIVE_DESC = (
    "词典序：先最小化总站数（等于无分期最少站数；有已确认期时为"
    "锁定前提下的最少站数），再按期次顺序最大化各期累计照顾人口；"
    "末期满覆盖、恒为总人口，不参与比较。代价：不允许为前期好看"
    "而超建，某一期的照顾人口可能小于允许超建的纯贪心方案。"
)


# ---------------------------------------------------------------------------
# 人口权重按位掩码求和（16 位分块预算表，O(位数/16)）
# ---------------------------------------------------------------------------

class WeightIndex:
    def __init__(self, weights: tuple[float, ...]):
        self.blocks: list[list[float]] = []
        for base in range(0, max(len(weights), 1), 16):
            w = weights[base:base + 16]
            sums = [0.0] * (1 << len(w))
            for v in range(1, len(sums)):
                lsb = v & -v
                sums[v] = sums[v ^ lsb] + w[lsb.bit_length() - 1]
            self.blocks.append(sums)
        self.total = float(sum(weights))

    def weight_of(self, mask: int) -> float:
        total = 0.0
        b = 0
        while mask:
            total += self.blocks[b][mask & 0xFFFF]
            mask >>= 16
            b += 1
        return total


def bits(mask: int):
    while mask:
        lsb = mask & -mask
        yield lsb.bit_length() - 1
        mask -= lsb


def _closing_blocked_period(budgets, k_total: int) -> int:
    """无锁定前缀时的卡点期。预算均为正、前缀容量单调增，纯容量不足
    必发生在末期；逐期核验给出语义明确的期号。"""
    return _closing_blocked_period_locked(budgets, 0, 0, k_total)


def _closing_blocked_period_locked(budgets, n_locked: int,
                                   locked_count: int,
                                   k_total: int) -> int:
    """有 n_locked 个已确认期（已实际建 locked_count 座）时的卡点期。

    已确认期内未用完的名额不可挪用；第 t（t>=n_locked）期末可用站数为
    locked_count + sum(budgets[n_locked:t+1])。返回最后一个该累计容量
    仍小于 k_total 的期。
    """
    free_cum = 0
    blocked = len(budgets) - 1
    for t in range(n_locked, len(budgets)):
        free_cum += budgets[t]
        if locked_count + free_cum < k_total:
            blocked = t
    return blocked


def mask_of(indices) -> int:
    m = 0
    for j in indices:
        m |= 1 << j
    return m


# ---------------------------------------------------------------------------
# 请求 / 结果
# ---------------------------------------------------------------------------

@dataclass
class PhaseRequest:
    # cover[j]：站 j 覆盖的居民点位掩码（原始居民点下标，不去重）
    cover: tuple[int, ...]
    weights: tuple[float, ...]
    budgets: tuple[int, ...]
    forced: frozenset[int] = frozenset()
    # 已确认（冻结）期从第 0 期起连续给出；locked[t] 为该期"新建"站集合，
    # 各期互不相交
    locked: tuple[frozenset[int], ...] = ()
    time_limit: float | None = None
    cancel_event: threading.Event | None = None


@dataclass
class PhaseResult:
    status: str
    feasible: bool
    proven_optimal: bool
    periods: list[dict] = field(default_factory=list)
    total_stations: int = 0
    minimum_stations: int | None = None   # 同必开约束下的无分期最少站数
    minimum_proven: bool = False
    extra_over_minimum: int | None = None
    total_population: float = 0.0
    blocked_period: int | None = None
    budget_shortfall: int = 0
    uncovered: tuple[int, ...] = ()
    explored_nodes: int = 0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "feasible": self.feasible,
            "proven_optimal": self.proven_optimal,
            "objective": OBJECTIVE_DESC,
            "periods": self.periods,
            "total_stations": self.total_stations,
            "minimum_stations": self.minimum_stations,
            "minimum_proven": self.minimum_proven,
            "extra_over_minimum": self.extra_over_minimum,
            "total_population": self.total_population,
            "blocked_period": self.blocked_period,
            "budget_shortfall": self.budget_shortfall,
            "uncovered": list(self.uncovered),
            "explored_nodes": self.explored_nodes,
        }


def _as_coverage(cover: tuple[int, ...], n: int) -> Coverage:
    """把掩码包成求解器要的 Coverage；居民点坐标全部唯一以避免几何折叠。

    求解器折叠后只用掩码重建一切，坐标不会再参与距离计算。
    """
    m = len(cover)
    coverers = [0] * n
    for j, cm in enumerate(cover):
        for i in bits(cm):
            coverers[i] |= 1 << j
    return Coverage(
        radius=1.0,
        residents=tuple(Point(float(i), 0.0) for i in range(n)),
        stations=tuple(Point(float(j), 0.0) for j in range(m)),
        by_station=tuple(cover),
        coverers=tuple(coverers),
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def plan_phased(req: PhaseRequest) -> PhaseResult:
    cover = tuple(req.cover)
    m = len(cover)
    weights = tuple(req.weights)
    n = len(weights)
    budgets = tuple(req.budgets)
    T = len(budgets)
    full = (1 << n) - 1
    all_stations = (1 << m) - 1
    forced_mask = mask_of(req.forced)
    locked = tuple(req.locked)
    widx = WeightIndex(weights)
    deadline = (time.perf_counter() + req.time_limit) if req.time_limit \
        else float("inf")

    def remaining_time():
        r = deadline - time.perf_counter()
        return r if r > 0 else None

    cover_cache: dict[int, int] = {}

    def cover_of(mask: int) -> int:
        v = cover_cache.get(mask)
        if v is None:
            v = 0
            for j in bits(mask):
                v |= cover[j]
            cover_cache[mask] = v
        return v

    # --- 1. 全开可达性（结构性无解，立即判定，无需搜索）-----------------
    reach = 0
    for cm in cover:
        reach |= cm
    unreachable = full & ~reach
    if unreachable:
        return PhaseResult(
            status=OPT_INFEASIBLE, feasible=False, proven_optimal=False,
            total_population=widx.total,
            uncovered=tuple(bits(unreachable)),
        )

    # --- 2. 已确认（冻结）期的新预算合法性 -------------------------------
    locked_masks: list[int] = []
    locked_union = 0
    for t, lset in enumerate(locked):
        lm = mask_of(lset)
        if lm & locked_union or lm & ~all_stations:
            raise ValueError("locked periods must be disjoint in-range sets")
        if lm.bit_count() > budgets[t]:
            # 已确认期的新建数本身就超过新预算：最早卡点就是该期
            return _blocked(
                cover, weights, widx, locked_masks + [lm], forced_mask,
                budgets, period=t,
                shortfall=lm.bit_count() - budgets[t],
                minimum=None, cover_of=cover_of, full=full)
        locked_masks.append(lm)
        locked_union |= lm

    # 必开站可出现在任意一期：未确认时由排顺序搜索安排；已确认前缀中
    # 尚未建成的必开站只能放进后续各期（终态断言保证末期全部建成）。

    # --- 3. 无分期最少站数（权重不参与；与旧服务同一求解器）-------------
    rt = remaining_time()
    base = solve(SolveRequest(
        coverage=_as_coverage(cover, n),
        forced=req.forced,
        time_limit=rt,
        cancel_event=req.cancel_event,
    ))
    if base.status == STATUS_INFEASIBLE:
        return PhaseResult(
            status=OPT_INFEASIBLE, feasible=False, proven_optimal=False,
            total_population=widx.total,
            uncovered=tuple(sorted(base.uncovered)),
        )
    k_star = base.station_count
    star_proven = base.status == STATUS_OPTIMAL

    # --- 4. 有已确认期时，求"锁定前提下"的最少站数 ----------------------
    if locked_union:
        work_forced = req.forced | frozenset(bits(locked_union))
        rt2 = remaining_time()
        work = solve(SolveRequest(
            coverage=_as_coverage(cover, n),
            forced=work_forced,
            time_limit=rt2,
            cancel_event=req.cancel_event,
        ))
        if work.status == STATUS_INFEASIBLE:
            return _blocked(
                cover, weights, widx, locked_masks, forced_mask,
                budgets, period=T - 1, shortfall=1,
                minimum=k_star if star_proven else None,
                cover_of=cover_of, full=full)
        k_total = work.station_count
        work_mask = mask_of(work.chosen)
        total_proven = star_proven and work.status == STATUS_OPTIMAL
    else:
        k_total = k_star
        work_mask = mask_of(base.chosen)
        total_proven = star_proven

    locked_count = locked_union.bit_count()
    n_prefix = len(locked_masks)
    # 已确认期内没用完的名额不能挪到以后。剩余站只能放进"锁定前缀之后"
    # 的各期。
    free_budget = sum(budgets[n_prefix:])

    # --- 5. 预算收口 ----------------------------------------------------
    # 口径：必建 k_total 座（含全部必开站）；前缀已锁 locked_count 座。
    # 后期共有 free_budget 个新建名额，必建的其余
    # k_total-locked_count 座（含尚未建的必开站）必须放得进。k_total
    # 未被证明时先不判死，转入下面的限时补证搜索（防止"收得了口却报
    # 收不了"）。
    capacity_ok = (k_total - locked_count) <= free_budget
    if not capacity_ok and total_proven:
        return _blocked(
            cover, weights, widx, locked_masks, forced_mask,
            budgets,
            period=_closing_blocked_period_locked(
                budgets, n_prefix, locked_count, k_total),
            shortfall=(k_total - locked_count) - free_budget,
            minimum=k_star if star_proven else None,
            cover_of=cover_of, full=full)

    # 未证明路径：用受限预算做一次"锁定前提下能否在自由预算内收口"的
    # 精确搜索。搜到 → 得到锁定前提下的精确最少站数；证伪 → 如实报
    # 收不了口；到时限 → 未证明收不了口（不伪装成功）。
    if not total_proven and not capacity_ok:
        probe = _SearchState(
            cover=cover, weights=weights, widx=widx, budgets=budgets,
            forced_mask=forced_mask, full=full, all_stations=all_stations,
            k_total=0, universe=all_stations, cover_of=cover_of,
            deadline=deadline, cancel_event=req.cancel_event,
        )
        # ext_min 只数自由站；后期名额要先扣除尚未建成的必开站
        mf_now = (forced_mask & ~locked_union).bit_count()
        free_slots = free_budget - mf_now
        need = probe.ext_min(locked_union, max(free_slots, 0))
        if probe.timed_out:
            return _unproven_blocked(
                cover, weights, widx, locked_masks, forced_mask, budgets,
                k_star if star_proven else None, cover_of, full)
        if mf_now > free_budget or need > free_slots:
            # 已证后期名额放不下（必开站本身就超，或补站后超）；卡点
            # 在末期（各期预算为正，前缀容量单调增）。
            k_impossible = locked_count + free_budget + 1
            return _blocked(
                cover, weights, widx, locked_masks, forced_mask,
                budgets,
                period=_closing_blocked_period_locked(
                    budgets, n_prefix, locked_count, k_impossible),
                shortfall=1,
                minimum=k_star if star_proven else None,
                cover_of=cover_of, full=full)
        free_picks = probe.ext_choice(locked_union, free_slots)
        work_mask = locked_union | forced_mask | mask_of(free_picks)
        k_total = work_mask.bit_count()
        total_proven = True

    # --- 6. 精确排顺序 --------------------------------------------------
    # 未证明的 k_total（求解器只交了 best）：把自由候选宇宙限制在该最少
    # 覆盖集合内排顺序，保证手上计划合法；必开站始终允许（它们本来就
    # 一定在任何可行方案里），最优性不标。
    universe = all_stations if total_proven else (work_mask | forced_mask)
    state = _SearchState(
        cover=cover, weights=weights, widx=widx, budgets=budgets,
        forced_mask=forced_mask, full=full, all_stations=all_stations,
        k_total=k_total, universe=universe, cover_of=cover_of,
        deadline=deadline, cancel_event=req.cancel_event,
    )
    seed = state.greedy_seed(locked_masks, work_mask)
    state.remember(seed)
    try:
        state.dfs(0, 0, [0] * T, locked_masks)
    except _DeadlineReached:
        pass
    per_masks = state.incumbent_masks
    if per_masks is None:
        per_masks = seed

    # --- 7. 组装 + 硬性核验 ---------------------------------------------
    periods: list[dict] = []
    built = 0
    for t, pm in enumerate(per_masks):
        new_bits = pm & ~built
        built |= pm
        cm = cover_of(built)
        periods.append({
            "period": t,
            "budget": budgets[t],
            "new_count": new_bits.bit_count(),
            "new": sorted(bits(new_bits)),
            "cumulative_count": built.bit_count(),
            "cumulative_mask": built,
            "covered_residents": sorted(bits(cm)),
            "covered_population": widx.weight_of(cm),
        })

    for t, p in enumerate(periods):
        assert p["new_count"] <= budgets[t], t
    # 已确认前缀：第 t 期新建集合必须逐位保留
    for t, lm in enumerate(locked_masks):
        assert mask_of(periods[t]["new"]) == lm
    assert cover_of(built) == full
    assert (forced_mask & built) == forced_mask

    total_built = built.bit_count()
    proven = total_proven and not state.timed_out
    status = OPT_OK if proven else OPT_FEASIBLE
    minimum = k_star if star_proven else None
    extra = (total_built - k_star) if star_proven else None
    return PhaseResult(
        status=status, feasible=True, proven_optimal=proven,
        periods=periods, total_stations=total_built,
        minimum_stations=minimum, minimum_proven=star_proven,
        extra_over_minimum=extra,
        total_population=widx.total,
        explored_nodes=state.nodes,
    )


# ---------------------------------------------------------------------------
# 收不了口：按预算尽量建站后列出仍然够不着的居民点
# ---------------------------------------------------------------------------

def _blocked_periods(cover, weights, widx, budgets, locked_masks,
                     forced_mask, full):
    """构造收不了口时的逐期尽力视图。

    顺序：已确认期原样；必开站优先占用后续各期名额（编号小的先）；
    剩余名额再按边际照顾人口贪心补自由站。各期新建数严格不超预算；
    必开站本身都放不下时，超出部分自然不会出现在视图里（最终 uncovered
    会如实反映收口失败）。
    """
    n_locked = len(locked_masks)
    built = 0
    periods = []
    # 已确认（锁定）期：新建集合固定
    for t in range(n_locked):
        new = locked_masks[t]
        built |= new
        cm = 0
        for j in bits(built):
            cm |= cover[j]
        periods.append({
            "period": t, "budget": budgets[t],
            "new_count": new.bit_count(),
            "new": sorted(bits(new)),
            "cumulative_count": built.bit_count(),
            "cumulative_mask": built,
            "covered_residents": sorted(bits(cm)),
            "covered_population": widx.weight_of(cm),
        })
    if len(budgets) > n_locked:
        # 先把尚未建的必开站按编号顺序塞进后续各期名额
        pending_forced = list(bits(forced_mask & ~built))
        per_masks = [0] * (len(budgets) - n_locked)
        fi = 0
        for u in range(len(per_masks)):
            cap = budgets[n_locked + u]
            while fi < len(pending_forced) and \
                    per_masks[u].bit_count() < cap:
                per_masks[u] |= 1 << pending_forced[fi]
                fi += 1
        # 再按边际人口贪心补自由站，填满剩余名额
        m = len(cover)
        for u in range(len(per_masks)):
            cap = budgets[n_locked + u]
            built |= per_masks[u]
            built_cover = 0
            for j in bits(built):
                built_cover |= cover[j]
            while per_masks[u].bit_count() < cap:
                unc = full & ~built_cover
                if not unc:
                    break
                best_j, best_gain = -1, 0.0
                for j in range(m):
                    if (built >> j) & 1:
                        continue
                    g = 0.0
                    for i in bits(cover[j] & unc):
                        g += weights[i]
                    if g > best_gain or (g == best_gain and g > 0 and
                                         (best_j < 0 or j < best_j)):
                        best_j, best_gain = j, g
                if best_j < 0:
                    break
                per_masks[u] |= 1 << best_j
                built |= 1 << best_j
                built_cover |= cover[best_j]
        # 组装逐期输出（running 已含锁定前缀，逐期并入新建掩码）
        running = 0
        for t0 in range(n_locked):
            running |= locked_masks[t0]
        for u, pm in enumerate(per_masks):
            t = n_locked + u
            running |= pm
            cm = 0
            for j in bits(running):
                cm |= cover[j]
            periods.append({
                "period": t, "budget": budgets[t],
                "new_count": pm.bit_count(),
                "new": sorted(bits(pm)),
                "cumulative_count": running.bit_count(),
                "cumulative_mask": running,
                "covered_residents": sorted(bits(cm)),
                "covered_population": widx.weight_of(cm),
            })
    return periods


def _blocked(cover, weights, widx, locked_masks, forced_mask, budgets,
             *, period, shortfall, minimum, cover_of, full) -> PhaseResult:
    locked_union = 0
    for lm in locked_masks:
        locked_union |= lm
    periods = _blocked_periods(cover, weights, widx, budgets, locked_masks,
                               forced_mask, full)
    reached = 0
    if periods:
        reached = cover_of(periods[-1]["cumulative_mask"])
    return PhaseResult(
        status=OPT_BLOCKED, feasible=False, proven_optimal=False,
        periods=periods,
        total_stations=periods[-1]["cumulative_count"] if periods
        else (locked_union | forced_mask).bit_count(),
        minimum_stations=minimum,
        minimum_proven=minimum is not None,
        total_population=widx.total,
        blocked_period=period,
        budget_shortfall=max(shortfall, 1),
        uncovered=tuple(bits(full & ~reached)),
    )


def _unproven_blocked(cover, weights, widx, locked_masks, forced_mask,
                      budgets, minimum, cover_of, full) -> PhaseResult:
    """到时限既没找到收口方案也没证明不存在：如实标未证明收不了口。"""
    periods = _blocked_periods(cover, weights, widx, budgets, locked_masks,
                               forced_mask, full)
    reached = cover_of(periods[-1]["cumulative_mask"]) if periods else 0
    return PhaseResult(
        status=OPT_BLOCKED, feasible=False, proven_optimal=False,
        periods=periods,
        total_stations=periods[-1]["cumulative_count"] if periods else 0,
        minimum_stations=minimum,
        minimum_proven=minimum is not None,
        total_population=widx.total,
        blocked_period=len(budgets) - 1,
        budget_shortfall=0,
        uncovered=tuple(bits(full & ~reached)),
    )


# ---------------------------------------------------------------------------
# 排顺序搜索
# ---------------------------------------------------------------------------

class _DeadlineReached(Exception):
    pass


class _SearchState:
    def __init__(self, *, cover, weights, widx, budgets, forced_mask,
                 full, all_stations, k_total, universe, cover_of,
                 deadline, cancel_event):
        self.cover = cover
        self.weights = weights
        self.widx = widx
        self.budgets = budgets
        self.T = len(budgets)
        self.forced_mask = forced_mask
        self.full = full
        self.all_stations = all_stations
        # 允许选用的站（已证明路径：全部站；未证明路径：手上最少覆盖集合）
        self.universe = universe
        self.k_total = k_total
        self.cover_of = cover_of
        self.deadline = deadline
        self.cancel_event = cancel_event
        self.nodes = 0
        self.timed_out = False
        # 最小补站数记忆化：key 已合并必开站；只缓存精确值与"无覆盖"INF
        self.ext_memo: dict[int, int] = {}
        self._coverers: list[int] = []
        for i in range(full.bit_count()):
            cs = 0
            for j, cm in enumerate(cover):
                if (cm >> i) & 1:
                    cs |= 1 << j
            self._coverers.append(cs)
        self.incumbent_masks: list[int] | None = None
        self.incumbent_vec: tuple[float, ...] | None = None

    def tick(self):
        self.nodes += 1
        if (self.nodes & 2047) == 0:
            if (self.cancel_event is not None and
                    self.cancel_event.is_set()) or \
                    time.perf_counter() >= self.deadline:
                self.timed_out = True
                raise _DeadlineReached

    # -- 最小"自由"补站数（key 必须已含全部必开站）----------------------
    # 必开站强制建成且不占自由名额；返回还需额外开几座非必开站。
    def ext_min(self, chosen_mask: int, cap: int) -> int:
        return self._ext(chosen_mask | self.forced_mask, cap)

    def _ext_exact(self, key: int) -> int:
        """key 已含全部必开站时的精确最少自由补站数（不可行为 INF）。"""
        memo = self.ext_memo
        if key in memo:
            return memo[key]
        unc = self.full & ~self.cover_of(key)
        if unc == 0:
            memo[key] = 0
            return 0
        avail = self.universe & ~key
        # 最大覆盖反推下界
        gains = [(self.cover[j] & unc).bit_count() for j in bits(avail)]
        gains = [g for g in gains if g]
        if not gains:
            memo[key] = INF
            return INF
        gains.sort(reverse=True)
        rows = unc.bit_count()
        cum = lb = 0
        for g in gains:
            cum += g
            lb += 1
            if cum >= rows:
                break
        else:
            memo[key] = INF
            return INF
        # 分支：可用覆盖者最少的未覆盖点（候选都是自由站：必开已在 key）
        pick = -1
        pick_deg = 1 << 30
        for i in bits(unc):
            deg = (self._coverers[i] & avail).bit_count()
            if deg < pick_deg:
                pick_deg, pick = deg, i
        cand = self._coverers[pick] & avail
        if cand == 0:
            memo[key] = INF
            return INF
        ordered = sorted(bits(cand),
                         key=lambda j: (-(self.cover[j] & unc).bit_count(), j))
        best = INF
        for j in ordered:
            sub = self._ext_exact(key | (1 << j))
            if sub < INF:
                v = 1 + sub
                if v < best:
                    best = v
                    if best == lb:
                        break
        memo[key] = best
        return best

    def _ext(self, key: int, cap: int) -> int:
        """精确补站数 <= cap 时返回该数，否则返回 INF（不缓存 cap 结论）。"""
        exact = self._ext_exact(key)
        return exact if exact <= cap else INF

    def ext_choice(self, chosen_mask: int, cap: int):
        """重建自由补站集合（不含必开站；chosen 已含已建站）。

        通过 DFS 精确找一组 <= cap 的自由站，与 chosen|forced 合起来
        满覆盖；找不到返回 None。
        """
        key = chosen_mask | self.forced_mask
        if self._ext(key, cap) > cap:
            return None

        def dfs(cur, remaining, picked):
            if self.full & ~self.cover_of(cur) == 0:
                return picked
            if remaining <= 0:
                return None
            free_avail = self.universe & ~cur & ~self.forced_mask
            # 选边际覆盖大的先试
            unc = self.full & ~self.cover_of(cur)
            ordered = sorted(bits(free_avail),
                             key=lambda j: (
                                 -(self.cover[j] & unc).bit_count(), j))
            for j in ordered:
                if self._ext(cur | (1 << j), remaining - 1) < INF:
                    res = dfs(cur | (1 << j), remaining - 1, picked + [j])
                    if res is not None:
                        return res
            return None

        return dfs(key, cap, [])

    # -- 贪心可行种子：每期"按边际人口选站 + 保证可延展" ----------------
    def greedy_seed(self, locked_masks, work_mask: int) -> list[int]:
        T = self.T
        per = [0] * T
        chosen = 0
        for t, lm in enumerate(locked_masks):
            per[t] = lm
            chosen |= lm
        n_lock = len(locked_masks)

        for t in range(n_lock, T):
            slots_after = sum(self.budgets[t + 1:]) if t + 1 < T else 0
            cap = self.budgets[t]
            if t == T - 1:
                missing_forced = self.forced_mask & ~chosen
                if missing_forced.bit_count() > cap:
                    break
                free = self.ext_choice(
                    chosen & ~self.forced_mask,
                    cap - missing_forced.bit_count())
                assert free is not None
                per[t] = (mask_of(free) | missing_forced) & ~chosen
                chosen |= per[t]
                continue
            remaining_total_now = (self.k_total - chosen.bit_count())
            min_total = max(0, remaining_total_now - slots_after)

            def later_feasible(cand_chosen):
                """后期（共 slots_after 个新建名额）能否收口。

                未建必开站必须在后期建成、占名额；其余自由补站数由
                ext_min 给出（它只数非必开站）。两者之和 <= slots_after
                才可行。
                """
                mf = (self.forced_mask & ~cand_chosen).bit_count()
                if mf > slots_after:
                    return False
                free_slots = slots_after - mf
                # ext_min 统计的是"在已合并必开站的前提下还需几座
                # 非必开站"；必开站在这里只占位、不要求它参与覆盖
                need_free = self._ext_exact(cand_chosen | self.forced_mask)
                return need_free <= free_slots

            picked_mask = 0
            added = 0

            def fill(target):
                """选恰好 target 座本期新建，使整体在后期可收口。

                按边际人口大的顺序回溯；每个候选组合只在最终成形时判一次
                可延展（中间态不要求），避免单步剪枝误杀"先甜后必需"的
                合法组合。
                """
                unc0 = self.full & ~self.cover_of(chosen)
                ordered = sorted(
                    bits(work_mask & ~chosen),
                    key=lambda j: (-self.widx.weight_of(
                        self.cover[j] & unc0), j))

                def rec(start, cur_picked, need):
                    if need == 0:
                        return cur_picked if later_feasible(
                            chosen | cur_picked) else None
                    for k in range(start, len(ordered)):
                        j = ordered[k]
                        # 粗剪：剩余总名额不能少于剩余总必建站数
                        if (self.k_total -
                                (chosen | cur_picked | (1 << j)).bit_count()) \
                                > slots_after + (need - 1):
                            continue
                        found = rec(k + 1, cur_picked | (1 << j), need - 1)
                        if found is not None:
                            return found
                    return None

                return rec(0, 0, target)

            picked = fill(min_total)
            assert picked is not None, ("greedy seed 无法凑足后期容量", t)
            picked_mask = picked
            added = picked.bit_count()
            # 有余量：在仍可延展前提下尽量多提前
            while added < cap:
                unc = self.full & ~self.cover_of(chosen | picked_mask)
                best_j = -1
                for j in sorted(bits(work_mask & ~(chosen | picked_mask)),
                                key=lambda j: (-self.widx.weight_of(
                                    self.cover[j] & unc), j)):
                    if later_feasible(chosen | picked_mask | (1 << j)):
                        best_j = j
                        break
                if best_j < 0:
                    break
                picked_mask |= 1 << best_j
                added += 1
            chosen |= picked_mask
            per[t] = picked_mask
        return per

    def objective_vec(self, per: list[int]) -> tuple[float, ...]:
        built = 0
        vec = []
        for t in range(self.T - 1):
            built |= per[t]
            vec.append(self.widx.weight_of(self.cover_of(built)))
        return tuple(vec)

    def remember(self, per: list[int]):
        vec = self.objective_vec(per)
        if self.incumbent_vec is None or vec > self.incumbent_vec:
            self.incumbent_vec = vec
            self.incumbent_masks = list(per)

    # -- 词典序乐观上界：prefix 到第 t-1 期已定；第 u 期（u>=t）乐观能再
    # 放 B_u = sum(budgets[t..u]) 座新站，忽略站间重叠取边际人口 top 和 --
    def upper_bound_vec(self, t: int, prefix_built: int,
                        per: list[int]) -> tuple[float, ...]:
        vec: list[float] = []
        built = 0
        for u in range(self.T - 1):
            if u < t:
                built |= per[u]
                vec.append(self.widx.weight_of(self.cover_of(built)))
                continue
            b_add = sum(self.budgets[t:u + 1])
            base_pop = self.widx.weight_of(self.cover_of(prefix_built))
            unc = self.full & ~self.cover_of(prefix_built)
            gains = sorted(
                (self.widx.weight_of(self.cover[j] & unc)
                 for j in bits(self.universe & ~prefix_built)),
                reverse=True)
            vec.append(base_pop + sum(gains[:b_add]))
        return tuple(vec)

    # -- 主 DFS -----------------------------------------------------------
    def dfs(self, t: int, chosen: int, per: list[int], locked_masks):
        self.tick()

        if t < len(locked_masks):
            lm = locked_masks[t]
            per[t] = lm
            self.dfs(t + 1, chosen | lm, per, locked_masks)
            return

        built_count = chosen.bit_count()
        slots_from_here = sum(self.budgets[t:])
        # 还需的"总建"站数（含未建必开站，它们不占自由名额但占总规模）
        remaining_total = self.k_total - built_count
        remaining_forced = (self.forced_mask & ~chosen).bit_count()
        remaining_free = remaining_total - remaining_forced
        if remaining_free < 0 or remaining_free > slots_from_here:
            return

        if t == self.T - 1:
            missing_forced = self.forced_mask & ~chosen
            if missing_forced.bit_count() > self.budgets[t]:
                return
            free_cap = self.budgets[t] - missing_forced.bit_count()
            free = self.ext_choice(chosen & ~self.forced_mask, free_cap)
            if free is None:
                return
            pm = mask_of(free) | missing_forced
            if (chosen | pm).bit_count() != self.k_total:
                return
            per[t] = pm & ~chosen
            self.remember(per)
            return

        slots_after = sum(self.budgets[t + 1:])
        # 本期恰好新建 s 座（含可能选中的必开站）是 WLOG；先枚举出全部
        # 可行的 s 座组合，再用 ext_min 判定可延展性。
        # 本期最少要新建几座：保证"剩余必建（占名额）+ 剩余自由站"
        # 放得进后期总名额
        s_min = max(0, remaining_total - slots_after)
        s_max = min(self.budgets[t], remaining_total)

        cand = [j for j in bits(self.universe & ~chosen)]
        # 边际人口大的先试，尽快拿到高质量 incumbent
        cand.sort(key=lambda j: (
            -self.widx.weight_of(
                self.cover[j] & (self.full & ~self.cover_of(chosen))), j))

        # 按本期新建数 s 从大到小枚举（更多的早期站在词典序上不更差，
        # 也更快拿到高质量 incumbent）；每个组合做可延展性检查。
        for s in range(s_max, s_min - 1, -1):
            for combo in combinations(cand, s):
                self.tick()
                cm = mask_of(combo)
                new_chosen = chosen | cm
                missing_forced_after = self.forced_mask & ~new_chosen
                # 后期总名额要先放得下尚未建的必开站，再放自由补站
                free_cap_after = slots_after - \
                    missing_forced_after.bit_count()
                if free_cap_after < 0:
                    continue
                need_free = self.ext_min(new_chosen, free_cap_after)
                if need_free > free_cap_after:
                    continue
                # 最终总规模必须恰好 k_total（不多建）
                if new_chosen.bit_count() + need_free + \
                        missing_forced_after.bit_count() != self.k_total:
                    continue
                per[t] = cm
                if self.incumbent_vec is not None:
                    ub = self.upper_bound_vec(t + 1, new_chosen, per)
                    if ub <= self.incumbent_vec:
                        continue
                self.dfs(t + 1, new_chosen, per, locked_masks)
