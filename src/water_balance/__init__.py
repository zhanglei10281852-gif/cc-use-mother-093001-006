"""分区水量平衡与漏损核算平台。"""
from .contracts import Component, LedgerEntry, SettlementWindow
from .engine import (
    IDENTITY_TOLERANCE,
    compute_balance,
    verify_identity,
    verify_transfer_pairs,
)
from .ledger import Ledger, LedgerError, month_window
from .models import (
    Adjustment,
    EstimateRule,
    Meter,
    MeterReplacement,
    Reading,
    Transfer,
    WaterEvent,
)
from .snapshots import (
    BalanceSnapshot,
    BalanceVersion,
    Contribution,
    Role,
    VersionStatus,
)
from .store import IdempotencyConflict, Store

__all__ = [
    "Component", "LedgerEntry", "SettlementWindow",
    "compute_balance", "verify_identity", "verify_transfer_pairs",
    "IDENTITY_TOLERANCE",
    "Ledger", "LedgerError", "month_window",
    "Adjustment", "EstimateRule", "Meter", "MeterReplacement",
    "Reading", "Transfer", "WaterEvent",
    "BalanceSnapshot", "BalanceVersion", "Contribution",
    "Role", "VersionStatus",
    "IdempotencyConflict", "Store",
]
