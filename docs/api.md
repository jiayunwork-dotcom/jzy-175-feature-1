# HTTP 接口说明

所有接口前缀 `/api`，请求/响应均为 JSON。错误统一形如：

```json
{ "error": "半径必须为正数", "field": "radius" }
```

- `field`：出错的具体字段（嵌套/列表为点分路径，如 `residents.0.id`）；
- 422：请求体结构/类型校验失败；400：业务校验（半径非正、空集合等）；
  404：资源或点名站编号不存在；409：对已终态作业取消、计划乐观锁
  冲突 / 对不可确认或已完成的计划操作 / 新预算容不下已确认期。

## 1. 项目

### POST /api/projects
```json
{ "name": "某街道 2026 选址" }
```
→ 201 `{ "id", "name", "created_at" }`

### GET /api/projects/{project_id}
### GET /api/projects/{project_id}/versions
返回版本摘要列表（含版本号、父版本、备注）。

## 2. 版本

点编号 `id` 为非空字符串，同一版本内不可重复。

### POST /api/projects/{project_id}/versions
提交全新版本（居民点 + 候选址）：
```json
{
  "residents": [{"id": "R01", "x": 0.0, "y": 0.0}],
  "stations":  [{"id": "C1",  "x": 2.0, "y": 1.0}],
  "change_note": "初版"
}
```
- 居民点为空 → 400 `field=residents`；
- 候选址为空 → 400 `field=stations`；
- 编号重复/缺失 → 422，`field` 指向具体列表。

### POST /api/projects/{project_id}/versions/from/{version_id}
在已有版本上**增删居民点**（候选址原样沿用，生成新版本）：
```json
{
  "add_residents": [{"id": "NEW1", "x": 5.0, "y": 0.5}],
  "remove_resident_ids": ["R01"],
  "change_note": "西侧加一户"
}
```
- 新增编号撞现存编号、删除不存在的编号、删空居民点 → 带 field 的错误。

### GET /api/projects/{project_id}/versions/{version_id}
取回完整版本（旧版本永远原样保留）。

### GET /api/projects/{project_id}/versions/{version_id}/diff?against_version_id={other}
两个版本的居民点/候选址增删改对比。

## 3. 求解作业

### POST /api/projects/{project_id}/versions/{version_id}/solve
```json
{ "radius": 3.0, "forced_station_ids": ["C1"], "time_limit": 30 }
```
- `radius`：正数，否则 400 `field=radius`；
- `forced_station_ids`：必开站编号，不存在 → 404 `field=forced_station_ids`，
  响应 `details.unknown` 列出坏编号；
- `time_limit`：秒，选填、正数；不给则不限时。

→ 202：
```json
{ "reused": false, "job": { "id": "...", "status": "queued", ... } }
```
幂等：同版本同 `(radius, forced)` 已有已完成方案时直接
`{"reused": true, "solution_id": ..., "result": ...}`，不产生重复记录；
已有在跑作业时复用该作业。

### GET /api/jobs/{job_id}
```json
{
  "id": "...",
  "kind": "solve",
  "status": "queued|running|completed|timeout|cancelled|interrupted|failed",
  "params": { "radius": 3.0, "forced": [0] },
  "progress": {
    "explored_nodes": 18230,
    "best_count": 18,
    "lower_bound": 12
  },
  "result": {
    "status": "optimal",
    "chosen": [0, 2, 4],
    "station_count": 3,
    "lower_bound": 3,
    "gap": 0,
    "explored_nodes": 47,
    "uncovered": [],
    "proven_optimal": true,
    "strategy": "warm"
  }
}
```
结果字段含义：

- `status=optimal`：已证明最优，`proven_optimal=true`、`gap=0`；
- `status=infeasible`：全开也够不着，`station_count=null`，
  `uncovered` 列出够不着的居民点下标；**这是 completed 作业**；
- 作业 `timeout/cancelled`：`result.status` 同名，`proven_optimal=false`，
  仍带手上最好可行解 `chosen/station_count`、合法 `lower_bound` 与 `gap`；
- `interrupted`：服务重启时未跑完的作业。

### POST /api/jobs/{job_id}/cancel
协作式取消，返回 `{"cancelling": true}`；已终态 → 409。

### GET /api/projects/{project_id}/jobs
列出项目下全部作业。

## 4. 半径扫描作业

### POST /api/projects/{project_id}/versions/{version_id}/sweep
```json
{
  "radii": [2.0, 3.0, 4.0, 5.5],
  "forced_station_ids": [],
  "time_limit": 30
}
```
- 半径必须非空、全部为正、升序且不重复，否则 422 `field=radii`；
- `time_limit` 为**整个扫描**的时限；每一步用剩余时限独立精解。

