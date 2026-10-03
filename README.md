# 社区服务站精确选址后端

平面欧氏距离、统一服务半径下的**最少开站**精确求解服务。支持：

- 集合覆盖的**分支定界精确解**（不是贪心近似），结果明确标注
  "是否已证明最优"；没证明的绝不标最优；
- 必开站（"非建不可"）约束；全开仍够不着时判无解并列出漏点；
- **居民点人口权重**（缺省按 1）；最少开站求解不看权重，加权前后
  结果完全一致；
- **分期建设计划**：给定期数与每期建站预算，在"总站数最少"前提下
  词典序最大化各期累计照顾人口；可逐期确认（已建不拆、已确认期
  冻结），可改预算重排；乐观锁防并发静默覆盖；预算收不了口如实
  报告并列漏点，绝不伪装成功；
- 项目/版本化存储：每次居民点或候选址变更生成新版本，旧版本与
  当时方案原样保留，可按版本号取回/对比；同版本同参数重复求解不产生
  重复记录；
- 求解与半径扫描都是**后台作业**：立即返回作业号，可查进度
  （已探索节点数、当前最好站数、已证明下界）、可设时限、可主动取消；
- 超时/取消交出手上最好可行解 + 合法下界 + 最优间隙，状态如实标注；
- 多作业并发互不干扰；**服务重启后已完成结果仍在，未完成作业标
  `interrupted`**，不会永远挂着"运行中"；
- 在旧版本上增删居民点的**增量重解**，结果与全量求解严格一致
  （策略见 `docs/algorithm.md`）；
- **挂载旧数据目录直接接管**：旧库自动补建新表，旧居民点按权重 1
  读取，旧方案/作业照常工作，无需清库。

技术栈：Python 3.12 · FastAPI · SQLite（WAL）· 纯标准库求解器，
覆盖构建 / 精确求解 / 分期规划 / 计划状态与并发 / 作业调度与取消 /
版本存储 / 增量重解 / HTTP 层各自独立成模块。

## 目录结构

```
app/
  config.py          配置（DB 路径、并发数，环境变量可覆盖）
  coverage.py        覆盖关系构建（位掩码）
  solver.py          精确分支定界求解器（下界、传播、时限、取消）
  phasing.py         分期规划（词典序目标、可延展剪枝、收口判定）
  plans.py           计划应用服务（逐期确认状态机 + revision 乐观锁）
  incremental.py     增量重解（热启动 + 完整证明，含冷启动退回规则）
  storage.py         SQLite：项目/版本/方案/计划/作业 + 旧库平滑升级
  jobs.py            后台作业调度（线程池、进度、取消、扫描）
  schemas.py         Pydantic 模型与字段校验（含 weight、计划请求）
  errors.py          统一错误（field 指向具体字段）
  examples.py        诊所选址与分期回归基准（手工可验算）
  api/routes.py       项目/版本/作业 HTTP 路由
  api/plans_routes.py 分期计划 HTTP 路由（创建/取回/确认/重排）
  main.py             应用工厂与生命周期
docs/
  algorithm.md       算法、增量策略与分期目标说明（重点）
  api.md             接口字段与状态语义
tests/               pytest 自动化测试（505 个用例）
Dockerfile           python:3.12-slim 单容器
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

### 分期基准（带权重，手工可验算）

同一套几何加上人口权重（总人口 62；`WEIGHTS` 常量）：r=3.0 各站
照顾人口 C1=28、C2=22、C3=15、C4=10、C5=3，无分期最少仍为
`{C1,C2,C3}` 3 座（求解不看权重）。

- 三期预算 `[1,1,1]`：最优排法 `[C1] → [C1,C2] → [C1,C2,C3]`，
  累计照顾人口 **28、50、62**，总站数 3，比无分期最少多 **0** 座；
- 两期预算 `[2,1]`：`{C1,C2} → {C1,C2,C3}`，累计 **50、62**；
- 预算 `[1,1]`（总 2 < 3）：最后一期收不了口，如实报 `cannot_close`、
  卡在第 2 期并列出够不着的点。

```bash
# 发起三期计划
curl -s -X POST \
  localhost:8000/api/projects/$PID/versions/$VID/plans \
  -H 'content-type: application/json' \
  -d '{"radius":3.0,"budgets":[1,1,1]}'
# 取回后带 revision 逐期确认；改预算重排
curl -s -X POST localhost:8000/api/plans/$PLAN_ID/confirm \
  -H 'content-type: application/json' -d '{"revision":1}'
curl -s -X POST localhost:8000/api/plans/$PLAN_ID/replan \
  -H 'content-type: application/json' \
  -d '{"revision":2,"budgets":[1,2]}'
```

优化目标（总站数最少优先，再词典序最大化早期人口）与取舍代价见
`docs/algorithm.md` 第 8 节，接口字段见 `docs/api.md` 第 5 节。

## 关于"最优"的承诺

- `proven_optimal=true` 只来自分支定界完整证明；
- 超时/取消结果即使数值正好等于最优也不标最优，并给出
  `gap = best - lower_bound`；
- 超时报的 `lower_bound ≤ 真实最优 ≤ best_count`（测试固化）；
- 分期计划同理：`status=optimal` 要求最少站数已证明 **且** 排顺序
  搜索完整穷尽；到时限交手上最好的合法计划，标 `status=feasible`；
  预算收不了口时是 `cannot_close`（不会报成功），漏点与卡点期
  一并列出。
