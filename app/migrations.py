"""存储升级模块：建表脚本、版本化迁移与旧数据回填。

旧服务（无分期能力）的数据库直接挂上来就能启动：

- 所有建表都是 ``CREATE TABLE IF NOT EXISTS``，旧库的 projects / versions /
  solutions / jobs 四张表原样不动，只补一张 plans；
- 用 ``PRAGMA user_version`` 记录结构版本，以后再升级走同一套增量迁移；
- 旧版本居民点 JSON 里没有 ``weight`` 字段 —— 不做任何重写，读取时统一
  由 :func:`normalize_resident` 按权重 1 回填，保证旧方案结果一个字不变，
  也不要求清库。
"""

from __future__ import annotations

import json
import sqlite3

# 当前结构版本（对应 PRAGMA user_version）
SCHEMA_VERSION = 1

# 全部表定义。对旧库幂等：已存在的表不会被改动。
SCHEMA_SQL = """
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
-- 分期建设计划：挂在某个项目的某个版本上，按 (版本,半径,必开,期数,各期预算) 去重
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES versions(id),
    radius REAL NOT NULL,
    forced TEXT NOT NULL,        -- JSON 排序后的站编号数组
    periods INTEGER NOT NULL,    -- 期数
    budgets TEXT NOT NULL,       -- JSON 每期最多新建站数
    status TEXT NOT NULL,        -- active | completed | infeasible
    confirmed_periods INTEGER NOT NULL,
    locked_json TEXT NOT NULL,   -- 已确认各期的新建站编号（组的序列）
    revision INTEGER NOT NULL,   -- 乐观锁：每次确认/重排 +1
    objective TEXT NOT NULL,
    unphased_count INTEGER,      -- 同约束无分期最少站数（未知为 NULL）
    result_json TEXT NOT NULL,   -- 完整计划体（含不可行诊断）
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(version_id, radius, forced, periods, budgets)
);
CREATE INDEX IF NOT EXISTS idx_plans_version ON plans(version_id);
"""

# 每一步结构升级：from_version -> (to_version, sql)
# 旧库 user_version=0：幂等建表并补 plans 表，已有数据不受影响。
_STEPS: dict[int, tuple[int, str]] = {
    0: (1, SCHEMA_SQL),
}


def migrate(conn: sqlite3.Connection) -> int:
    """把数据库结构升级到最新版本，返回迁移后的版本号。幂等。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    while version < SCHEMA_VERSION:
        to_version, sql = _STEPS[version]
        conn.executescript(sql)
        version = to_version
        conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    return version


def normalize_resident(row: dict) -> dict:
    """旧版本居民点没有 weight 字段时按 1 处理（不修改库里的 JSON）。"""
    if "weight" not in row or row["weight"] is None:
        row = dict(row)
        row["weight"] = 1.0
    return row


def hydrate_version_row(d: dict) -> dict:
    d["residents"] = [normalize_resident(r) for r in json.loads(d["residents"])]
    d["stations"] = json.loads(d["stations"])
    return d
