"""Pydantic 请求/响应模型与字段级校验。

所有 422 错误统一为 {"error": "...", "field": "具体字段路径"}，
业务层（强制站编号不存在等）同样给出 field 指向。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class PointIn(BaseModel):
    # 编号：仅要求非空字符串；不允许同批次内重复
    id: str = Field(min_length=1)
    x: float
    y: float
    # 人口权重：该点代表多少人；不填按 1 算（必须为正数）
    weight: float = Field(default=1.0)

    @field_validator("weight")
    @classmethod
    def _positive_weight(cls, v: float):
        if v <= 0:
            raise ValueError("人口权重必须为正数")
        return v


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)


class VersionCreate(BaseModel):
    residents: list[PointIn]
    stations: list[PointIn]
    change_note: str | None = None

    @field_validator("residents")
    @classmethod
    def _unique_resident_ids(cls, v: list[PointIn]):
        ids = [p.id for p in v]
        if len(ids) != len(set(ids)):
            dup = {i for i in ids if ids.count(i) > 1}
            raise ValueError(f"居民点编号重复: {sorted(dup)}")
        return v

    @field_validator("stations")
    @classmethod
    def _unique_station_ids(cls, v: list[PointIn]):
        ids = [p.id for p in v]
        if len(ids) != len(set(ids)):
            dup = {i for i in ids if ids.count(i) > 1}
            raise ValueError(f"候选址编号重复: {sorted(dup)}")
        return v


class VersionDerive(BaseModel):
    """在某版本上增删居民点（高频操作）。只允许调整居民点，候选址沿用父版本。"""
    add_residents: list[PointIn] = Field(default_factory=list)
    remove_resident_ids: list[str] = Field(default_factory=list)
    change_note: str | None = None


class SolveRequestIn(BaseModel):
    radius: float
    forced_station_ids: list[str] = Field(default_factory=list)
    time_limit: float | None = Field(default=None, gt=0)


class SweepRequestIn(BaseModel):
    radii: list[float]
    forced_station_ids: list[str] = Field(default_factory=list)
    time_limit: float | None = Field(default=None, gt=0)

    @field_validator("radii")
    @classmethod
    def _radii_rules(cls, v: list[float]):
        if not v:
            raise ValueError("半径列表不能为空")
        if any(r <= 0 for r in v):
            raise ValueError("半径必须全部为正数")
        if v != sorted(v):
            raise ValueError("半径需按从小到大排序")
        if len(v) != len(set(v)):
            raise ValueError("半径不能重复")
        return v


class ProjectOut(BaseModel):
    id: str
    name: str
    created_at: float


class VersionOut(BaseModel):
    id: str
    project_id: str
    version_no: int
    parent_version_id: str | None
    change_note: str | None
    residents: list[dict]
    stations: list[dict]
    created_at: float


class PlanCreateIn(BaseModel):
    radius: float
    # 每期最多新建几座站；期数 = 列表长度（至少 1 期，每项为正整数）
    budgets: list[int]
    forced_station_ids: list[str] = Field(default_factory=list)
    time_limit: float | None = Field(default=None, gt=0)

    @field_validator("budgets")
    @classmethod
    def _budgets_rules(cls, v: list[int]):
        if not v:
            raise ValueError("至少要有一期")
        if any(b <= 0 for b in v):
            raise ValueError("每期新建站数上限必须为正整数")
        return v


class PlanConfirmIn(BaseModel):
    # 乐观锁：必须带上取回计划时看到的 revision，过期即冲突
    revision: int


class PlanReplanIn(BaseModel):
    # 调整每期预算后重排；已确认期冻结，只重排之后的期
    budgets: list[int]
    revision: int
    time_limit: float | None = Field(default=None, gt=0)

    @field_validator("budgets")
    @classmethod
    def _budgets_rules(cls, v: list[int]):
        if not v:
            raise ValueError("至少要有一期")
        if any(b <= 0 for b in v):
            raise ValueError("每期新建站数上限必须为正整数")
        return v
