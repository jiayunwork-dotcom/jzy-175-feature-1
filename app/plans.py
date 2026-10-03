"""分期计划的应用服务：计算编排、逐期确认、预算重排与并发控制。

职责边界
--------
- app/phasing.py 只负责"给定覆盖掩码/权重/预算/锁定前缀，算出一份计划"，
  是纯组合求解；
- 本模块负责版本数据 → 覆盖/权重/下标的映射、最少站数基线复用、
  持久化记录、**逐期确认状态机**与**乐观锁并发控制**；
- HTTP 细节在 app/api/plans_routes.py。

并发模型
--------
计划记录带单调递增的 revision：
- 确认第 k 期、调预算重排都必须带上取回时看到的 revision；
- 存储层用单条 UPDATE ... WHERE revision=? 做比较并交换，失败者收到
  明确的 409 冲突提示（"你看到的计划已过时"），绝不会静默覆盖对方。
典型场景：A 在确认第 2 期的同时 B 改预算重排，只有一方成功。

确认状态机
----------
- confirmed_until：已确认到第几期（-1 表示尚未确认任何一期）；
- 只能按顺序确认下一期；第 k 期确认要求第 k-1 期已确认；
- 已确认期的"新建站集合"冻结，此后任何重排一位都不能改它；
  重排只允许改变它之后各期的安排（新预算必须先容得下已确认期）；
- 末期确认后计划进入 completed；已全部确认的计划不能再重排。
"""

from __future__ import annotations

from .coverage import Point, build_coverage
from .errors import ApiError
from .phasing import (
    PhaseRequest,
    plan_phased,
)

PLAN_PLANNING = "planning"
PLAN_COMPLETED = "completed"
NONE_CONFIRMED = -1


def _build(version: dict, radius: float):
    residents = [Point(r["x"], r["y"]) for r in version["residents"]]
    stations = [Point(s["x"], s["y"]) for s in version["stations"]]
    cov = build_coverage(residents, stations, radius)
    weights = tuple(float(r.get("weight", 1.0)) for r in version["residents"])
    return cov, weights


def _forced_indices(version: dict, forced_ids: list[str]) -> list[int]:
    ids = [s["id"] for s in version["stations"]]
    missing = [f for f in forced_ids if f not in ids]
    if missing:
        raise ApiError(f"点名的站编号不存在: {missing}",
                       field="forced_station_ids", status_code=404,
                       details={"unknown": missing})
    return [ids.index(f) for f in forced_ids]


