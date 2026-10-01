"""应用配置。

数据库路径与求解并发数均可通过环境变量覆盖：
- FACILITY_DB_PATH：SQLite 文件路径（容器内默认 /data/facility.db，支持挂载卷）
- FACILITY_MAX_WORKERS：同时求解的后台作业数（默认 2）
"""

import os
from dataclasses import dataclass

DEFAULT_DB_PATH = os.environ.get("FACILITY_DB_PATH", "/data/facility.db")
DEFAULT_MAX_WORKERS = int(os.environ.get("FACILITY_MAX_WORKERS", "2"))


@dataclass(frozen=True)
class Settings:
    db_path: str = DEFAULT_DB_PATH
    max_workers: int = DEFAULT_MAX_WORKERS


settings = Settings()
