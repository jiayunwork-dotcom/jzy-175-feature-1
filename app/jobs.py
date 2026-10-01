"""后台作业调度：提交、进度、取消、时限、半径扫描与重启恢复。

- 有界线程池（FACILITY_MAX_WORKERS，默认 2）：多个作业同时跑互不干扰，
  每个作业有独立 cancel_event 与独立求解器实例。
- 求解作业：完成后把结果写入 solutions 表（同参数天然去重）。
- 扫描作业：固定必开集合，一串半径逐个求解，共享一个作业级时限，
  超时前算完的半径全部保留，未算的标 not_computed。
- 取消：协作式（threading.Event），求解器在节点循环中轮询。
- 重启：Storage.recover_interrupted 把未落终态的作业标 interrupted。
"""

from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from .coverage import Point, build_coverage
from .incremental import PriorSolution, solve_incremental
from .solver import (
    STATUS_CANCELLED,
    STATUS_INFEASIBLE,
    STATUS_OPTIMAL,
    STATUS_TIMEOUT,
    Progress,
)
from .storage import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_KIND_SOLVE,
    JOB_KIND_SWEEP,
    JOB_RUNNING,
    JOB_TIMEOUT,
    Storage,
)

# 求解器状态 → 作业状态
_STATUS_MAP = {
    STATUS_OPTIMAL: JOB_COMPLETED,
    STATUS_TIMEOUT: JOB_TIMEOUT,
    STATUS_CANCELLED: JOB_CANCELLED,
    STATUS_INFEASIBLE: JOB_COMPLETED,  # 无解也是正常完成的终态结果
}


