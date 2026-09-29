"""结算窗口和水量分录的基础契约。"""
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class SettlementWindow:
    zone_id: str
    starts_on: date
    ends_on: date

    def __post_init__(self) -> None:
        if self.ends_on <= self.starts_on:
            raise ValueError("结算结束日必须晚于开始日")


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    zone_id: str
    category: str
    volume_m3: float

    def __post_init__(self) -> None:
        if not self.category or self.volume_m3 < 0:
            raise ValueError("水量分录无效")
