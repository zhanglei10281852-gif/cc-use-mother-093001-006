"""结算窗口和水量分录的基础契约。"""
from dataclasses import dataclass
from datetime import date


class Component:
    """平衡分量名称。漏损 = 输入侧分量之和 - 用水侧分量之和。"""

    BULK_INPUT = "bulk_input"                    # 总表计量供水量
    TRANSFER_IN = "transfer_in"                  # 跨分区调入
    TRANSFER_OUT = "transfer_out"                # 跨分区调出
    CUSTOMER_CONSUMPTION = "customer_consumption"  # 用户表注册用水量
    FIRE_USE = "fire_use"                        # 消防用水
    AUTHORIZED_UNMETERED = "authorized_unmetered"  # 其它合法未计量用水
    LOSSES = "losses"                            # 漏损水量

    #: 分量对净输入的系数
    NET_INPUT_COEFF = {
        BULK_INPUT: +1,
        TRANSFER_IN: +1,
        TRANSFER_OUT: -1,
    }
    #: 合法用水（从净输入中扣减得到漏损）
    AUTHORIZED = (CUSTOMER_CONSUMPTION, FIRE_USE, AUTHORIZED_UNMETERED)
    #: 调整单允许指向的分量
    ADJUSTABLE = (
        BULK_INPUT, TRANSFER_IN, TRANSFER_OUT,
        CUSTOMER_CONSUMPTION, FIRE_USE, AUTHORIZED_UNMETERED,
    )

    @classmethod
    def loss_sign(cls, component: str) -> int:
        """该分量增加 1 m³ 时漏损水量的变化方向。"""
        if component in cls.NET_INPUT_COEFF:
            return cls.NET_INPUT_COEFF[component]
        return -1  # 合法用水分量增加则漏损减少


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


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    zone_id: str
    category: str
    volume_m3: float

    def __post_init__(self) -> None:
        if not self.category or self.volume_m3 < 0:
            raise ValueError("水量分录无效")
