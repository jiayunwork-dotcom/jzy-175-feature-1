"""增量重解模块。

策略（详见 docs/algorithm.md）
------------------------------
采用"热启动（warm start）+ 完整分支定界证明"，而不是在旧解上局部修补
后直接宣布最优：

1. 把旧版本的最优开站集合作为初始可行种子交给精确求解器：
   - 旧站在新版本中仍存在（候选址、半径、必开约束一致）且对新居民点
     仍可行，求解器内部再贪心补齐，由此立刻得到一个高质量上界；
   - 随后分支定界照常完整运行到证明最优（或超时/取消）。
2. 覆盖关系不做差量缓存——每次用新数据重建。原因：覆盖构建是 O(n·m)，
   规模上千也是毫秒级，而它恰恰是"增量结果与全量对不上"最容易藏 bug
   的地方；把热启动放在求解器内部、由同一套剪枝/下界证明收尾，
   正确性与全量求解完全等价。

什么时候退回全量重解（prepare 返回 warm=None）：
- 半径发生变化（本系统半径挂在作业上，旧解覆盖关系不再适用）；
- 候选址发生增删或坐标变化（旧站下标可能已失效）；
- 必开站集合发生变化；
- 旧没有已证明的可行方案可复用；
- 旧站编号在新版本中找不到（防御性）。
即使热启动种子不可行，求解器内部也会退回从强制站出发的贪心初解，
正确性不受影响；prepare 层再兜一层异常冷启动（见 jobs 模块）。

硬性保证：增量路径与全量路径跑的是同一个精确求解器，差别仅在初始
上界，因此站数一致、且方案满足全覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass

from .coverage import Coverage
from .solver import SolveRequest, SolveResult, solve


@dataclass(frozen=True)
class PriorSolution:
    """上一版本可复用的求解结果（只需已证明最优的可行方案）。"""
    radius: float
    chosen_station_ids: tuple[int, ...]   # 候选站在"旧版本"中的下标
    residents: tuple                      # 旧版本居民点 (id, x, y)
    stations: tuple                       # 旧版本候选址 (id, x, y)
    forced: tuple[int, ...]


@dataclass
class IncrementalOutcome:
    result: SolveResult
    strategy: str          # "warm" | "cold"
    reused_station_ids: tuple[int, ...]


def prepare_warm(
    prior: PriorSolution | None,
    coverage: Coverage,
    current_radius: float,
    current_residents: list,
    current_stations: list,
    forced: frozenset[int],
) -> frozenset[int] | None:
    """判断旧解能否作为热启动种子；不能则返回 None（退回全量）。"""
    if prior is None:
        return None
    if current_radius != prior.radius:
        return None
    if list(current_stations) != list(prior.stations):
        return None
    if tuple(sorted(forced)) != tuple(sorted(prior.forced)):
        return None
    # 旧站下标必须都还在
    if any(j < 0 or j >= coverage.station_count for j in prior.chosen_station_ids):
        return None
    return frozenset(prior.chosen_station_ids)


def solve_incremental(
    prior: PriorSolution | None,
    coverage: Coverage,
    *,
    forced: frozenset[int],
    time_limit: float | None = None,
    cancel_event=None,
    progress_cb=None,
    current_radius: float,
    current_residents: list,
    current_stations: list,
) -> IncrementalOutcome:
    warm = prepare_warm(prior, coverage, current_radius, current_residents,
                        current_stations, forced)
    req = SolveRequest(
        coverage=coverage,
        forced=forced,
        time_limit=time_limit,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        warm_stations=warm,
    )
    result = solve(req)
    return IncrementalOutcome(
        result=result,
        strategy="warm" if warm is not None else "cold",
        reused_station_ids=tuple(sorted(warm)) if warm is not None else (),
    )
