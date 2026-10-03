# HTTP 接口说明

所有接口前缀 `/api`，请求/响应均为 JSON。错误统一形如：

```json
{ "error": "半径必须为正数", "field": "radius" }
```

- `field`：出错的具体字段（嵌套/列表为点分路径，如 `residents.0.id`）；
- 422：请求体结构/类型校验失败；400：业务校验（半径非正、空集合等）；
  404：资源或点名站编号不存在；409：对已终态作业取消。

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

居民点可带 `weight`（正数，不填按 1）：表示该点代表多少人，只影响
分期人口统计，不影响最少开站求解结果。

### POST /api/projects/{project_id}/versions/{version_id}/plans
发起（或幂等取回）一份分期建设计划：
```json
{
  "radius": 3.0,
  "budgets": [1, 1, 1],
  "forced_station_ids": [],
  "time_limit": 30
}
```
- `budgets`：每期最多**新建**几座站，列表长度即期数（2~3 期或更多）；
  元素非负、列表非空，否则 422 `field=budgets`；
- 半径非正 → 400；点名的站不存在 → 404 `field=forced_station_ids`。

→ 201（新建）或 200（同参数已有记录，`reused=true`，不产生重复）：
```json
{
  "reused": false, "created": true,
  "plan": {
    "id": "...", "revision": 0, "status": "active",
    "radius": 3.0, "forced_station_ids": [],
    "periods": 3, "budgets": [1,1,1],
    "confirmed_periods": 0, "locked_periods": [],
    "unphased_min_station_count": 3,
    "objective": "lex_min_stations_then_lex_max_covered_population",
    "plan": {
      "status": "optimal", "feasible": true, "proven_optimal": true,
      "total_station_count": 3, "extra_over_unphased": 0,
      "total_population": 56.0,
      "phases": [
        {"period": 0, "new_station_ids": ["C2"],
         "cumulative_station_ids": ["C2"],
         "cumulative_population": 28.0},
        {"period": 1, "new_station_ids": ["C1"],
         "cumulative_station_ids": ["C1","C2"],
         "cumulative_population": 50.0},
        {"period": 2, "new_station_ids": ["C3"],
         "cumulative_station_ids": ["C1","C2","C3"],
         "cumulative_population": 56.0}
      ],
      "unphased_min_station_count": 3,
      "infeasible_reason": null, "uncovered_resident_ids": [],
      "blocked_at_period": null, "budget_shortage": null, "note": null
    }
  }
}
```

优化目标（详见 `docs/algorithm.md` 第 7 节）：**先把总站数锁到最少**
（正常 `extra_over_unphased=0`），同站数下再逐期最大化累计照顾人口，
再平局取编号最小的站集合。规模/时限导致没证完时
`plan.status="best_feasible"`、`proven_optimal=false`，计划仍合法。

收不了口时计划记录照常创建，但外层 `status="infeasible"`，内层：
- 全开够不着：`uncovered_resident_ids` 列出够不着的居民点；
- 预算注定不够：`blocked_at_period`（0 起，第一个累计预算不够的期）、
  `budget_shortage`（该期累计缺口座数）、`infeasible_reason`。
这种计划不能确认（409）。时限内无法给结论时不产生记录，返回
`{"inconclusive": true, "result": {...}}`。

### GET /api/projects/{project_id}/versions/{version_id}/plans
列出版本下全部计划。

### GET /api/projects/{project_id}/versions/{version_id}/plans/{plan_id}
取回单份计划（含 `revision`、确认状态、各期排布）。

### POST /api/projects/{project_id}/versions/{version_id}/plans/{plan_id}/confirm
确认某一期开工：
```json
{ "revision": 0, "period": 0 }
```
- `revision`：取回计划时看到的版本号（乐观锁）；
- 第 k 期必须在第 k-1 期确认后才能确认；重复/跳期确认 → 409
  `field=period`；
- 与另一位同事的确认/重排竞争落败 → 409 `field=revision`，错误信息
  明确说明"你看到的计划已经过时"，`details.current_revision` 给出
  最新版本号，**本次提交不落库**；
- 确认最后一期后计划状态变 `completed`。

### POST /api/projects/{project_id}/versions/{version_id}/plans/{plan_id}/replan
调整各期预算后重排（期数不能变）：
```json
{ "revision": 1, "budgets": [1, 2, 2] }
```
- 已确认各期**原样锁死**；其新预算若小于已开工站数 → 409
  `field=budgets.t`，`details` 给出该期已建数；
- 改期数 → 400 `field=budgets`；
- 旧 revision 提交 → 409 `field=revision`（同上，绝不静默覆盖）；
- 重排后若新预算注定收不了口，计划状态变 `infeasible`，诊断字段
  照填，但已确认期保留。

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
