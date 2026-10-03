"""SQLite 持久化层。

五张表：
- projects：选址项目
- versions：项目下的数据版本（居民点 + 候选址 + 父版本指针），旧版本原样保留
  居民点可选带 weight（人口权重）；旧服务写入的数据没有该字段，取回时
  一律按 1 补齐（存储升级在读取时懒完成，不要求清库/重导）
- solutions：版本在固定 (radius, 必开集合) 下的求解结果；同参数重复求解
  直接取回已有记录（upsert，不产生重复）
- plans：挂在版本上的分期建设计划，按 (radius, 预算序列, 必开集合) 去重；
  revision 为乐观锁版本号，每次确认/重排自增
- jobs：后台作业（求解 / 半径扫描），服务重启时把未完成作业标 interrupted

所有连接 check_same_thread=False；WAL 打开。写操作都在短事务中。
旧库（无 plans 表 / 无 PRAGMA user_version）启动时由 _ensure_schema 平滑升级。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

# 作业状态
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_TIMEOUT = "timeout"
JOB_CANCELLED = "cancelled"
JOB_INTERRUPTED = "interrupted"
JOB_FAILED = "failed"

JOB_KIND_SOLVE = "solve"
JOB_KIND_SWEEP = "sweep"

_TERMINAL = {JOB_COMPLETED, JOB_TIMEOUT, JOB_CANCELLED,
             JOB_INTERRUPTED, JOB_FAILED}

# 数据库 schema 版本（旧库无 user_version，即 0）
SCHEMA_VERSION = 1

# plans 表：revision 乐观锁；参数键做幂等去重
_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    version_no INTEGER NOT NULL,
    parent_version_id TEXT,
    change_note TEXT,
    residents TEXT NOT NULL,   -- [{"id":..,"x":..,"y":..,"weight"?:..}]
    stations TEXT NOT NULL,    -- [{"id":..,"x":..,"y":..}]
    created_at REAL NOT NULL,
    UNIQUE(project_id, version_no)
);
CREATE TABLE IF NOT EXISTS solutions (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES versions(id),
    radius REAL NOT NULL,
    forced TEXT NOT NULL,      -- JSON 排序后的下标数组
    result_json TEXT NOT NULL,
    strategy TEXT NOT NULL DEFAULT 'full',
    updated_at REAL NOT NULL,
    UNIQUE(version_id, radius, forced)
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES versions(id),
    radius REAL NOT NULL,
    budgets TEXT NOT NULL,     -- JSON 每期新建上限数组
    forced TEXT NOT NULL,      -- JSON 排序后的下标数组
    result_json TEXT NOT NULL,
    confirmed_until INTEGER NOT NULL,  -- 已确认到第几期（-1=未确认，0=第1期已确认）
    state TEXT NOT NULL,      -- planning | completed
    revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(version_id, radius, budgets, forced)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    project_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL,
    progress_json TEXT,
    result_json TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_versions_project ON versions(project_id);
CREATE INDEX IF NOT EXISTS idx_plans_version ON plans(version_id);
"""


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()
        self._conn.commit()

    def _ensure_schema(self):
        """旧库平滑升级：CREATE TABLE IF NOT EXISTS 补齐新表后标记版本。

        居民点 weight 字段不做一次性数据搬迁——旧记录在读取时按 1 补齐，
        避免对挂载的长期数据目录做不可逆的批量改写。
        """
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self):
        with self._lock:
            self._conn.commit()
            self._conn.close()

    # ---------------------------------------------------------------
    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def create_project(self, name: str) -> dict:
        pid = self.new_id()
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO projects(id,name,created_at) VALUES(?,?,?)",
                (pid, name, now),
            )
            self._conn.commit()
        return {"id": pid, "name": name, "created_at": now}

    def get_project(self, pid: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id=?", (pid,)
            ).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------
    def create_version(self, project_id: str, residents: list[dict],
                       stations: list[dict], parent_version_id: str | None,
                       change_note: str | None) -> dict:
        with self._lock:
            r = self._conn.execute(
                "SELECT COALESCE(MAX(version_no),0) AS mx FROM versions WHERE project_id=?",
                (project_id,),
            ).fetchone()
            version_no = r["mx"] + 1
            vid = self.new_id()
            now = time.time()
            self._conn.execute(
                """INSERT INTO versions(id,project_id,version_no,parent_version_id,
                   change_note,residents,stations,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (vid, project_id, version_no, parent_version_id, change_note,
                 json.dumps(residents, ensure_ascii=False),
                 json.dumps(stations, ensure_ascii=False), now),
            )
            self._conn.commit()
        return self.get_version(vid)

    def get_version(self, vid: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM versions WHERE id=?", (vid,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["residents"] = self._hydrate_residents(json.loads(d["residents"]))
        d["stations"] = json.loads(d["stations"])
        return d

    def get_version_by_no(self, project_id: str, version_no: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM versions WHERE project_id=? AND version_no=?",
                (project_id, version_no),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["residents"] = self._hydrate_residents(json.loads(d["residents"]))
        d["stations"] = json.loads(d["stations"])
        return d

    @staticmethod
    def _hydrate_residents(rows: list[dict]) -> list[dict]:
        # 旧服务写入的居民点没有 weight：一律按 1 补齐
        for r in rows:
            if "weight" not in r or r["weight"] is None:
                r["weight"] = 1.0
        return rows

    def list_versions(self, project_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,version_no,parent_version_id,change_note,created_at "
                "FROM versions WHERE project_id=? ORDER BY version_no",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------
    def get_solution(self, version_id: str, radius: float,
                     forced: tuple[int, ...]):
        key = json.dumps(sorted(forced))
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM solutions WHERE version_id=? AND radius=? AND forced=?",
                (version_id, radius, key),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result_json"] = json.loads(d["result_json"])
        return d

    def upsert_solution(self, version_id: str, radius: float,
                        forced: tuple[int, ...], result: dict,
                        strategy: str) -> dict:
        key = json.dumps(sorted(forced))
        sid = self.new_id()
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO solutions(id,version_id,radius,forced,result_json,
                   strategy,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(version_id,radius,forced) DO UPDATE SET
                     result_json=excluded.result_json,
                     strategy=excluded.strategy,
                     updated_at=excluded.updated_at""",
                (sid, version_id, radius, key,
                 json.dumps(result, ensure_ascii=False), strategy, now),
            )
            self._conn.commit()
        return self.get_solution(version_id, radius, forced)

    # ---------------------------------------------------------------
    def create_job(self, kind: str, project_id: str, version_id: str,
                   params: dict) -> dict:
        jid = self.new_id()
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs(id,kind,project_id,version_id,params_json,
                   status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (jid, kind, project_id, version_id,
                 json.dumps(params, ensure_ascii=False), JOB_QUEUED, now, now),
            )
            self._conn.commit()
        return self.get_job(jid)

    def get_job(self, jid: str):
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?",
                                     (jid,)).fetchone()
        if not row:
            return None
        return self._hydrate(dict(row))

    def list_jobs(self, project_id: str | None = None) -> list[dict]:
        with self._lock:
            if project_id:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE project_id=? ORDER BY created_at",
                    (project_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at").fetchall()
        return [self._hydrate(dict(r)) for r in rows]

    @staticmethod
    def _hydrate(d: dict) -> dict:
        for col in ("params_json", "progress_json", "result_json"):
            if d.get(col):
                d[col] = json.loads(d[col])
        return d

    def update_job(self, jid: str, *, status: str | None = None,
                   progress: dict | None = None, result: dict | None = None,
                   error: str | None = None, started: bool = False):
        now = time.time()
        sets = ["updated_at=?"]
        args: list = [now]
        if status is not None:
            sets.append("status=?")
            args.append(status)
        if progress is not None:
            sets.append("progress_json=?")
            args.append(json.dumps(progress, ensure_ascii=False))
        if result is not None:
            sets.append("result_json=?")
            args.append(json.dumps(result, ensure_ascii=False))
        if error is not None:
            sets.append("error=?")
            args.append(error)
        if started:
            sets.append("started_at=?")
            args.append(now)
        if status in _TERMINAL:
            sets.append("finished_at=?")
            args.append(now)
        args.append(jid)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", args
            )
            self._conn.commit()

    def find_active_solve_job(self, version_id: str, radius: float,
                              forced: tuple[int, ...]):
        """同版本 + 同参数已有未完成作业时复用，避免重复求解。"""
        key = json.dumps(sorted(forced))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM jobs WHERE kind=? AND version_id=? AND status IN (?,?)",
                (JOB_KIND_SOLVE, version_id, JOB_QUEUED, JOB_RUNNING),
            ).fetchall()
            for r in rows:
                full = self.get_job(r["id"])
                p = full["params_json"]
                if p.get("radius") == radius and \
                        sorted(p.get("forced", [])) == sorted(forced):
                    return full
        return None

    def recover_interrupted(self) -> int:
        """启动时把 queued/running 作业全部标记 interrupted。返回条数。"""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE jobs SET status=?, finished_at=?, updated_at=?,
                   error=? WHERE status IN (?,?)""",
                (JOB_INTERRUPTED, now, now,
                 "服务重启，作业未完成", JOB_QUEUED, JOB_RUNNING),
            )
            self._conn.commit()
            return cur.rowcount

    # ---------------------------------------------------------------
    # 分期计划
    # ---------------------------------------------------------------
    @staticmethod
    def _plan_key(radius: float, budgets, forced) -> tuple:
        return (radius, json.dumps(list(budgets)),
                json.dumps(sorted(forced)))

    def create_plan(self, version_id: str, radius: float, budgets,
                    forced, result: dict, confirmed_until: int,
                    state: str = "planning") -> dict | None:
        """按 (version, radius, budgets, forced) 幂等创建。

        已存在同参数计划 → 返回 None（调用方改为取回旧记录，不重复
        产生记录）。
        """
        pid = self.new_id()
        now = time.time()
        _, bkey, fkey = self._plan_key(radius, budgets, forced)
        with self._lock:
            exists = self._conn.execute(
                "SELECT id FROM plans WHERE version_id=? AND radius=? "
                "AND budgets=? AND forced=?",
                (version_id, radius, bkey, fkey),
            ).fetchone()
            if exists:
                return None
            self._conn.execute(
                """INSERT INTO plans(id,version_id,radius,budgets,forced,
                   result_json,confirmed_until,state,revision,
                   created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, version_id, radius, bkey, fkey,
                 json.dumps(result, ensure_ascii=False),
                 confirmed_until, state, 1, now, now),
            )
            self._conn.commit()
        return self.get_plan(pid)

    def get_plan(self, plan_id: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        return self._hydrate_plan(dict(row)) if row else None

    def find_plan(self, version_id: str, radius: float, budgets, forced):
        _, bkey, fkey = self._plan_key(radius, budgets, forced)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM plans WHERE version_id=? AND radius=? "
                "AND budgets=? AND forced=?",
                (version_id, radius, bkey, fkey),
            ).fetchone()
        return self._hydrate_plan(dict(row)) if row else None

    def list_plans(self, version_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM plans WHERE version_id=? ORDER BY created_at",
                (version_id,),
            ).fetchall()
        return [self._hydrate_plan(dict(r)) for r in rows]

    @staticmethod
    def _hydrate_plan(d: dict) -> dict:
        d["budgets"] = json.loads(d["budgets"])
        d["forced"] = json.loads(d["forced"])
        d["result_json"] = json.loads(d["result_json"])
        return d

    def cas_plan(self, plan_id: str, expected_revision: int,
                 *, result: dict | None = None, budgets=None,
                 confirmed_until: int | None = None,
                 state: str | None = None) -> dict | None:
        """乐观锁条件更新：仅当 revision == expected_revision 时写入并
        把 revision +1。冲突（计划不存在 / revision 过时）返回 None，
        调用方据此报 409，绝不静默覆盖。
        """
        now = time.time()
        sets = ["revision=revision+1", "updated_at=?"]
        args: list = [now]
        if result is not None:
            sets.append("result_json=?")
            args.append(json.dumps(result, ensure_ascii=False))
        if budgets is not None:
            sets.append("budgets=?")
            args.append(json.dumps(list(budgets)))
        if confirmed_until is not None:
            sets.append("confirmed_until=?")
            args.append(confirmed_until)
        if state is not None:
            sets.append("state=?")
            args.append(state)
        args += [plan_id, expected_revision]
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE plans SET {', '.join(sets)} WHERE id=? "
                f"AND revision=?", args)
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_plan(plan_id)
