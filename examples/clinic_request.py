#!/usr/bin/env python3
"""把诊所基准（app/examples.py）输出为创建版本用的 JSON 请求体。

用法：
    python examples/clinic_request.py                # 权重全 1（原基准）
    WEIGHTED=1 python examples/clinic_request.py     # 带人口权重（分期基准）
    python examples/clinic_request.py | curl -X POST .../versions \
        -H 'content-type: application/json' -d @-
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.examples import RESIDENTS, RESIDENT_WEIGHTS, STATIONS  # noqa: E402

weighted = os.environ.get("WEIGHTED") == "1"

if weighted:
    residents = [{"id": i, "x": x, "y": y, "weight": RESIDENT_WEIGHTS[i]}
                 for i, x, y in RESIDENTS]
    note = "诊所选址带权分期基准（总人口 56；r=3 无分期仍为 {C1,C2,C3}）"
else:
    residents = [{"id": i, "x": x, "y": y} for i, x, y in RESIDENTS]
    note = "诊所选址回归基准（r=3 最优 {C1,C2,C3} 共 3 座）"

payload = {
    "change_note": note,
    "residents": residents,
    "stations": [{"id": i, "x": x, "y": y} for i, x, y in STATIONS],
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
