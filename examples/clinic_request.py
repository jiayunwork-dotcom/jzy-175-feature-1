#!/usr/bin/env python3
"""把诊所基准（app/examples.py）输出为创建版本用的 JSON 请求体。

用法：
    python examples/clinic_request.py
    python examples/clinic_request.py | curl -X POST .../versions \
        -H 'content-type: application/json' -d @-
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.examples import RESIDENTS, STATIONS  # noqa: E402

payload = {
    "change_note": "诊所选址回归基准（r=3 最优 {C1,C2,C3} 共 3 座）",
    "residents": [{"id": i, "x": x, "y": y} for i, x, y in RESIDENTS],
    "stations": [{"id": i, "x": x, "y": y} for i, x, y in STATIONS],
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
