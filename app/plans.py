"""分期建设计划的状态管理与并发控制。

计划挂在某个项目的某个版本上，由 (版本, 半径, 必开集合, 期数, 各期预算)
唯一确定——同一组参数重复发起直接取回已有记录，不产生重复。

两种变更计划的操作
--------------------
1. **确认某一期**（confirm）：确认第 k 期后，第 0..k 期的新建站组就算
   开工、被锁死；第 k 期必须在第 k-1 期已确认之后才能确认。锁掉的前缀
   在之后任何"调预算重排"中原样保留。
2. **调整预算后重排**（replan）：期数不变、预算可改，但已确认各期的
   预算只许放宽（必须仍容得下已开工的站组），未确认期重新优化排布。

并发控制：乐观锁（revision）
----------------------------
取回计划时带一个 revision；确认/重排必须带着它看到的 revision 提交。
若中间另一位同事已经成功改过（revision 被推进），提交以 409 失败，
错误信息明确说明"你看到的计划已过时"，**绝不允许一方静默冲掉另一方**。
存储层用 ``UPDATE ... WHERE id=? AND revision=?`` 单条语句做
比较并交换（CAS），进程内多线程与跨进程都安全。

不可行的诚实报告
----------------
全开够不着（列居民点）或各期预算注定收不了口（列卡在第几期、缺几座）
时，计划落为 infeasible 记录并照常返回诊断，调用方不得报成成功。
"""

from __future__ import annotations

import threading

from .errors import ApiError
from .phasing import (
    STATUS_INCONCLUSIVE,
    PhaseRequest,
    plan_phases,
    result_to_dict,
)
from .storage import (
    PLAN_ACTIVE,
    PLAN_COMPLETED,
    PLAN_INFEASIBLE,
    DuplicatePlanError,
    Storage,
)


