# 社区服务站精确选址后端

平面欧氏距离、统一服务半径下的**最少开站**精确求解服务。支持：

- 集合覆盖的**分支定界精确解**（不是贪心近似），结果明确标注
  "是否已证明最优"；没证明的绝不标最优；
- 必开站（"非建不可"）约束；全开仍够不着时判无解并列出漏点；
- 项目/版本化存储：每次居民点或候选址变更生成新版本，旧版本与
  当时方案原样保留，可按版本号取回/对比；同版本同参数重复求解不产生
  重复记录；
- 求解与半径扫描都是**后台作业**：立即返回作业号，可查进度
  （已探索节点数、当前最好站数、已证明下界）、可设时限、可主动取消；
- 超时/取消交出手上最好可行解 + 合法下界 + 最优间隙，状态如实标注；
- 多作业并发互不干扰；**服务重启后已完成结果仍在，未完成作业标
  `interrupted`**，不会永远挂着"运行中"；
- 在旧版本上增删居民点的**增量重解**，结果与全量求解严格一致
  （策略见 `docs/algorithm.md`）。

技术栈：Python 3.12 · FastAPI · SQLite（WAL）· 纯标准库求解器，
覆盖构建 / 精确求解 / 作业调度与取消 / 版本存储 / 增量重解 / HTTP
层各自独立成模块。

## 目录结构

```
app/
  config.py       配置（DB 路径、并发数，环境变量可覆盖）
  coverage.py     覆盖关系构建（位掩码）
  solver.py       精确分支定界求解器（下界、传播、时限、取消）
  incremental.py  增量重解（热启动 + 完整证明，含冷启动退回规则）
  storage.py      SQLite：项目/版本/方案去重/作业/重启恢复
  jobs.py         后台作业调度（线程池、进度、取消、扫描）
  schemas.py      Pydantic 模型与字段校验
  errors.py       统一错误（field 指向具体字段）
  examples.py     诊所选址回归基准（手工可验算）
  api/routes.py   HTTP 路由
  main.py         应用工厂与生命周期
docs/
  algorithm.md    算法与增量策略说明（重点）
  api.md          接口字段与状态语义
tests/            pytest 自动化测试（284 个用例）
Dockerfile        python:3.12-slim 单容器
```

## 本地运行（容器）

```bash
docker build -t facility-location .
docker run -d --name facility -p 8000:8000 \
  -v $PWD/data:/data facility-location
# 数据落在挂载目录的 facility.db（SQLite）
curl http://localhost:8000/health
```

环境变量：

- `FACILITY_DB_PATH`（默认 `/data/facility.db`）；
- `FACILITY_MAX_WORKERS`（默认 2，同时求解的作业数）。

## 本地开发（不用容器）

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
FACILITY_DB_PATH=./data/facility.db \
  uvicorn app.main:app --reload
pytest          # 运行全部自动化测试
```

## 快速上手

```bash
# 1. 建项目
PID=$(curl -s -X POST localhost:8000/api/projects \
  -H 'content-type: application/json' \
  -d '{"name":"某街道"}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 2. 提交诊所基准版本（12 居民点 / 5 候选址）
VID=$(python examples/clinic_request.py | curl -s -X POST \
  localhost:8000/api/projects/$PID/versions \
  -H 'content-type: application/json' -d @- | \
  python -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 3. 发起求解（半径 3，30 秒时限）
JID=$(curl -s -X POST localhost:8000/api/projects/$PID/versions/$VID/solve \
  -H 'content-type: application/json' \
  -d '{"radius":3.0,"forced_station_ids":[],"time_limit":30}' | \
  python -c 'import sys,json;print(json.load(sys.stdin)["job"]["id"])')

# 4. 查进度/结果
curl -s localhost:8000/api/jobs/$ID | python -m json.tool
# → station_count=3, chosen=[C1,C2,C3], proven_optimal=true

# 5. 半径阶梯
curl -s -X POST localhost:8000/api/projects/$PID/versions/$VID/sweep \
  -H 'content-type: application/json' \
  -d '{"radii":[2,3,4,5.5]}'
```

## 诊所回归基准

`app/examples.py`：12 个居民点 + 5 个候选诊所，半径 3.0 时**唯一**
最优解为 `{C1,C2,C3}` 共 3 座，可纯手工验算（文件头部有完整论证），
并已经暴力枚举复核。它同时覆盖：半径单调（3 座→2 座）、无解漏点
（r=2 漏 R01/R02）、强制非最优站（4 座）、移除关键站变无解。

## 关于"最优"的承诺

- `proven_optimal=true` 只来自分支定界完整证明；
- 超时/取消结果即使数值正好等于最优也不标最优，并给出
  `gap = best - lower_bound`；
- 超时报的 `lower_bound ≤ 真实最优 ≤ best_count`（测试固化）。
