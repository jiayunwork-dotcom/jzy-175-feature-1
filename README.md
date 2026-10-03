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
  （策略见 `docs/algorithm.md`）；
- 居民点可带**人口权重**，并在此之上出**分期建设计划**：钱按期拨、
  已建不拆，目标"总站数最少优先，同站数下早期照顾人口尽量多"，
  结果逐期给出新建站、累计照顾人口、比无分期最少站数多几座；
- 计划可逐期**确认开工**、确认后**调预算重排**；乐观锁（revision）
  保证两位同事并发改同一份计划时只有一个成功，落败方收到明确冲突；
- 预算注定收不了口时如实报告（列够不着的点 / 卡在哪一期、缺几座），
  绝不报成成功；没证明的计划绝不标"已证明最优"；
- **旧服务数据库直接挂载接管**：旧居民点按权重 1 处理、旧方案结果
  不变，无需清库。

技术栈：Python 3.12 · FastAPI · SQLite（WAL）· 纯标准库求解器，
覆盖构建 / 精确求解 / 作业调度与取消 / 版本存储 / 存储升级 /
分期规划 / 计划状态与并发 / HTTP 层各自独立成模块。

## 目录结构

```
app/
  config.py       配置（DB 路径、并发数，环境变量可覆盖）
  coverage.py     覆盖关系构建（位掩码）
  solver.py       精确分支定界求解器（下界、传播、时限、取消）
  phasing.py      带人口权重的分期建设规划（枚举/截断/不可行诊断）
  plans.py        计划状态机：确认、重排、乐观锁并发控制
  incremental.py  增量重解（热启动 + 完整证明，含冷启动退回规则）
  migrations.py   存储建表与版本化升级（旧库直接接管）
  storage.py      SQLite：项目/版本/方案去重/作业/计划/重启恢复
  jobs.py         后台作业调度（线程池、进度、取消、扫描）
  schemas.py      Pydantic 模型与字段校验（含居民点 weight）
  errors.py       统一错误（field 指向具体字段）
  examples.py     诊所选址回归基准（无权 + 带权分期，手工可验算）
  api/routes.py       HTTP 路由（项目/版本/作业）
  api/plans_routes.py HTTP 路由（分期计划）
  main.py         应用工厂与生命周期
docs/
  algorithm.md    算法、增量与分期策略说明（重点）
  api.md          接口字段与状态语义
tests/            pytest 自动化测试（381 个用例）
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

# 6. 分期建设计划（带权基准请求体：WEIGHTED=1 examples/clinic_request.py）
PLAN=$(curl -s -X POST localhost:8000/api/projects/$PID/versions/$VID/plans \
  -H 'content-type: application/json' \
  -d '{"radius":3.0,"budgets":[1,1,1]}')
PLAN_ID=$(echo "$PLAN" | python -c 'import sys,json;print(json.load(sys.stdin)["plan"]["id"])')
# → 三期最优：C2（28 人）→ C1（累计 50）→ C3（累计 56），总站数 3，extra=0

# 7. 同事 A 取回的 revision=0，确认第一期开工
curl -s -X POST localhost:8000/api/projects/$PID/versions/$VID/plans/$PLAN_ID/confirm \
  -H 'content-type: application/json' -d '{"revision":0,"period":0}'
# 8. 确认后调预算重排（第一期 C2 锁死；拿旧 revision 会收到 409）
curl -s -X POST localhost:8000/api/projects/$PID/versions/$VID/plans/$PLAN_ID/replan \
  -H 'content-type: application/json' -d '{"revision":1,"budgets":[1,2,2]}'
```

## 诊所回归基准

`app/examples.py`：12 个居民点 + 5 个候选诊所，半径 3.0 时**唯一**
最优解为 `{C1,C2,C3}` 共 3 座，可纯手工验算（文件头部有完整论证），
并已经暴力枚举复核。它同时覆盖：半径单调（3 座→2 座）、无解漏点
（r=2 漏 R01/R02）、强制非最优站（4 座）、移除关键站变无解。

同文件还固化了**带人口权重的分期基准**（总人口 56，覆盖关系与上面
完全一致，无分期最优仍是 `{C1,C2,C3}`）：

- `[1,1,1]` 三期各 1 座：`C2 → C1 → C3`，累计人口 `28 → 50 → 56`，
  总站数 3、`extra_over_unphased=0`、标已证明最优；
- `[2,1]` 两期：先 `{C1,C2}`（50），再 `C3`（56）；
- `[1,1]` 总预算只有 2：收不了口，报卡在第 0 期、累计缺口 2 座，
  状态 infeasible，不会报成功。

权重不进入最少开站求解：加权前后 `r=3` 的无权最优结果逐位不变
（有专门测试固化）。

## 关于"最优"的承诺

- `proven_optimal=true` 只来自分支定界完整证明；
- 超时/取消结果即使数值正好等于最优也不标最优，并给出
  `gap = best - lower_bound`；
- 超时报的 `lower_bound ≤ 真实最优 ≤ best_count`（测试固化）；
- 分期计划的 `proven_optimal=true` 同理：无分期站数已证明 +
  同规模最终集合枚举完 + 每个最终集合的排布枚举完，三者缺一即
  降级为 `best_feasible`（计划仍合法，可逐期确认）。

## 旧库接管

数据目录是挂载长期使用的。新版本启动时对旧库做幂等迁移
（`PRAGMA user_version` 版本化，见 `app/migrations.py`）：只补
`plans` 表，不改旧表；旧版本居民点 JSON 没有 `weight` 字段时读取
按 1 回填（不重写数据）。旧项目、旧版本、旧方案照常读取与求解，
不需要先清库（有端到端测试用手工旧结构库验证）。
