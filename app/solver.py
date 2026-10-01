"""精确求解器：带强制开站约束的集合覆盖问题（分支定界）。

问题是经典的无权集合覆盖（NP 难）：站为"列"，居民点为"行"，
每座站覆盖与它欧氏距离不超过半径的居民点，求覆盖全部居民点所需的
最少开站集合，并支持若干座站"非建不可"。

"是否已证明最优"有严格含义：只有当分支定界把整棵搜索树探索完
（或全部剩余活节点的下界都已不劣于当前最好解）时，才标记
proven_optimal=True。超时或取消时即便当前解恰好等于最优，也绝不
标记为已证明最优。

主要机制
--------
1. 坐标去重：同坐标居民点折叠为一个唯一居民点，重复坐标自然不影响站数。
2. 支配列消除：站 j 的覆盖集是站 k 的子集时 j 可安全删除（强制站永不
   删除）。只在根节点做一次，这是保持最优解不变的安全归约。
3. 强制传播：某未覆盖居民点只剩唯一可选站时，该站必选。
4. 两个合法下界取大：
   - 打包下界：两两不共享可用站的居民点至少各需一座站；
   - 最大覆盖反推：s 座站至多覆盖前 s 大覆盖增益之和，凑不够行数就还得加。
   任意时刻报告的 lower_bound 都是对"还需开几座站"的合法下界，
   故超时交出的 lower_bound ≤ 真实最优，current best 是可行解（≥ 最优）。
5. 分支：选可用覆盖站最少的居民点，对能覆盖它的站逐一"选入"分支
   （include-only k 叉分支：不选某站由"选了另一座"的兄弟分支隐含，
   搜索仍完整）。
6. DFS + 各帧兄弟节点的 LB 小顶堆：内存占用线性可控，全局下界取所有
   未处理兄弟节点 LB 的最小值（合法且尽量紧）。
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .coverage import Coverage

# 作业状态（与持久化层保持一致）
STATUS_OPTIMAL = "optimal"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
STATUS_INFEASIBLE = "infeasible"


@dataclass
class Progress:
    explored_nodes: int = 0
    best_count: int | None = None
    lower_bound: int = 0
    status: str = "running"


@dataclass
class SolveRequest:
    coverage: Coverage
    # 强制开站：候选站下标集合（原始下标，0 起）
    forced: frozenset[int] = frozenset()
    time_limit: float | None = None
    cancel_event: threading.Event | None = None
    progress_cb: Callable[[Progress], None] | None = None
    # 热启动：一个已知/期望可行的开站下标集合，仅用作初始上界，绝不影响正确性
    warm_stations: frozenset[int] | None = None
    progress_every: int = 256


@dataclass
class SolveResult:
    status: str
    chosen: tuple[int, ...]            # 选中的候选站下标（原始下标）
    station_count: int | None          # 当前最好可行解站数；无解为 None
    lower_bound: int                   # 已证明的合法下界
    gap: int | None                    # station_count - lower_bound
    explored_nodes: int
    uncovered: tuple[int, ...] = ()    # 无解时：够不着的居民点（原始下标）
    proven_optimal: bool = False

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "chosen": list(self.chosen),
            "station_count": self.station_count,
            "lower_bound": self.lower_bound,
            "gap": self.gap,
            "explored_nodes": self.explored_nodes,
            "uncovered": list(self.uncovered),
            "proven_optimal": self.proven_optimal,
        }


# ---------------------------------------------------------------------------
# 贪心初始解（合法上界；绝不会被当作最优）
# ---------------------------------------------------------------------------

def _greedy(cover: Sequence[int], coverers: Sequence[int], uncovered: int,
            avail: int, chosen: int, chosen_cnt: int):
    """在已选 chosen 基础上贪心补站。返回 (总数, 选中掩码)；补不齐返回 None。"""
    add_mask = 0
    while uncovered:
        best_j = -1
        best_gain = 0
        bits = avail & ~add_mask
        while bits:
            lsb = bits & -bits
            j = lsb.bit_length() - 1
            bits -= lsb
            gain = (cover[j] & uncovered).bit_count()
            if gain > best_gain:  # 平局取下标小的（扫描序由小到大）
                best_gain = gain
                best_j = j
        if best_j < 0:
            return None
        add_mask |= 1 << best_j
        uncovered &= ~cover[best_j]
    return chosen_cnt + add_mask.bit_count(), chosen | add_mask


# ---------------------------------------------------------------------------
# 下界
# ---------------------------------------------------------------------------

def _packing_lb(cover: Sequence[int], coverers: Sequence[int],
                uncovered: int, avail: int) -> int:
    """两两不共享可用站的居民点打包数：每个被计入的居民点必须由不同站覆盖。"""
    rem = uncovered
    cnt = 0
    while rem:
        bits = rem
        best_i = -1
        best_deg = 1 << 30
        while bits:
            lsb = bits & -bits
            i = lsb.bit_length() - 1
            bits -= lsb
            deg = (coverers[i] & avail).bit_count()
            if deg < best_deg:
                best_deg = deg
                best_i = i
        cnt += 1
        # 删除所有与 best_i 共享任一可用站的居民点（它们可能由同一座站覆盖）
        cs = coverers[best_i] & avail
        blocked = 0
        while cs:
            lsb = cs & -cs
            j = lsb.bit_length() - 1
            cs -= lsb
            blocked |= cover[j]
        rem &= ~blocked
    return cnt


def _maxcov_lb(cover: Sequence[int], uncovered: int, avail: int) -> int:
    """最大覆盖反推下界：s 座站至多覆盖前 s 大增益之和，凑不够行数就还得加站。"""
    rows = uncovered.bit_count()
    if rows == 0:
        return 0
    gains: list[int] = []
    bits = avail
    while bits:
        lsb = bits & -bits
        j = lsb.bit_length() - 1
        bits -= lsb
        g = (cover[j] & uncovered).bit_count()
        if g:
            gains.append(g)
    gains.sort(reverse=True)
    cum = 0
    s = 0
    for g in gains:
        cum += g
        s += 1
        if cum >= rows:
            return s
    # 合起来也盖不住：本子问题不可行（reduce_node 通常已先行截掉）
    return rows + 1


# ---------------------------------------------------------------------------
# DFS 帧：某一层尚未展开的兄弟节点，按 LB 升序暴露
# ---------------------------------------------------------------------------

class _Frame:
    __slots__ = ("fid", "entries", "cur")

    def __init__(self, fid: int, entries: list[tuple[int, tuple]]):
        # entries: (lb, node)，node=(uncovered, avail, chosen, k)
        self.fid = fid
        entries.sort(key=lambda e: e[0])
        self.entries = entries
        self.cur = 0

    def exhausted(self) -> bool:
        return self.cur >= len(self.entries)

    def head(self) -> tuple[int, tuple] | None:
        if self.exhausted():
            return None
        return self.entries[self.cur]


def solve(req: SolveRequest) -> SolveResult:
    cov = req.coverage
    m = cov.station_count
    forced = set(req.forced)
    if forced - set(range(m)):
        raise ValueError("forced station index out of range")

    deadline = float("inf")
    if req.time_limit is not None and req.time_limit > 0:
        deadline = time.perf_counter() + req.time_limit

    def timed_out() -> bool:
        return time.perf_counter() >= deadline

    # --- 1. 同坐标居民点折叠 -------------------------------------------
    uniq_points = []
    orig_of_uniq: list[list[int]] = []
    coord_index: dict[tuple[float, float], int] = {}
    for i, p in enumerate(cov.residents):
        key = (p.x, p.y)
        if key in coord_index:
            orig_of_uniq[coord_index[key]].append(i)
        else:
            coord_index[key] = len(uniq_points)
            uniq_points.append(p)
            orig_of_uniq.append([i])
    n = len(uniq_points)

    if n == 0:
        return SolveResult(STATUS_OPTIMAL, (), 0, 0, 0, 0, (), True)

    # 在唯一居民点上重建掩码
    cover = [0] * m
    for j, mask in enumerate(cov.by_station):
        cm = 0
        for ui, origs in enumerate(orig_of_uniq):
            if mask & (1 << origs[0]):
                cm |= 1 << ui
        cover[j] = cm
    coverers = [0] * n
    for j, cm in enumerate(cover):
        t = cm
        while t:
            lsb = t & -t
            ui = lsb.bit_length() - 1
            t -= lsb
            coverers[ui] |= 1 << j

    all_residents = (1 << n) - 1

    # --- 2. 全开也够不着 → 无解 ----------------------------------------
    unreachable = [ui for ui in range(n) if coverers[ui] == 0]
    if unreachable:
        uncovered_orig = tuple(sorted(
            i for ui in unreachable for i in orig_of_uniq[ui]
        ))
        return SolveResult(STATUS_INFEASIBLE, (), None, 0, None, 0,
                           uncovered_orig, False)

    # --- 3. 支配列消除（强制站保留）------------------------------------
    forced_mask = 0
    for j in forced:
        forced_mask |= 1 << j
    order = sorted(range(m), key=lambda j: (-cover[j].bit_count(), j))
    kept: list[int] = []
    for j in order:
        if forced_mask & (1 << j):
            kept.append(j)
            continue
        cj = cover[j]
        dominated = False
        for k in kept:
            if (cj | cover[k]) == cover[k]:  # cj ⊆ cover[k]
                dominated = True
                break
        if not dominated:
            kept.append(j)
    kept.sort()

    q = len(kept)
    orig_of_q = kept
    q_of_orig = {orig: qi for qi, orig in enumerate(kept)}
    cover_q = [cover[o] for o in kept]
    coverers_q = [0] * n
    for qi, cm in enumerate(cover_q):
        t = cm
        while t:
            lsb = t & -t
            ui = lsb.bit_length() - 1
            t -= lsb
            coverers_q[ui] |= 1 << qi
    forced_q = 0
    for o in forced:
        forced_q |= 1 << q_of_orig[o]
    all_q = (1 << q) - 1

    forced_cover = 0
    t = forced_q
    while t:
        lsb = t & -t
        qi = lsb.bit_length() - 1
        t -= lsb
        forced_cover |= cover_q[qi]

    def back_to_orig(mask_q: int) -> tuple[int, ...]:
        out: list[int] = []
        tt = mask_q
        while tt:
            lsb = tt & -tt
            qi = lsb.bit_length() - 1
            tt -= lsb
            out.append(orig_of_q[qi])
        out.sort()
        return tuple(out)

    # --- 4. 初始可行上界：贪心 + 热启动 --------------------------------
    def finish(seed_mask: int):
        """从开站种子出发补齐全覆盖（补不齐返回 None）。"""
        covered = 0
        tt = seed_mask
        while tt:
            lsb = tt & -tt
            qi = lsb.bit_length() - 1
            tt -= lsb
            covered |= cover_q[qi]
        unc = all_residents & ~covered
        av = all_q & ~seed_mask
        return _greedy(cover_q, coverers_q, unc, av, seed_mask,
                       seed_mask.bit_count())

    best: int | None = None
    best_mask = 0
    g = finish(forced_q)
    if g is not None:
        best, best_mask = g

    if req.warm_stations is not None:
        warm_q = 0
        for o in req.warm_stations:
            qi = q_of_orig.get(o)
            if qi is None:
                warm_q = -1
                break
            warm_q |= 1 << qi
        if warm_q >= 0 and (forced_q & ~warm_q) == 0:
            w = finish(warm_q)
            if w is not None and (best is None or w[0] < best):
                best, best_mask = w

    explored = 0

    def reduce_node(uncovered: int, avail: int, chosen: int, k: int):
        """单选站强制传播；发现不可行返回 None。"""
        while True:
            bits = uncovered
            forced_here = 0
            while bits:
                lsb = bits & -bits
                ui = lsb.bit_length() - 1
                bits -= lsb
                cs = coverers_q[ui] & avail
                if cs == 0:
                    return None
                if cs & (cs - 1) == 0:
                    forced_here = cs
                    break
            if not forced_here:
                return uncovered, avail, chosen, k
            qi = forced_here.bit_length() - 1
            uncovered &= ~cover_q[qi]
            avail &= ~forced_here
            chosen |= forced_here
            k += 1

    root_uncovered = all_residents & ~forced_cover
    root = reduce_node(root_uncovered, all_q & ~forced_q, forced_q,
                       forced_q.bit_count())
    root_k = forced_q.bit_count()

    if root is None:
        # 防御性：与第 2 步的 reachable 检查理论上不会同时不一致
        return SolveResult(STATUS_INFEASIBLE, (), None, 0, None, explored,
                           tuple(range(len(cov.residents))), False)

    ru, ra, rc, rk = root
    if ru == 0:
        return SolveResult(STATUS_OPTIMAL, back_to_orig(rc), rk, rk, 0,
                           explored, (), True)

    root_lb_add = max(
        _packing_lb(cover_q, coverers_q, ru, ra),
        _maxcov_lb(cover_q, ru, ra),
    )
    root_lb = rk + root_lb_add
    if best is not None:
        root_lb = min(root_lb, best)

    def current_global_lb(default: int) -> int:
        while gheap:
            lb0, fid0, idx0 = gheap[0]
            fr = frame_by_id.get(fid0)
            if fr is None or idx0 != fr.cur:
                heapq.heappop(gheap)
                continue
            return lb0
        return default

    def report():
        if req.progress_cb is not None:
            req.progress_cb(Progress(
                explored_nodes=explored,
                best_count=best,
                lower_bound=current_global_lb(root_lb),
                status="running",
            ))

    # 根下界已等于当前最好 → 无需分支即可证明最优
    if best is not None and root_lb >= best:
        return SolveResult(STATUS_OPTIMAL, back_to_orig(best_mask), best,
                           best, 0, explored, (), True)

    # 超时点：根下界算完、尚未分支（time_limit<=0 时在此立即交出贪心解）
    if timed_out():
        return _truncated(STATUS_TIMEOUT, best, best_mask, root_lb, explored,
                          back_to_orig)
    if req.cancel_event is not None and req.cancel_event.is_set():
        return _truncated(STATUS_CANCELLED, best, best_mask, root_lb, explored,
                          back_to_orig)

    # --- 5. 分支定界主循环 ---------------------------------------------
    fid_counter = itertools.count()
    stack: list[_Frame] = []
    frame_by_id: dict[int, _Frame] = {}
    gheap: list[tuple[int, int, int]] = []  # (lb, fid, entry_idx)

    def make_frame(node) -> _Frame:
        """对 node 分支：叶节点立即更新 best，其余预归约、算 LB 后入帧。"""
        nonlocal best, best_mask
        uncovered, avail, chosen, k = node
        # 选可用覆盖站最少的未覆盖居民点；平局选"覆盖它的站能盖住的居民并集"大的
        bits = uncovered
        pick = -1
        pick_deg = 1 << 30
        pick_union = -1
        while bits:
            lsb = bits & -bits
            ui = lsb.bit_length() - 1
            bits -= lsb
            cs = coverers_q[ui] & avail
            deg = cs.bit_count()
            union = 0
            t2 = cs
            while t2:
                b2 = t2 & -t2
                qi2 = b2.bit_length() - 1
                t2 -= b2
                union |= cover_q[qi2]
            usz = (union & uncovered).bit_count()
            if deg < pick_deg or (deg == pick_deg and usz > pick_union):
                pick_deg, pick, pick_union = deg, ui, usz
        cand = coverers_q[pick] & avail
        entries: list[tuple[int, tuple]] = []
        t = cand
        while t:
            lsb = t & -t
            qi = lsb.bit_length() - 1
            t -= lsb
            child = reduce_node(
                uncovered & ~cover_q[qi],
                avail & ~(1 << qi),
                chosen | (1 << qi),
                k + 1,
            )
            if child is None:
                continue
            cu, ca, cc, ck = child
            if cu == 0:
                if best is None or ck < best:
                    best, best_mask = ck, cc
                continue
            if best is not None and ck >= best:
                continue
            lb_add = max(
                _packing_lb(cover_q, coverers_q, cu, ca),
                _maxcov_lb(cover_q, cu, ca),
            )
            lb = min(ck + lb_add, best) if best is not None else ck + lb_add
            if best is not None and lb >= best:
                continue
            entries.append((lb, (cu, ca, cc, ck)))
        fid = next(fid_counter)
        return _Frame(fid, entries)

    root_frame = make_frame((ru, ra, rc, rk))
    if not root_frame.exhausted():
        stack.append(root_frame)
        frame_by_id[root_frame.fid] = root_frame
        h = root_frame.head()
        heapq.heappush(gheap, (h[0], root_frame.fid, 0))
    elif best is None:
        # 根的所有分支都不可行，且贪心也失败 → 无解
        return SolveResult(STATUS_INFEASIBLE, (), None, 0, None, explored,
                           tuple(range(len(cov.residents))), False)
    else:
        # 根分支全部以叶/剪枝结束：搜索已穷尽
        return SolveResult(STATUS_OPTIMAL, back_to_orig(best_mask), best,
                           best, 0, explored, (), True)

    terminal: str | None = None
    while terminal is None:
        if timed_out():
            terminal = STATUS_TIMEOUT
            break
        if req.cancel_event is not None and req.cancel_event.is_set():
            terminal = STATUS_CANCELLED
            break

        explored += 1

        if not stack:
            terminal = STATUS_OPTIMAL
            break

        # 全部活节点 LB 都不劣于当前最好 → 已证明最优
        if best is not None:
            glb = current_global_lb(root_lb)
            if glb >= best:
                terminal = STATUS_OPTIMAL
                break

        top = stack[-1]
        head = top.head()
        if head is None:
            stack.pop()
            frame_by_id.pop(top.fid, None)
            continue

        lb, node = head
        top.cur += 1
        nxt = top.head()
        if nxt is not None:
            heapq.heappush(gheap, (nxt[0], top.fid, top.cur))

        child_frame = make_frame(node)
        if not child_frame.exhausted():
            stack.append(child_frame)
            frame_by_id[child_frame.fid] = child_frame
            ch = child_frame.head()
            heapq.heappush(gheap, (ch[0], child_frame.fid, 0))

        if explored % req.progress_every == 0:
            report()

    report()

    if terminal == STATUS_OPTIMAL and best is not None:
        return SolveResult(STATUS_OPTIMAL, back_to_orig(best_mask), best,
                           best, 0, explored, (), True)

    glb = current_global_lb(root_lb)
    return _truncated(terminal, best, best_mask, glb, explored, back_to_orig)


def _truncated(status: str | None, best, best_mask, glb, explored,
               back_to_orig) -> SolveResult:
    """构造超时/取消结果：保证 LB ≤ best ≤ 真实……LB 合法、best 可行。"""
    if best is None:
        return SolveResult(status or STATUS_TIMEOUT, (), None, glb, None,
                           explored, (), False)
    lb = min(glb, best)
    return SolveResult(
        status or STATUS_TIMEOUT,
        back_to_orig(best_mask),
        best,
        lb,
        best - lb,
        explored,
        (),
        False,
    )
