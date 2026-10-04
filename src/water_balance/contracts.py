"""领域契约：枚举、异常与基础数据结构。

保留历史的 ``SettlementWindow`` / ``LedgerEntry`` 接口不变，
平台其余模块在此之上构建。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any


# ---------------------------------------------------------------- 基础结构

@dataclass(frozen=True)
class SettlementWindow:
    zone_id: str
    starts_on: date
    ends_on: date

    def __post_init__(self) -> None:
        if self.ends_on <= self.starts_on:
            raise ValueError("结算结束日必须晚于开始日")

    @property
    def days(self) -> int:
        return (self.ends_on - self.starts_on).days

    @property
    def month(self) -> str:
        return self.starts_on.strftime("%Y-%m")


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    zone_id: str
    category: str
    volume_m3: float

    def __post_init__(self) -> None:
        if not self.category or self.volume_m3 < 0:
            raise ValueError("水量分录无效")


# ---------------------------------------------------------------- 枚举

class MeterRole(str, Enum):
    MASTER = "master"   # 分区总表
    USER = "user"       # 用户表


class UsageCategory(str, Enum):
    FIRE_WATER = "fire_water"                  # 消防用水
    AUTHORIZED_UNMETERED = "authorized_unmetered"  # 合法未计量用水


class Role(str, Enum):
    OPERATOR = "operator"  # 抄表录入、重算
    REVIEWER = "reviewer"  # 复核
    ISSUER = "issuer"      # 签发
    ADMIN = "admin"        # 重开、规则审批


class VersionState(str, Enum):
    REVIEW = "review"        # 复核中
    ISSUED = "issued"        # 已签发（不可变）
    REOPENED = "reopened"    # 已重开（等待重新核算）
    SUPERSEDED = "superseded"  # 被更新的签发版本替代，仅留痕


class AdjustmentStatus(str, Enum):
    OPEN = "open"        # 调整单已生成，尚未经签发版本确认
    APPLIED = "applied"  # 已进入签发版本


# 平衡表行项目代码
class ItemCode(str, Enum):
    MASTER_SUPPLY = "master_supply"            # 总表供入
    TRANSFER_IN = "transfer_in"                # 跨分区调入
    TRANSFER_OUT = "transfer_out"              # 跨分区调出
    BILLED_METERED = "billed_metered"          # 用户表实计水量
    ESTIMATED_METERED = "estimated_metered"    # 经审批规则估算水量
    FIRE_WATER = "fire_water"                  # 消防用水
    AUTHORIZED_UNMETERED = "authorized_unmetered"  # 其他合法未计量
    NRW = "nrw"                                # 漏损水量（残值）


# ---------------------------------------------------------------- 异常

class BalanceError(Exception):
    """业务规则违反基类。"""

    code = "balance_error"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


class NotFound(BalanceError):
    code = "not_found"


class Conflict(BalanceError):
    code = "conflict"


class PermissionDenied(BalanceError):
    code = "permission_denied"


class InvalidTransition(BalanceError):
    code = "invalid_transition"


class MissingDataError(BalanceError):
    """窗口内缺少读数且无已审批估算规则可用。"""

    code = "missing_data"


# ---------------------------------------------------------------- 计算结果结构

@dataclass(frozen=True)
class LineItem:
    code: str
    label: str
    volume_m3: float
    estimated: bool = False
    ref: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EstimateRecord:
    service_point_id: str
    meter_ids: tuple[str, ...]
    uncovered_days: int
    actual_alongside_m3: float
    volume_m3: float
    rule_id: str
    basis: dict[str, Any]


@dataclass(frozen=True)
class BalanceResult:
    zone_id: str
    month: str
    starts_on: date
    ends_on: date
    items: tuple[LineItem, ...]
    estimates: tuple[EstimateRecord, ...]
    digest: str
    totals: dict[str, float]
    indicators: dict[str, float]
    source_refs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    sp_actual: dict[str, float] = field(default_factory=dict)

    def volume(self, code: ItemCode) -> float:
        return self.totals.get(code.value, 0.0)
