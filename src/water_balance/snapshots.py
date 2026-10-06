"""平衡版本快照、状态机与角色权限。

版本按 (zone_id, window_start) 维护有序修订号 revision：
DRAFT 草稿可反复重算；进入复核后冻结；签发后不可改，只能由管理员
重开并产生新的修订号。每次状态流转都留下不可变的历史记录。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class VersionStatus(str, Enum):
    DRAFT = "draft"                    # 草稿：可重算
    UNDER_REVIEW = "under_review"      # 复核中：等待复核意见
    ISSUED = "issued"                  # 已签发：冻结
    REOPENED = "reopened"              # 重开留痕（历史标记，新修订为 DRAFT）


class Role(str, Enum):
    ANALYST = "analyst"        # 核算员：编制、提交
    REVIEWER = "reviewer"      # 复核人：退回、签发
    ISSUER = "issuer"          # 签发人（亦可签发）
    ADMIN = "admin"            # 管理员：重开


#: 允许的状态流转：(当前状态, 目标状态) -> 允许角色
TRANSITIONS: dict[tuple[VersionStatus, VersionStatus], frozenset[Role]] = {
    (VersionStatus.DRAFT, VersionStatus.UNDER_REVIEW): frozenset({Role.ANALYST}),
    (VersionStatus.UNDER_REVIEW, VersionStatus.DRAFT): frozenset({Role.REVIEWER, Role.ADMIN}),
    (VersionStatus.UNDER_REVIEW, VersionStatus.ISSUED): frozenset({Role.REVIEWER, Role.ISSUER}),
}
#: 重开不走普通流转：只允许管理员，且强制产生新修订号
REOPEN_ROLES = frozenset({Role.ADMIN})


@dataclass(frozen=True)
class StatusEvent:
    from_status: str
    to_status: str
    actor: str
    at: str
    note: str = ""


@dataclass(frozen=True)
class Contribution:
    """计算输入摘要中的一项来源（一条读数区间、一笔调水、一个事件、一张调整单）。"""

    component: str
    source: str                 # reading | replacement | transfer | event | estimate | adjustment
    key: str
    volume_m3: float
    estimated: bool = False
    detail: str = ""
    rule_key: Optional[str] = None
    linked_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class BalanceSnapshot:
    """某一修订号的完整计算结果与输入摘要。"""

    components: dict[str, float]
    net_input: float
    authorized_consumption: float
    losses_m3: float
    loss_rate: float            # 漏损率 = 漏损 / 净输入
    warnings: tuple[str, ...]
    contributions: tuple[Contribution, ...]
    fingerprint: str
    estimated_volume_m3: float
    adjustment_volume_m3: float


@dataclass
class BalanceVersion:
    version_id: str             # f"{zone}@{start}#r{revision}"
    zone_id: str
    window_start: str
    window_end: str
    revision: int
    status: VersionStatus
    snapshot: BalanceSnapshot
    created_by: str
    created_at: str
    status_history: list[StatusEvent] = field(default_factory=list)
    issued_at: Optional[str] = None
    issued_by: Optional[str] = None
    reopen_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "zone_id": self.zone_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "revision": self.revision,
            "status": self.status.value,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "issued_at": self.issued_at,
            "issued_by": self.issued_by,
            "reopen_reason": self.reopen_reason,
            "status_history": [e.__dict__ for e in self.status_history],
            "snapshot": _snapshot_to_dict(self.snapshot),
        }


def _snapshot_to_dict(snap: BalanceSnapshot) -> dict[str, Any]:
    return {
        "components": dict(snap.components),
        "net_input": snap.net_input,
        "authorized_consumption": snap.authorized_consumption,
        "losses_m3": snap.losses_m3,
        "loss_rate": snap.loss_rate,
        "warnings": list(snap.warnings),
        "estimated_volume_m3": snap.estimated_volume_m3,
        "adjustment_volume_m3": snap.adjustment_volume_m3,
        "fingerprint": snap.fingerprint,
        "contributions": [
            {
                "component": c.component,
                "source": c.source,
                "key": c.key,
                "volume_m3": c.volume_m3,
                "estimated": c.estimated,
                "detail": c.detail,
                "rule_key": c.rule_key,
                "linked_keys": list(c.linked_keys),
            }
            for c in snap.contributions
        ],
    }


def snapshot_from_dict(data: dict[str, Any]) -> BalanceSnapshot:
    return BalanceSnapshot(
        components={k: float(v) for k, v in data["components"].items()},
        net_input=float(data["net_input"]),
        authorized_consumption=float(data["authorized_consumption"]),
        losses_m3=float(data["losses_m3"]),
        loss_rate=float(data["loss_rate"]),
        warnings=tuple(data.get("warnings", ())),
        contributions=tuple(
            Contribution(
                component=c["component"],
                source=c["source"],
                key=c["key"],
                volume_m3=float(c["volume_m3"]),
                estimated=bool(c.get("estimated", False)),
                detail=c.get("detail", ""),
                rule_key=c.get("rule_key"),
                linked_keys=tuple(c.get("linked_keys", ())),
            )
            for c in data.get("contributions", ())
        ),
        fingerprint=data["fingerprint"],
        estimated_volume_m3=float(data.get("estimated_volume_m3", 0.0)),
        adjustment_volume_m3=float(data.get("adjustment_volume_m3", 0.0)),
    )


def version_from_dict(data: dict[str, Any]) -> BalanceVersion:
    return BalanceVersion(
        version_id=data["version_id"],
        zone_id=data["zone_id"],
        window_start=data["window_start"],
        window_end=data["window_end"],
        revision=int(data["revision"]),
        status=VersionStatus(data["status"]),
        snapshot=snapshot_from_dict(data["snapshot"]),
        created_by=data["created_by"],
        created_at=data["created_at"],
        status_history=[StatusEvent(**e) for e in data.get("status_history", ())],
        issued_at=data.get("issued_at"),
        issued_by=data.get("issued_by"),
        reopen_reason=data.get("reopen_reason"),
    )


def utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def to_jsonable(obj: Any) -> Any:
    """把 dataclass/date/容器转成 JSON 可序列化结构。"""
    from dataclasses import asdict, is_dataclass
    from datetime import date as _date
    from enum import Enum

    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, _date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