结果：
```json
{
  "status": "completed",
  "forced": [0],
  "steps": [
    {"radius": 2.0, "status": "infeasible", "station_count": null,
     "uncovered": [0,1], "proven_optimal": false},
    {"radius": 3.0, "status": "optimal", "station_count": 3,
     "chosen": [0,1,2], "lower_bound": 3, "gap": 0, "proven_optimal": true},
    {"radius": 4.0, "status": "optimal", "station_count": 2, ...}
  ]
}
```
超时后剩余半径标 `not_computed`，取消后标 `cancelled`，已算完的步骤
全部保留；作业状态为 `timeout`/`cancelled` 而非 `completed`。

## 5. 分期建设计划

居民点可带 `weight`（人口权重，正数，缺省 1）。计划挂在版本上，
给定半径、期数与每期最多新建站数。

### POST /api/projects/{project_id}/versions/{version_id}/plans
```json
{
  "radius": 3.0,
  "budgets": [1, 1, 1],
  "forced_station_ids": [],
  "time_limit": 30
}
```
- `budgets`：每期最多新建几座站；非空、每项正整数，否则 422
  `field=budgets`；期数即列表长度。半径非正 → 400；必开站编号
  不存在 → 404。
- **同步**返回计划（分期搜索规模通常很小；可用 `time_limit` 限时）。
  同版本同 `(radius, budgets, forced)` 重复发起不产生新记录：
  重复请求返回同 `id` 且带 `"reused": true`（首次 201，复用 201 同体）。

→ 201（节选）：
```json
{
  "id": "...", "revision": 1, "state": "planning",
  "confirmed_until": -1,
  "status": "optimal",
  "feasible": true,
  "proven_optimal": true,
  "objective": "词典序：先最小化总站数……代价：不允许为前期好看而超建……",
  "periods": [
    {"period": 0, "budget": 1, "new_station_ids": ["C1"],
     "new_count": 1, "cumulative_station_ids": ["C1"],
     "cumulative_count": 1,
     "covered_resident_ids": ["R01","R02","R03","M06","M08","M09"],
     "covered_population": 28, "confirmed": false},
    {"period": 1, "new_station_ids": ["C2"], "covered_population": 50, ...},
    {"period": 2, "new_station_ids": ["C3"], "covered_population": 62, ...}
  ],
  "total_stations": 3,
  "minimum_stations": 3,
  "extra_over_minimum": 0,
  "total_population": 62,
  "blocked_period": null, "budget_shortfall": 0,
  "uncovered_resident_ids": []
}
```

状态语义（`status`）：

| 值 | 含义 | proven_optimal |
|---|---|---|
| `optimal` | 词典序最优且完整证明（总站数=最少，人口向量逐期最优） | true |
| `feasible` | 到时限交出的可行计划，嵌套/预算/末期满覆盖均核验通过 | false |
| `cannot_close` | 给定预算无论怎么排末期都收不了口（或新预算容不下已确认期） | false |
| `infeasible` | 全开也够不着，`uncovered_resident_ids` 列漏点 | false |

收不了口时还给出 `blocked_period`（0 起；纯容量不足时为末期）、
`budget_shortfall`（还差几座），并在尽量建满预算后列出仍然
够不着的居民点。`feasible=false` 绝不允许确认。

### GET /api/plans/{plan_id}
### GET /api/projects/{project_id}/versions/{version_id}/plans
取回单份 / 列出该版本全部计划（含预算不同的多份计划）。

### POST /api/plans/{plan_id}/confirm
```json
{ "revision": 1 }
```
确认"下一期"（只能按顺序：确认第 k 期要求第 k-1 期已确认）。成功
返回更新后的计划，`confirmed_until` 与 `revision` 各 +1；末期确认
后 `state=completed`。

- `revision` 与当前不一致 → 409 `field=revision`，错误信息明确
  告知计划已被另一方修改、请取回最新版本重试（乐观锁，防静默覆盖）；
- 对 `feasible=false` 的计划或已 completed 的计划确认 → 409。

### POST /api/plans/{plan_id}/replan
```json
{ "revision": 2, "budgets": [1, 2], "time_limit": 30 }
```
调整每期预算后重排。**已确认期一位都不能改**：求解器以已确认期为
锁定前缀，只重排之后的期。新预算期数必须多于已确认期数、且每个
已确认期的新建数不得超过对应新预算，否则 409（记录保持不变）。
`revision` 过时 → 409；已 completed 的计划不能重排 → 409。
成功后 `revision+1`、`budgets` 更新为新值。

## 6. 健康检查

### GET /health → `{"status":"ok"}`

## 7. 状态语义一览（重点防错）

| 情形 | 作业 status | result.status | proven_optimal |
|---|---|---|---|
| 完整证明最优 | completed | optimal | true |
| 全开仍有漏点 | completed | infeasible | false（带 uncovered） |
| 到达时限 | timeout | timeout | false（带 best/lb/gap） |
| 被取消 | cancelled | cancelled | false（带 best/lb/gap） |
| 服务重启时未完成 | interrupted | —（无结果） | — |
| 内部异常 | failed | — | — |

不变量：任何 `station_count` 非空的结果，其 `chosen` 都满足全覆盖；
`proven_optimal=true` 当且仅当 `gap=0` 且搜索树被完整剪枝/穷尽。
