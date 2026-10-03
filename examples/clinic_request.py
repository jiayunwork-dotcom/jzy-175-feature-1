#!/usr/bin/env python3
"""把诊所基准（app/examples.py）输出为创建版本用的 JSON 请求体。

默认带上分期基准的人口权重（WEIGHTS，未列出的点按 1）；
加 --plain 输出不带 weight 的旧格式（按缺省权重 1 处理）。

用法：
    python examples/clinic_request.py
    python examples/clinic_request.py --plain
    python examples/clinic_request.py | curl -X POST .../versions \
        -H 'content-type: application/json' -d @-
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.examples import RESIDENTS, STATIONS, WEIGHTS  # noqa: E402

plain = "--plain" in sys.argv

if plain:
    residents = [{"id": i, "x": x, "y": y} for i, x, y in RESIDENTS]
    note = "诊所选址回归基准（r=3 最优 {C1,C2,C3} 共 3 座；权重缺省 1）"
else:
    residents = [{"id": i, "x": x, "y": y,
                 "weight": WEIGHTS.get(i, 1)} for i, x, y in RESIDENTS]
    note = "诊所选址+人口权重分期基准（r=3 最少 {C1,C2,C3}；三期 28/50/62）"

payload = {
    "change_note": note,
    "residents": residents,
    "stations": [{"id": i, "x": x, "y": y} for i, x, y in STATIONS],
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