class PlanManager:
    def __init__(self, storage):
        self.storage = storage

    # ------------------------------------------------------------------
    def create_plan(self, project_id: str, version: dict, radius: float,
                    budgets: list[int], forced_ids: list[str],
                    time_limit: float | None) -> tuple[dict, bool]:
        """返回 (计划记录, 是否新建)。同版本同参数重复发起 → 取回旧记录。"""
        forced = _forced_indices(version, forced_ids)
        existing = self.storage.find_plan(
            version["id"], radius, budgets, forced)
        if existing is not None:
            return existing, False

        result = self._compute(version, radius, tuple(budgets), forced,
                               locked=(), time_limit=time_limit)
        # 只有 1 期且收口成功时，第 1 期即末期，创建即视为全部确认；
        # 多期计划从"逐期确认"开始，状态为 planning。
        state = PLAN_COMPLETED if (len(budgets) == 1 and result.get("feasible"))\
            else PLAN_PLANNING
        confirmed_until = 0 if state == PLAN_COMPLETED else NONE_CONFIRMED
        row = self.storage.create_plan(
            version["id"], radius, budgets, forced, result,
            confirmed_until, state)
        if row is None:
            # 并发下另一请求刚刚插入同参数记录：取回它，不产生重复
            row = self.storage.find_plan(version["id"], radius, budgets,
                                         forced)
            return row, False
        return row, True

    # ------------------------------------------------------------------
    def confirm_period(self, plan_id: str, revision: int) -> dict:
        plan = self._require(plan_id)
        if plan["revision"] != revision:
            raise self._conflict(plan)
        if plan["state"] == PLAN_COMPLETED:
            raise ApiError("计划各期均已确认，无可确认的下一期",
                           field="revision", status_code=409)
        result = plan["result_json"]
        if not result.get("feasible"):
            raise ApiError(
                "计划未能收口（status=%s），无法确认；请调整预算后重排"
                % result.get("status"),
                field="plan_id", status_code=409)
        nxt = plan["confirmed_until"] + 1
        periods = result.get("periods", [])
        if nxt >= len(periods):
            raise ApiError("没有更多期可确认", field="plan_id",
                           status_code=409)
        new_state = PLAN_COMPLETED if nxt == len(periods) - 1 \
            else PLAN_PLANNING
        updated = self.storage.cas_plan(
            plan_id, revision, confirmed_until=nxt, state=new_state)
        if updated is None:
            raise self._conflict(plan)
        return updated

    # ------------------------------------------------------------------
    def replan(self, plan_id: str, revision: int, budgets: list[int],
               time_limit: float | None) -> dict:
        plan = self._require(plan_id)
        if plan["revision"] != revision:
            raise self._conflict(plan)
        if plan["state"] == PLAN_COMPLETED:
            raise ApiError("计划已全部确认，不能再调整预算重排",
                           field="revision", status_code=409)

        version = self.storage.get_version(plan["version_id"])
        if version is None:
            raise ApiError("版本不存在", field="version_id", status_code=404)
        forced = list(plan["forced"])
        T = len(budgets)
        locked = self._locked_prefix(plan, T)
        # 新预算必须先容得下已确认期
        for t, lset in enumerate(locked):
            if len(lset) > budgets[t]:
                raise ApiError(
                    f"第 {t + 1} 期已确认 {len(lset)} 座站，新预算 "
                    f"{budgets[t]} 座无法容纳；已确认期不可改动",
                    field="budgets", status_code=409,
                    details={"period": t, "confirmed": len(lset),
                             "new_budget": budgets[t]})

        result = self._compute(version, plan["radius"], tuple(budgets),
                               forced, locked=tuple(locked),
                               time_limit=time_limit)
        updated = self.storage.cas_plan(
            plan_id, revision, result=result, budgets=budgets,
            state=PLAN_PLANNING)
        if updated is None:
            fresh = self.storage.get_plan(plan_id)
            raise self._conflict(fresh)
        return updated

    # ------------------------------------------------------------------
    def serialize(self, plan: dict, version: dict | None = None) -> dict:
        if version is None:
            version = self.storage.get_version(plan["version_id"])
        station_ids = [s["id"] for s in version["stations"]]
        resident_ids = [r["id"] for r in version["residents"]]
        result = plan["result_json"]
        periods_out = []
        for p in result.get("periods", []):
            cumulative_ids = [station_ids[j]
                              for j in p.get("cumulative_new_indices", [])]
            periods_out.append({
                "period": p["period"],
                "budget": p["budget"],
                "new_station_ids": [station_ids[j] for j in p["new"]],
                "new_count": p["new_count"],
                "cumulative_station_ids": cumulative_ids,
                "cumulative_count": p["cumulative_count"],
                "covered_resident_ids": [resident_ids[i]
                                         for i in p["covered_residents"]],
                "covered_population": p["covered_population"],
                "confirmed": p["period"] <= plan["confirmed_until"],
            })
        out = {
            "id": plan["id"],
            "version_id": plan["version_id"],
            "radius": plan["radius"],
            "budgets": plan["budgets"],
            "forced_station_ids": [station_ids[j] for j in plan["forced"]],
            "state": plan["state"],
            "revision": plan["revision"],
            "confirmed_until": plan["confirmed_until"],
            "status": result.get("status"),
            "feasible": result.get("feasible", False),
            "proven_optimal": result.get("proven_optimal", False),
            "objective": result.get("objective"),
            "periods": periods_out,
            "total_stations": result.get("total_stations"),
            "minimum_stations": result.get("minimum_stations"),
            "extra_over_minimum": result.get("extra_over_minimum"),
            "total_population": result.get("total_population"),
            "blocked_period": result.get("blocked_period"),
            "budget_shortfall": result.get("budget_shortfall"),
            "uncovered_resident_ids": [
                resident_ids[i] for i in result.get("uncovered", [])],
            "explored_nodes": result.get("explored_nodes"),
            "created_at": plan["created_at"],
            "updated_at": plan["updated_at"],
        }
        return out

    # ------------------------------------------------------------------
    def _compute(self, version, radius, budgets, forced, *, locked,
                 time_limit):
        cov, weights = _build(version, radius)
        req = PhaseRequest(
            cover=cov.by_station,
            weights=weights,
            budgets=budgets,
            forced=frozenset(forced),
            locked=tuple(frozenset(s) for s in locked),
            time_limit=time_limit,
        )
        res = plan_phased(req)
        d = res.to_dict()
        # cumulative_mask 是内部位掩码，序列化前换成累计下标，避免把
        # 内部表示写进接口
        for p in d["periods"]:
            m = p.pop("cumulative_mask")
            p["cumulative_new_indices"] = [
                j for j in range(cov.station_count) if (m >> j) & 1]
        return d

    def _locked_prefix(self, plan: dict, T: int) -> list[frozenset]:
        """从已存储计划中取已确认期的新建站集合（按下标）。

        重排后期数必须严格多于已确认期数（必须至少留一期给未确认的站，
        否则已确认的末期之外没有位置收口）。
        """
        confirmed = plan["confirmed_until"]  # 已确认期数 = 它 + 1
        n_locked = confirmed + 1
        if T <= n_locked:
            raise ApiError(
                f"已确认前 {n_locked} 期，新预算只有 {T} 期，"
                "至少要再保留一个未确认期用于收口",
                field="budgets", status_code=409,
                details={"confirmed_periods": n_locked,
                         "new_periods": T})
        result = plan["result_json"]
        locked = []
        for t in range(n_locked):
            locked.append(frozenset(result["periods"][t]["new"]))
        return locked

    def _require(self, plan_id: str) -> dict:
        plan = self.storage.get_plan(plan_id)
        if plan is None:
            raise ApiError("计划不存在", field="plan_id", status_code=404)
        return plan

    @staticmethod
    def _conflict(plan: dict) -> ApiError:
        return ApiError(
            "计划已被另一方修改（确认或重排）：你手上的 revision 已过时，"
            "请取回最新计划后重试，禁止静默覆盖",
            field="revision", status_code=409,
            details={"current_revision":
                         plan["revision"] if plan else None})
