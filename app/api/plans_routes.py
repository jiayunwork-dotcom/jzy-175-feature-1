"""分期建设计划的 HTTP 路由（独立于求解/作业路由）。

- POST   .../plans                 发起（或幂等取回）分期计划
- GET    .../plans                 列出版本下的计划
- GET    .../plans/{plan_id}       取回计划（含各期排布与确认状态）
- POST   .../plans/{plan_id}/confirm   确认某一期（带 revision 乐观锁）
- POST   .../plans/{plan_id}/replan    调整各期预算后重排（带乐观锁）
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..errors import ApiError
from ..plans import PlanManager, serialize_plan
from ..schemas import PlanConfirmIn, PlanCreateIn, PlanReplanIn

router = APIRouter(prefix="/api")


def _storage(req: Request):
    return req.app.state.storage


def _plans(req: Request) -> PlanManager:
    return req.app.state.plans


def _require_version(req: Request, project_id: str, version_id: str) -> dict:
    if _storage(req).get_project(project_id) is None:
        raise ApiError("项目不存在", field="project_id", status_code=404)
    v = _storage(req).get_version(version_id)
    if v is None or v["project_id"] != project_id:
        raise ApiError("版本不存在或不属于该项目", field="version_id",
                       status_code=404)
    return v


def _require_plan(req: Request, project_id: str, version_id: str,
                  plan_id: str) -> dict:
    _require_version(req, project_id, version_id)
    plan = _storage(req).get_plan(plan_id)
    if plan is None or plan["version_id"] != version_id:
        raise ApiError("计划不存在或不属于该版本", field="plan_id",
                       status_code=404)
    return plan


def _check_forced(version: dict, forced_ids: list[str]):
    known = {s["id"] for s in version["stations"]}
    missing = [f for f in forced_ids if f not in known]
    if missing:
        raise ApiError(f"点名的站编号不存在: {missing}",
                       field="forced_station_ids", status_code=404,
                       details={"unknown": missing})


@router.post("/projects/{project_id}/versions/{version_id}/plans",
             status_code=201)
def create_plan(project_id: str, version_id: str, body: PlanCreateIn,
                request: Request):
    version = _require_version(request, project_id, version_id)
    if body.radius <= 0:
        raise ApiError("半径必须为正数", field="radius")
    _check_forced(version, body.forced_station_ids)

    plan, created = _plans(request).create_or_get(
        version, radius=body.radius, budgets=list(body.budgets),
        forced_ids=list(body.forced_station_ids),
        time_limit=body.time_limit)

    if "_inconclusive" in plan:
        # 时限内无法给出任何结论：不产生记录，200 + 诊断（不能报成功）
        return {"reused": False, "created": False, "inconclusive": True,
                "result": plan["_inconclusive"]}

    # 幂等命中返回 200；新计算（含确定不可行）落库返回 201
    payload = {"reused": not created, "created": created,
               "plan": serialize_plan(plan)}
    return JSONResponse(status_code=200 if not created else 201,
                        content=payload)


@router.get("/projects/{project_id}/versions/{version_id}/plans")
def list_plans(project_id: str, version_id: str, request: Request):
    _require_version(request, project_id, version_id)
    plans = _storage(request).list_plans(version_id)
    return [serialize_plan(p) for p in plans]


@router.get("/projects/{project_id}/versions/{version_id}/plans/{plan_id}")
def get_plan(project_id: str, version_id: str, plan_id: str,
             request: Request):
    plan = _require_plan(request, project_id, version_id, plan_id)
    return serialize_plan(plan)


@router.post("/projects/{project_id}/versions/{version_id}/plans/"
             "{plan_id}/confirm")
def confirm_plan(project_id: str, version_id: str, plan_id: str,
                 body: PlanConfirmIn, request: Request):
    _require_plan(request, project_id, version_id, plan_id)
    updated = _plans(request).confirm(
        plan_id, expected_revision=body.revision, period=body.period)
    return {"confirmed": True, "plan": serialize_plan(updated)}


@router.post("/projects/{project_id}/versions/{version_id}/plans/"
             "{plan_id}/replan")
def replan_plan(project_id: str, version_id: str, plan_id: str,
                body: PlanReplanIn, request: Request):
    _require_plan(request, project_id, version_id, plan_id)
    updated = _plans(request).replan(
        plan_id, expected_revision=body.revision,
        budgets=list(body.budgets), time_limit=body.time_limit)
    return {"replanned": True, "plan": serialize_plan(updated)}
