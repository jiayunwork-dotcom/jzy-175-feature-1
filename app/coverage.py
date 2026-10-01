"""覆盖关系构建模块。

给定居民点、候选站址与统一服务半径，构建两类位掩码：
- residents_by_station[j]：站 j 能覆盖的居民点集合
- coverers_of_resident[i]：能覆盖居民点 i 的站集合

坐标重复的居民点共享同一组覆盖关系（这也是"重复坐标不影响站数"
性质在求解器里自然成立的原因）。距离固定为平面欧氏距离。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Point:
    x: float
    y: float


def euclidean(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


@dataclass(frozen=True)
class Coverage:
    radius: float
    residents: tuple[Point, ...]
    stations: tuple[Point, ...]
    # residents_by_station[j]：第 j 个站覆盖的居民点（位掩码，位 i 对应第 i 个居民点）
    by_station: tuple[int, ...]
    # coverers[i]：能覆盖第 i 个居民点的站（位掩码，位 j 对应第 j 个站）
    coverers: tuple[int, ...]

    @property
    def resident_count(self) -> int:
        return len(self.residents)

    @property
    def station_count(self) -> int:
        return len(self.stations)


def build_coverage(residents: list[Point], stations: list[Point], radius: float) -> Coverage:
    r2 = radius * radius
    n = len(residents)
    m = len(stations)
    by_station = [0] * m
    coverers = [0] * n
    for j, s in enumerate(stations):
        mask = 0
        sx, sy = s.x, s.y
        for i, rp in enumerate(residents):
            dx = sx - rp.x
            dy = sy - rp.y
            if dx * dx + dy * dy <= r2:
                mask |= 1 << i
                coverers[i] |= 1 << j
        by_station[j] = mask
    return Coverage(
        radius=radius,
        residents=tuple(residents),
        stations=tuple(stations),
        by_station=tuple(by_station),
        coverers=tuple(coverers),
    )
