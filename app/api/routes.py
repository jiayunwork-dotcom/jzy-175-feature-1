"""HTTP 路由层：项目、版本、求解作业、扫描作业。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import ApiError
from ..incremental import PriorSolution
from ..schemas import (
    ProjectCreate,
    SolveRequestIn,
    SweepRequestIn,
    VersionCreate,
    VersionDerive,
)
from ..storage import JOB_INTERRUPTED

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# 依赖辅助
# ---------------------------------------------------------------------------

def _storage(req: Request):
    return req.app.state.storage


def _jobs(req: Request):
    return req.app.state.jobs


def _require_project(req: Request, project_id: str) -> dict:
    p = _storage(req).get_project(project_id)
    if p is None:
        raise ApiError("项目不存在", field="project_id", status_code=404)
    return p


def _require_version(req: Request, project_id: str, version_id: str) -> dict:
    _require_project(req, project_id)
    v = _storage(req).get_version(version_id)
    if v is None or v["project_id"] != project_id:
        raise ApiError("版本不存在或不属于该项目", field="version_id",
                       status_code=404)
    return v


def _validate_payload(req: Request, body: VersionCreate):
    """字段级业务校验：空集合 / 半径等规则在对应求解入口再校验半径。"""
    if not body.residents:
        raise ApiError("居民点不能为空", field="residents")
    if not body.stations:
        raise ApiError("候选址不能为空", field="stations")
    return body


def _check_forced(version: dict, forced_ids: list[str]):
    known = {s["id"] for s in version["stations"]}
    missing = [f for f in forced_ids if f not in known]
    if missing:
        raise ApiError(
            f"点名的站编号不存在: {missing}",
            field="forced_station_ids",
            status_code=404,
            details={"unknown": missing},
        )


def _prior_solution(req: Request, version: dict, radius: float,
                    forced_ids: list[str]):
    """从父版本取同参数的已证明最优方案，供增量热启动。"""
    parent_id = version.get("parent_version_id")
    if not parent_id:
        return None
    storage = _storage(req)
    parent = storage.get_version(parent_id)
    if parent is None:
        return None
    # 父版本候选址顺序 → 下标
    parent_index = {s["id"]: i for i, s in enumerate(parent["stations"])}
    forced = tuple(sorted(parent_index[f] for f in forced_ids
                          if f in parent_index))
    if len(forced) != len(set(forced_ids)):
        return None  # 必开站在父版本不存在 → 不热启动
    sol = storage.get_solution(parent["id"], radius, forced)
    if sol is None or not sol["result_json"].get("proven_optimal"):
        return None
    chosen_ids = [parent["stations"][j]["id"]
                  for j in sol["result_json"]["chosen"]]
    # 映射成"当前版本下标"（derive 不改候选址，顺序一致）
    cur_index = {s["id"]: i for i, s in enumerate(version["stations"])}
    if any(cid not in cur_index for cid in chosen_ids):
        return None
    return PriorSolution(
        radius=radius,
        chosen_station_ids=tuple(cur_index[cid] for cid in chosen_ids),
        residents=tuple((r["id"], r["x"], r["y"])
                        for r in parent["residents"]),
        stations=tuple((s["id"], s["x"], s["y"])
                       for s in parent["stations"]),
        forced=forced,
    )


# ---------------------------------------------------------------------------
# 项目与版本
# ---------------------------------------------------------------------------

@router.post("/projects", status_code=201)
def create_project(req: Request, body: ProjectCreate):
    return _storage(req).create_project(body.name)


@router.get("/projects/{project_id}")
def get_project(project_id: str, request: Request):
    return _require_project(request, project_id)


@router.get("/projects/{project_id}/versions")
def list_versions(project_id: str, request: Request):
    _require_project(request, project_id)
    return _storage(request).list_versions(project_id)


@router.post("/projects/{project_id}/versions", status_code=201)
def create_version(project_id: str, body: VersionCreate, request: Request):
    _require_project(request, project_id)
    _validate_payload(request, body)
    residents = [p.model_dump() for p in body.residents]
    stations = [p.model_dump() for p in body.stations]
    return _storage(request).create_version(
        project_id, residents, stations, None, body.change_note)


@router.post("/projects/{project_id}/versions/from/{version_id}",
             status_code=201)
def derive_version(project_id: str, version_id: str, body: VersionDerive,
                   request: Request):
    """在已有版本上增删居民点，生成新版本（候选址原样沿用）。"""
    parent = _require_version(request, project_id, version_id)

    existing = {r["id"]: r for r in parent["residents"]}
    add_ids = {p.id for p in body.add_residents}
    # 新增编号不能与现存居民点撞号（现存编号请先删除再重建）
    clash = sorted(add_ids & set(existing))
    if clash:
        raise ApiError(f"新增居民点编号已存在: {clash}",
                       field="add_residents")
    add_dups = [i for i in add_ids if list(add_ids).count(i) > 1]
    if add_dups:
        raise ApiError(f"新增居民点编号重复: {sorted(set(add_dups))}",
                       field="add_residents")

    remove = set(body.remove_resident_ids)
    unknown = sorted(remove - set(existing))
    if unknown:
        raise ApiError(f"要删除的居民点编号不存在: {unknown}",
                       field="remove_resident_ids", status_code=404)

    kept = [r for rid, r in existing.items() if rid not in remove]
    merged = kept + [p.model_dump() for p in body.add_residents]
    if not merged:
        raise ApiError("删除后居民点不能为空", field="remove_resident_ids")

    return _storage(request).create_version(
        project_id, merged, parent["stations"], parent["id"],
        body.change_note or "增量增删居民点")


@router.get("/projects/{project_id}/versions/{version_id}")
def get_version(project_id: str, version_id: str, request: Request):
    return _require_version(request, project_id, version_id)


@router.get("/projects/{project_id}/versions/{version_id}/diff")
def diff_versions(project_id: str, version_id: str,
                  against_version_id: str, request: Request):
    """两个版本的居民点/候选址差异，便于取回对比。"""
    v1 = _require_version(request, project_id, version_id)
    v2 = _require_version(request, project_id, against_version_id)

    def idx(rows):
        return {r["id"]: r for r in rows}

    r1, r2 = idx(v1["residents"]), idx(v2["residents"])
    s1, s2 = idx(v1["stations"]), idx(v2["stations"])
    return {
        "left_version_no": v1["version_no"],
        "right_version_no": v2["version_no"],
        "residents_added": [r2[k] for k in r2.keys() - r1.keys()],
        "residents_removed": [r1[k] for k in r1.keys() - r2.keys()],
        "residents_changed": [
            {"id": k, "left": r1[k], "right": r2[k]}
            for k in r1.keys() & r2.keys() if r1[k] != r2[k]
        ],
        "stations_added": [s2[k] for k in s2.keys() - s1.keys()],
        "stations_removed": [s1[k] for k in s1.keys() - s2.keys()],
        "stations_changed": [
            {"id": k, "left": s1[k], "right": s2[k]}
            for k in s1.keys() & s2.keys() if s1[k] != s2[k]
        ],
    }


# ---------------------------------------------------------------------------
# 求解 / 扫描作业
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/versions/{version_id}/solve",
             status_code=202)
def start_solve(project_id: str, version_id: str, body: SolveRequestIn,
                request: Request):
    version = _require_version(request, project_id, version_id)
    if body.radius <= 0:
        raise ApiError("半径必须为正数", field="radius")
    _check_forced(version, body.forced_station_ids)

    prior = _prior_solution(request, version, body.radius,
                            body.forced_station_ids)
    out = _jobs(request).submit_solve(
        project_id, version, body.radius, body.forced_station_ids,
        body.time_limit, prior)

    if out.get("already") and out.get("reused_solution") is not None:
        sol = out["reused_solution"]
        return {"reused": True, "solution_id": sol["id"],
                "result": sol["result_json"], "strategy": sol["strategy"]}
    if out.get("already"):
        return {"reused": True, "job": _serialize_job(out["job"])}
    return {"reused": False, "job": _serialize_job(out["job"])}


@router.post("/projects/{project_id}/versions/{version_id}/sweep",
             status_code=202)
def start_sweep(project_id: str, version_id: str, body: SweepRequestIn,
                request: Request):
    version = _require_version(request, project_id, version_id)
    _check_forced(version, body.forced_station_ids)
    out = _jobs(request).submit_sweep(
        project_id, version, body.radii, body.forced_station_ids,
        body.time_limit)
    return {"job": _serialize_job(out["job"])}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = _storage(request).get_job(job_id)
    if job is None:
        raise ApiError("作业不存在", field="job_id", status_code=404)
    return _serialize_job(job)


@router.post("/jobs/{job_id}/cancel", status_code=200)
def cancel_job(job_id: str, request: Request):
    job = _storage(request).get_job(job_id)
    if job is None:
        raise ApiError("作业不存在", field="job_id", status_code=404)
    ok = _jobs(request).cancel(job_id)
    if not ok:
        raise ApiError(
            f"作业已处于终态（{job['status']}），无法取消",
            field="job_id", status_code=409)
    return {"cancelling": True, "job_id": job_id}


@router.get("/projects/{project_id}/jobs")
def list_jobs(project_id: str, request: Request):
    _require_project(request, project_id)
    return [_serialize_job(j)
            for j in _storage(request).list_jobs(project_id)]


def _serialize_job(job: dict) -> dict:
    return {
        "id": job["id"],
        "kind": job["kind"],
        "project_id": job["project_id"],
        "version_id": job["version_id"],
        "params": job["params_json"],
        "status": job["status"],
        "progress": job.get("progress_json"),
        "result": job.get("result_json"),
        "error": job.get("error"),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "interrupted": job["status"] == JOB_INTERRUPTED,
    }