class JobManager:
    def __init__(self, storage: Storage, max_workers: int = 2):
        self.storage = storage
        self.max_workers = max_workers
        self._pool: ThreadPoolExecutor | None = None
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        # 提交串行化：两个"同版本同参数"的并发请求不会各自创建一个作业
        self._submit_lock = threading.Lock()

    def start(self):
        recovered = self.storage.recover_interrupted()
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="facility-job",
        )
        return recovered

    def shutdown(self, wait: bool = False):
        # 关闭时不等待长作业；未完成者由下次启动的 recover_interrupted 收尾
        with self._lock:
            for ev in self._events.values():
                ev.set()
        if self._pool is not None:
            self._pool.shutdown(wait=wait, cancel_futures=not wait)
            self._pool = None

    # ------------------------------------------------------------------
    def _event_for(self, jid: str) -> threading.Event:
        with self._lock:
            ev = self._events.get(jid)
            if ev is None:
                ev = threading.Event()
                self._events[jid] = ev
            return ev

    def cancel(self, jid: str) -> bool:
        """请求取消。已在终态的作业返回 False。"""
        job = self.storage.get_job(jid)
        if not job:
            return False
        if job["status"] in (JOB_COMPLETED, JOB_TIMEOUT, JOB_CANCELLED,
                             JOB_INTERRUPTED, JOB_FAILED):
            return False
        self._event_for(jid).set()
        return True

    # ------------------------------------------------------------------
    def submit_solve(self, project_id: str, version: dict, radius: float,
                     forced_ids: list[int], time_limit: float | None,
                     prior: PriorSolution | None) -> dict:
        forced = self._forced_indices(version, forced_ids)
        params = {"radius": radius, "forced": forced,
                  "forced_ids": forced_ids, "time_limit": time_limit}

        with self._submit_lock:
            # 1) 已有已完成方案 → 直接复用（同版本同参数不重复求解、不重复记录）
            existing = self.storage.get_solution(version["id"], radius,
                                                 tuple(forced))
            if existing is not None:
                return {"job": None, "reused_solution": existing,
                        "already": True}

            # 2) 已有在跑的同参数作业 → 复用该作业
            active = self.storage.find_active_solve_job(version["id"], radius,
                                                        tuple(forced))
            if active is not None:
                return {"job": active, "reused_solution": None,
                        "already": True}

            job = self.storage.create_job(JOB_KIND_SOLVE, project_id,
                                          version["id"], params)
            self._pool.submit(self._run_solve, job["id"], dict(version),
                              radius, tuple(forced), time_limit, prior)
            return {"job": self.storage.get_job(job["id"]),
                    "reused_solution": None, "already": False}

    def submit_sweep(self, project_id: str, version: dict,
                     radii: list[float], forced_ids: list[int],
                     time_limit: float | None) -> dict:
        forced = self._forced_indices(version, forced_ids)
        params = {"radii": radii, "forced": forced,
                  "forced_ids": forced_ids, "time_limit": time_limit}
        job = self.storage.create_job(JOB_KIND_SWEEP, project_id,
                                      version["id"], params)
        self._pool.submit(self._run_sweep, job["id"], dict(version),
                          list(radii), tuple(forced), time_limit)
        return {"job": self.storage.get_job(job["id"])}

    # ------------------------------------------------------------------
    @staticmethod
    def _forced_indices(version: dict, forced_ids: list[int]) -> list[int]:
        ids = [s["id"] for s in version["stations"]]
        return [ids.index(fid) for fid in forced_ids]

    @staticmethod
    def _points(version: dict):
        residents = [Point(r["x"], r["y"]) for r in version["residents"]]
        stations = [Point(s["x"], s["y"]) for s in version["stations"]]
        return residents, stations

    def _make_progress_cb(self, jid: str, base: dict | None = None):
        def cb(p: Progress):
            prog = {
                "explored_nodes": p.explored_nodes,
                "best_count": p.best_count,
                "lower_bound": p.lower_bound,
            }
            if base:
                prog.update(base)
            self.storage.update_job(jid, status=JOB_RUNNING, progress=prog)
        return cb

    def _run_solve(self, jid: str, version: dict, radius: float,
                   forced: tuple[int, ...], time_limit: float | None,
                   prior: PriorSolution | None):
        ev = self._event_for(jid)
        self.storage.update_job(jid, status=JOB_RUNNING, started=True)
        try:
            residents, stations = self._points(version)
            coverage = build_coverage(residents, stations, radius)
            current_residents = [(r["id"], r["x"], r["y"])
                                 for r in version["residents"]]
            current_stations = [(s["id"], s["x"], s["y"])
                                for s in version["stations"]]
            forced_set = frozenset(forced)

            try:
                outcome = solve_incremental(
                    prior, coverage,
                    forced=forced_set,
                    time_limit=time_limit,
                    cancel_event=ev,
                    progress_cb=self._make_progress_cb(jid),
                    current_radius=radius,
                    current_residents=current_residents,
                    current_stations=current_stations,
                )
                result = outcome.result
                strategy = outcome.strategy
            except Exception:
                # 增量热启动的任何意外都不得影响正确性：退回全量冷启动
                outcome = solve_incremental(
                    None, coverage,
                    forced=forced_set,
                    time_limit=time_limit,
                    cancel_event=ev,
                    progress_cb=self._make_progress_cb(jid),
                    current_radius=radius,
                    current_residents=current_residents,
                    current_stations=current_stations,
                )
                result = outcome.result
                strategy = "cold"

            result_d = result.to_dict()
            result_d["strategy"] = strategy
            job_status = _STATUS_MAP[result.status]
            # 取消事件可能恰好在最优返回前被设置：求解器已自行处理状态
            self.storage.update_job(jid, status=job_status,
                                    result=result_d,
                                    progress=self._final_progress(result_d))
            # 只有成功完成（最优 / 无解）的结果落方案表；超时取消不落
            if job_status == JOB_COMPLETED:
                self.storage.upsert_solution(
                    version["id"], radius, forced, result_d, strategy)
        except Exception as exc:
            self.storage.update_job(jid, status=JOB_FAILED,
                                    error=f"{exc}\n{traceback.format_exc()}")
        finally:
            with self._lock:
                self._events.pop(jid, None)

    @staticmethod
    def _final_progress(result_d: dict) -> dict:
        return {
            "explored_nodes": result_d.get("explored_nodes", 0),
            "best_count": result_d.get("station_count"),
            "lower_bound": result_d.get("lower_bound", 0),
        }

    def _run_sweep(self, jid: str, version: dict, radii: list[float],
                   forced: tuple[int, ...], time_limit: float | None):
        ev = self._event_for(jid)
        self.storage.update_job(jid, status=JOB_RUNNING, started=True)
        try:
            residents, stations = self._points(version)
            steps = []
            overall: str = JOB_COMPLETED
            deadline = time.perf_counter() + time_limit \
                if time_limit and time_limit > 0 else float("inf")

            for idx, radius in enumerate(radii):
                remaining = deadline - time.perf_counter()
                if ev.is_set():
                    overall = JOB_CANCELLED
                    for r in radii[idx:]:
                        steps.append(self._skip_step(r, "cancelled"))
                    break
                if remaining <= 0:
                    overall = JOB_TIMEOUT
                    for r in radii[idx:]:
                        steps.append(self._skip_step(r, "not_computed"))
                    break
                coverage = build_coverage(residents, stations, radius)
                # 扫描内的每个半径都从头精解；共享作业级剩余时限
                outcome = solve_incremental(
                    None, coverage,
                    forced=frozenset(forced),
                    time_limit=remaining,
                    cancel_event=ev,
                    progress_cb=None,
                    current_radius=radius,
                    current_residents=[(r["id"], r["x"], r["y"])
                                       for r in version["residents"]],
                    current_stations=[(s["id"], s["x"], s["y"])
                                      for s in version["stations"]],
                )
                rd = outcome.result.to_dict()
                step = {
                    "radius": radius,
                    "status": outcome.result.status,
                    "station_count": rd["station_count"],
                    "chosen": rd["chosen"],
                    "lower_bound": rd["lower_bound"],
                    "gap": rd["gap"],
                    "proven_optimal": rd["proven_optimal"],
                    "uncovered": rd["uncovered"],
                }
                steps.append(step)
                self.storage.update_job(jid, status=JOB_RUNNING, progress={
                    "completed": idx + 1,
                    "total": len(radii),
                    "last_radius": radius,
                    "last_station_count": rd["station_count"],
                })
                if outcome.result.status == STATUS_TIMEOUT:
                    overall = JOB_TIMEOUT
                    for r in radii[idx + 1:]:
                        steps.append(self._skip_step(r, "not_computed"))
                    break
                if outcome.result.status == STATUS_CANCELLED:
                    overall = JOB_CANCELLED
                    for r in radii[idx + 1:]:
                        steps.append(self._skip_step(r, "cancelled"))
                    break

            result_d = {"status": overall, "steps": steps,
                        "forced": list(forced)}
            self.storage.update_job(jid, status=overall, result=result_d)
        except Exception as exc:
            self.storage.update_job(jid, status=JOB_FAILED,
                                    error=f"{exc}\n{traceback.format_exc()}")
        finally:
            with self._lock:
                self._events.pop(jid, None)

    @staticmethod
    def _skip_step(radius: float, why: str) -> dict:
        return {"radius": radius, "status": why, "station_count": None,
                "chosen": [], "lower_bound": None, "gap": None,
                "proven_optimal": False, "uncovered": []}
