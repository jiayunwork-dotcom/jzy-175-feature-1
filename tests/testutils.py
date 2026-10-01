"""测试共用的难例构造：纯集合覆盖（掩码直构，坐标保持唯一以绕过几何去重）。"""

import random

from app.coverage import Coverage, Point


def comb_coverage(n, m, k_min, k_max, seed):
    rng = random.Random(seed)
    masks = [0] * m
    coverers = [0] * n
    for i in range(n):
        for j in rng.sample(range(m), rng.randint(k_min, k_max)):
            masks[j] |= 1 << i
            coverers[i] |= 1 << j
    return Coverage(
        1.0,
        tuple(Point(i, 0) for i in range(n)),
        tuple(Point(j, 0) for j in range(m)),
        tuple(masks), tuple(coverers))


# 一个 B&B 真正需要若干秒才能证明最优的实例（120 居民/60 站，3~6 覆盖）
HARD = dict(n=120, m=60, k_min=3, k_max=6, seed=0)
