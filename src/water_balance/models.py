"""领域模型：计量装置、读数、调水、消防/未计量用水、估算规则、调整单。

所有主数据都有有效区间（valid_from/valid_to，右端点 None 表示至今），
同一种业务事实用稳定的 ``key`` 标识，重复导入按 key 幂等。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


def _require(value: float, name: str) -> float:
    if value < 0:
        raise ValueError(f"{name} 不能为负数")
    return float(value)


@dataclass
class Meter:
    """计量装置（分区总表或用户表）。换表不更新本行，而是登记新表。"""

    key: str
    meter_type: str            # "bulk" 总表 | "customer" 用户表
    zone_id: str
    valid_from: date
    valid_to: Optional[date] = None
    multiplier: float = 1.0    # 倍率

    def __post_init__(self) -> None:
        if self.meter_type not in ("bulk", "customer"):
            raise ValueError("meter_type 必须是 bulk 或 customer")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("有效截止日不能早于生效日")
        if self.multiplier <= 0:
            raise ValueError("倍率必须为正")


@dataclass
class Reading:
    """周期末抄表读数。

    ``estimated=True`` 表示按审批规则估算的读数；真实读数到达后不覆盖，
    而是由服务层生成调整单。
    """

    key: str                   # 自然键，重复导入时按它去重
    meter_key: str
    read_on: date
    value: float               # 表底数（累计）
    estimated: bool = False
    source: str = "import"

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("读数不能为负")


@dataclass
class MeterReplacement:
    """换表归零：旧表末次读数与新表首次读数在同一日衔接。"""

    key: str
    old_meter_key: str
    new_meter_key: str
    replaced_on: date
    old_final_reading: float   # 旧表末次底数（归零前累计）
    new_initial_reading: float  # 新表起算底数（通常为 0）
    source: str = "import"


@dataclass
class Transfer:
    """跨分区调水。一条调水是同一笔业务事实，两侧必须成对入账。"""

    key: str
    from_zone: str
    to_zone: str
    occurred_on: date
    volume_m3: float
    valid_from: date
    valid_to: Optional[date] = None
    source: str = "import"

    def __post_init__(self) -> None:
        _require(self.volume_m3, "调水量")
        if self.from_zone == self.to_zone:
            raise ValueError("调水的源分区与目的分区必须不同")


@dataclass
class WaterEvent:
    """消防用水或其它合法未计量用水（按事件登记，量已核定）。"""

    key: str
    zone_id: str
    kind: str                  # "fire" | "authorized_unmetered"
    occurred_on: date
    volume_m3: float
    valid_from: date
    valid_to: Optional[date] = None
    source: str = "import"

    def __post_init__(self) -> None:
        if self.kind not in ("fire", "authorized_unmetered"):
            raise ValueError("kind 必须是 fire 或 authorized_unmetered")
        _require(self.volume_m3, "水量")


@dataclass
class EstimateRule:
    """经审批的缺数估算规则，按优先级在窗口内选用。"""

    key: str
    zone_id: str = "*"         # "*" 表示集团通用规则
    scope: str = "customer"    # customer | bulk | fire | authorized_unmetered
    priority: int = 100        # 数字越小优先级越高
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    method: str = "history"    # history | flat | daily_average
    parameter: float = 0.0
    approved_by: str = ""
    source: str = "import"

    def __post_init__(self) -> None:
        if self.scope not in ("customer", "bulk", "fire", "authorized_unmetered"):
            raise ValueError("估算规则适用范围无效")
        if self.method not in ("history", "flat", "daily_average"):
            raise ValueError("估算方法无效")
        if not self.approved_by:
            raise ValueError("估算规则必须有审批人")


@dataclass
class Adjustment:
    """调整单：真实读数/补录到达后形成，绝不改写旧账。

    每条调整单只针对一个已签发版本的一个分量；重算时按窗口汇总。
    """

    key: str
    zone_id: str
    window_start: date
    component: str
    delta_m3: float
    reason: str
    created_on: date
    created_by: str
    linked_keys: tuple[str, ...] = ()       # 触发该调整的读数/事件 key
    supersedes: Optional[str] = None        # 被本单取代的估算读数 key
    applied_revisions: tuple[int, ...] = ()  # 已落入哪些修订号（追加时记录）
    source: str = "system"

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("调整单必须说明原因")
        if not self.created_by:
            raise ValueError("调整单必须有创建人")
