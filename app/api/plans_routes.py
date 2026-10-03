"""HTTP 路由：分期建设计划（创建 / 取回 / 列出 / 确认 / 预算重排）。

与既有项目、版本、作业路由分文件放置；分期规划逻辑在 app/phasing.py，
计划状态与并发控制在 app/plans.py，本文件只做参数校验与编排。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import ApiError
from ..schemas import PlanConfirmIn, PlanCreateIn, PlanReplanIn

router = APIRouter(prefix="/api")


def _storage(req: Request):
    return req.app.state.storage


def _plans(req: Request):
    return req.app.state.plans


def _require_project(req: Request, project_id: str):
    if _storage(req).get_project(project_id) is None:
        raise ApiError("项目不存在", field="project_id", status_code=404)


def _require_version(req: Request, project_id: str, version_id: str) -> dict:
    _require_project(req, project_id)
    v = _storage(req).get_version(version_id)
    if v is None or v["project_id"] != project_id:
        raise ApiError("版本不存在或不属于该项目", field="version_id",
                       status_code=404)
    return v


@router.post(
    "/projects/{project_id}/versions/{version_id}/plans",
    status_code=201)
def create_plan(project_id: str, version_id: str, body: PlanCreateIn,
                request: Request):
    """创建（或幂等取回）一份分期建设计划。同步计算并返回。"""
    version = _require_version(request, project_id, version_id)
    if body.radius <= 0:
        raise ApiError("半径必须为正数", field="radius")
    if not body.budgets:
        raise ApiError("至少要有一期", field="budgets")
    plan, created = _plans(request).create_plan(
        project_id, version, body.radius, body.budgets,
        body.forced_station_ids, body.time_limit)
    out = _plans(request).serialize(plan, version)
    out["reused"] = not created
    return out


@router.get(
    "/projects/{project_id}/versions/{version_id}/plans")
def list_plans(project_id: str, version_id: str, request: Request):
    version = _require_version(request, project_id, version_id)
    rows = _storage(request).list_plans(version_id)
    return [_plans(request).serialize(p, version) for p in rows]


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, request: Request):
    row = _storage(request).get_plan(plan_id)
    if row is None:
        raise ApiError("计划不存在", field="plan_id", status_code=404)
    version = _storage(request).get_version(row["version_id"])
    return _plans(request).serialize(row, version)


@router.post("/plans/{plan_id}/confirm", status_code=200)
def confirm_period(plan_id: str, body: PlanConfirmIn, request: Request):
    """确认下一期（只能按顺序；带 revision 乐观锁）。"""
    updated = _plans(request).confirm_period(plan_id, body.revision)
    version = _storage(request).get_version(updated["version_id"])
    out = _plans(request).serialize(updated, version)
    out["confirmed"] = True
    return out


@router.post("/plans/{plan_id}/replan", status_code=200)
def replan(plan_id: str, body: PlanReplanIn, request: Request):
    """调整每期预算后重排。已确认期冻结不动；revision 过时 → 409。"""
    updated = _plans(request).replan(
        plan_id, body.revision, body.budgets, body.time_limit)
    version = _storage(request).get_version(updated["version_id"])
    out = _plans(request).serialize(updated, version)
    out["replanned"] = True
    return out