class PlanManager:
    def __init__(self, storage: Storage):
        self.storage = storage
        # 进程内串行化"检查重复 → 计算 → 插入"，避免并发同参数请求
        # 各自算一遍（存储层 UNIQUE 是最终防线，抛 DuplicatePlanError）。
        self._create_lock = threading.Lock()

    # ------------------------------------------------------------------
    def create_or_get(self, version: dict, *, radius: float,
                      budgets: list[int], forced_ids: list[str],
                      time_limit: float | None) -> tuple[dict, bool]:
        """同参数已有记录则取回；否则计算并落库。返回 (计划, 是否新建)。"""
        existing = self.storage.find_plan(version["id"], radius, forced_ids,
                                          budgets)
        if existing is not None:
            return existing, False

        with self._create_lock:
            existing = self.storage.find_plan(version["id"], radius,
                                              forced_ids, budgets)
            if existing is not None:
                return existing, False

            result = self._compute(
                version, radius, budgets, forced_ids, time_limit,
                locked_groups=[], unphased_baseline=None)
            if result["status"] == STATUS_INCONCLUSIVE:
                # 不可结论不落库（也没有计划可确认）；让调用方原样返回
                return {"_inconclusive": result}, False

            feasible = result["feasible"]
            try:
                plan = self.storage.insert_plan(
                    version_id=version["id"], radius=radius,
                    forced_ids=forced_ids, budgets=budgets,
                    status=PLAN_ACTIVE if feasible else PLAN_INFEASIBLE,
                    confirmed_periods=0, locked=[],
                    objective=result["objective"],
                    unphased_count=result.get("unphased_min_station_count"),
                    result=result)
            except DuplicatePlanError:
                got = self.storage.find_plan(version["id"], radius,
                                             forced_ids, budgets)
                return got, False
            return plan, True

    # ------------------------------------------------------------------
    def confirm(self, plan_id: str, *, expected_revision: int,
                period: int) -> dict:
        """确认第 period 期（0 起）。返回更新后的计划。"""
        plan = self._require(plan_id)
        if plan["status"] == PLAN_INFEASIBLE:
            raise ApiError("该计划收不了口，没有任何一期可以确认",
                           field="plan_id", status_code=409)
        if plan["status"] == PLAN_COMPLETED:
            raise ApiError("计划的最后一期已确认，无需再确认",
                           field="period", status_code=409)
        self._check_revision(plan, expected_revision)
        if period != plan["confirmed_periods"]:
            if period < plan["confirmed_periods"]:
                raise ApiError(
                    f"第 {period} 期已确认，不能重复确认",
                    field="period", status_code=409,
                    details={"confirmed_periods": plan["confirmed_periods"]})
            raise ApiError(
                f"第 {period} 期还不能确认：上一期（第 "
                f"{plan['confirmed_periods']} 期）尚未确认",
                field="period", status_code=409,
                details={"confirmed_periods": plan["confirmed_periods"]})

        locked = [list(g) for g in plan["locked"]]
        new_group = list(plan["result"]["phases"][period]["new_station_ids"])
        if not new_group:
            # 空期无需"开工"：直接推进确认指针，不锁任何站
            pass
        locked.append(new_group)
        T = plan["result"]["periods"]
        new_status = (PLAN_COMPLETED if period == T - 1 else PLAN_ACTIVE)

        updated = self.storage.cas_update_plan(
            plan_id, expected_revision, status=new_status,
            confirmed_periods=period + 1, locked=locked)
        if updated is None:
            self._raise_stale(plan_id)
        return updated

    # ------------------------------------------------------------------
    def replan(self, plan_id: str, *, expected_revision: int,
               budgets: list[int], time_limit: float | None) -> dict:
        """调整每期预算后重排（期数不变，已确认前缀锁死）。

        上一次重排得到收不了口的计划时，允许再用放宽的预算重排；
        只有全部期已确认（completed）才不允许。
        """
        plan = self._require(plan_id)
        if plan["status"] == PLAN_COMPLETED:
            raise ApiError("计划已全部确认，不能再调预算重排",
                           field="plan_id", status_code=409)
        self._check_revision(plan, expected_revision)

        old_budgets = plan["budgets"]
        if len(budgets) != len(old_budgets):
            raise ApiError(
                f"重排不能改变期数：原计划 {len(old_budgets)} 期，"
                f"本次给了 {len(budgets)} 期",
                field="budgets", status_code=400,
                details={"expected_periods": len(old_budgets)})
        L = plan["confirmed_periods"]
        for t in range(L):
            need = len(plan["locked"][t])
            if budgets[t] < need:
                raise ApiError(
                    f"第 {t} 期已开工 {need} 座站，新预算 {budgets[t]} "
                    f"座容不下；已确认期的预算只能放宽",
                    field=f"budgets.{t}", status_code=409,
                    details={"period": t, "built": need,
                             "given_budget": budgets[t]})

        # 新参数若恰好是同版本下另一份计划的参数，不做行内改键，
        # 避免破坏 UNIQUE(版本,半径,必开,期数,预算) 的幂等语义。
        if list(budgets) != old_budgets:
            other = self.storage.find_plan(
                plan["version_id"], plan["radius"], plan["forced_ids"],
                budgets)
            if other is not None and other["id"] != plan_id:
                raise ApiError(
                    "同一版本、同一半径与必开约束下，这组期数/预算已经有"
                    "另一份计划；请直接取回那份计划或换一组参数",
                    field="budgets", status_code=409,
                    details={"existing_plan_id": other["id"]})

        version = self.storage.get_version(plan["version_id"])
        if version is None:
            raise ApiError("计划所属版本已不存在", field="plan_id",
                           status_code=404)

        result = self._compute(
            version, plan["radius"], budgets, plan["forced_ids"],
            time_limit, locked_groups=[list(g) for g in plan["locked"]],
            unphased_baseline=plan["unphased_count"])

        if result["status"] == STATUS_INCONCLUSIVE:
            raise ApiError(result.get("note") or "时限内未能完成重排",
                           field="time_limit", status_code=409,
                           details={"plan_revision": plan["revision"]})

        new_status = (PLAN_INFEASIBLE if not result["feasible"]
                      else PLAN_COMPLETED if L == len(budgets)
                      else PLAN_ACTIVE)

        updated = self.storage.cas_update_plan(
            plan_id, expected_revision,
            budgets=budgets, status=new_status,
            confirmed_periods=L,
            locked=[list(g) for g in plan["locked"]],
            result=result)
        if updated is None:
            self._raise_stale(plan_id)
        return updated

    # ------------------------------------------------------------------
    def _compute(self, version: dict, radius: float, budgets: list[int],
                 forced_ids: list[str], time_limit: float | None,
                 locked_groups: list[list[str]],
                 unphased_baseline: int | None) -> dict:
        req = PhaseRequest(
            residents=version["residents"],
            stations=version["stations"],
            radius=radius, budgets=budgets, forced_ids=forced_ids,
            time_limit=time_limit,
            locked_groups=locked_groups,
            unphased_baseline=unphased_baseline)
        r = plan_phases(req)
        return result_to_dict(r, budgets)

    def _require(self, plan_id: str) -> dict:
        plan = self.storage.get_plan(plan_id)
        if plan is None:
            raise ApiError("计划不存在", field="plan_id", status_code=404)
        return plan

    def _check_revision(self, plan: dict, expected_revision: int):
        if expected_revision != plan["revision"]:
            self._raise_stale(plan["id"])

    def _raise_stale(self, plan_id: str):
        fresh = self.storage.get_plan(plan_id)
        details = {"plan_id": plan_id}
        if fresh is not None:
            details["current_revision"] = fresh["revision"]
        raise ApiError(
            "你看到的计划已经过时：另一位同事刚刚改过（确认或重排），"
            "请重新取回最新计划再操作，本次提交未生效",
            field="revision", status_code=409, details=details)


def serialize_plan(plan: dict) -> dict:
    return {
        "id": plan["id"],
        "version_id": plan["version_id"],
        "radius": plan["radius"],
        "forced_station_ids": plan["forced_ids"],
        "periods": plan["periods"],
        "budgets": plan["budgets"],
        "status": plan["status"],
        "confirmed_periods": plan["confirmed_periods"],
        "locked_periods": plan["locked"],
        "revision": plan["revision"],
        "objective": plan["objective"],
        "unphased_min_station_count": plan["unphased_count"],
        "plan": plan["result"],
        "created_at": plan["created_at"],
        "updated_at": plan["updated_at"],
    }
