"""平衡版本台账：修订号管理、状态机、调整单入账与影响追踪。

* ``recompute`` 只写草稿修订；已签发版本永不被覆盖，必须由管理员
  :meth:`Ledger.reopen` 后产生新修订号；
* 迟到的真实读数经 :meth:`Ledger.post_late_reading` 入账：真实读数
  照常登记，被取代的估算读数标记 supersedes，并生成一张调整单记录
  分量差额与原因；
* ``trace_impacts`` 逐修订比较分量与漏损率，并给出每张调整单对漏损
  水量的方向性影响。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

from .contracts import Component as C
from .engine import (
    HistoryProvider,
    _meter_consumption,
    compute_balance,
)
from .models import Adjustment, Reading
from .snapshots import (
    REOPEN_ROLES,
    TRANSITIONS,
    BalanceSnapshot,
    BalanceVersion,
    Role,
    StatusEvent,
    VersionStatus,
    utcnow_iso,
)
from .store import Store


class LedgerError(Exception):
    """台账操作违反状态或权限约束。"""


def month_window(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        return start, date(year + 1, 1, 1)
    return start, date(year, month + 1, 1)


class Ledger:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.versions: dict[tuple[str, date], list[BalanceVersion]] = {}

    # ------------------------------------------------------------------
    # 版本编制
    # ------------------------------------------------------------------
    def recompute(self, zone_id: str, start: date, end: date, *,
                  actor: str = "system",
                  history: Optional[HistoryProvider] = None) -> BalanceVersion:
        snap = compute_balance(self.store, zone_id, start, end, history=history)
        series = self.versions.setdefault((zone_id, start), [])
        now = utcnow_iso()
        if series:
            latest = series[-1]
            if latest.status is VersionStatus.ISSUED:
                raise LedgerError(
                    f"{latest.version_id} 已签发并冻结；如需重算请由管理员重开")
            if latest.status is VersionStatus.UNDER_REVIEW:
                raise LedgerError(
                    f"{latest.version_id} 复核中，须先退回草稿才能重算")
            # DRAFT：草稿可反复重算，就地替换计算结果，状态历史保留
            latest.snapshot = snap
            return latest
        version = BalanceVersion(
            version_id=self._vid(zone_id, start, 1),
            zone_id=zone_id,
            window_start=start.isoformat(),
            window_end=end.isoformat(),
            revision=1,
            status=VersionStatus.DRAFT,
            snapshot=snap,
            created_by=actor,
            created_at=now,
        )
        series.append(version)
        return version

    def get(self, zone_id: str, start: date,
            revision: Optional[int] = None) -> BalanceVersion:
        series = self.versions.get((zone_id, start))
        if not series:
            raise LedgerError(f"无 {zone_id} @ {start} 的平衡版本")
        if revision is None:
            return series[-1]
        for v in series:
            if v.revision == revision:
                return v
        raise LedgerError(f"修订号 r{revision} 不存在")

    def list_versions(self, zone_id: str, start: date) -> list[BalanceVersion]:
        return list(self.versions.get((zone_id, start), ()))

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------
    def transition(self, zone_id: str, start: date, to_status: VersionStatus,
                   actor: str, role: Role, note: str = "") -> BalanceVersion:
        version = self.get(zone_id, start)
        allowed = TRANSITIONS.get((version.status, to_status))
        if allowed is None:
            raise LedgerError(f"不允许 {version.status.value} -> {to_status.value}")
        if role not in allowed:
            raise LedgerError(
                f"角色 {role.value} 无权执行 {version.status.value} -> {to_status.value}")
        self._append_status(version, to_status, actor, note)
        if to_status is VersionStatus.ISSUED:
            version.issued_at = version.status_history[-1].at
            version.issued_by = actor
        return version

    def submit(self, zone_id, start, actor, note=""):
        return self.transition(zone_id, start, VersionStatus.UNDER_REVIEW,
                               actor, Role.ANALYST, note)

    def send_back(self, zone_id, start, actor, role: Role, note=""):
        return self.transition(zone_id, start, VersionStatus.DRAFT,
                               actor, role, note)

    def issue(self, zone_id, start, actor, role: Role, note=""):
        return self.transition(zone_id, start, VersionStatus.ISSUED,
                               actor, role, note)

    def reopen(self, zone_id: str, start: date, actor: str, role: Role,
               reason: str, end: Optional[date] = None,
               history: Optional[HistoryProvider] = None) -> BalanceVersion:
        if role not in REOPEN_ROLES:
            raise LedgerError(f"只有管理员可以重开，当前角色 {role.value}")
        if not reason:
            raise LedgerError("重开必须填写原因")
        series = self.versions.get((zone_id, start))
        if not series:
            raise LedgerError("版本不存在，无需重开")
        latest = series[-1]
        if latest.status is not VersionStatus.ISSUED:
            raise LedgerError("只有已签发版本才需要重开")
        win_end = end or date.fromisoformat(latest.window_end)
        self._append_status(latest, VersionStatus.REOPENED, actor, reason)
        snap = compute_balance(self.store, zone_id, start, win_end, history=history)
        new_rev = BalanceVersion(
            version_id=self._vid(zone_id, start, latest.revision + 1),
            zone_id=zone_id,
            window_start=start.isoformat(),
            window_end=win_end.isoformat(),
            revision=latest.revision + 1,
            status=VersionStatus.DRAFT,
            snapshot=snap,
            created_by=actor,
            created_at=utcnow_iso(),
            reopen_reason=reason,
        )
        series.append(new_rev)
        return new_rev

    @staticmethod
    def _append_status(version: BalanceVersion, to_status: VersionStatus,
                       actor: str, note: str) -> None:
        version.status_history.append(StatusEvent(
            from_status=version.status.value,
            to_status=to_status.value,
            actor=actor,
            at=utcnow_iso(),
            note=note,
        ))
        version.status = to_status

    # ------------------------------------------------------------------
    # 迟到真实读数 -> 调整单
    # ------------------------------------------------------------------
    def post_late_reading(self, real: Reading, window_start: date, window_end: date,
                          actor: str, reason: str,
                          created_on: Optional[date] = None) -> Adjustment:
        """登记迟到真实读数，并就其取代的估算生成一张调整单。

        - 同一表计、同一读数日已有估算读数时，估算读数被本单 ``supersedes``；
        - 调整量 = 真实读数口径下该表窗口用量 − 原估算口径用量；
        - 没有估算读数时不生成调整单（仅幂等登记真实读数）。
        """
        meter = self.store.meters.get(real.meter_key)
        if meter is None:
            raise LedgerError(f"表计 {real.meter_key} 不存在")
        if real.estimated:
            raise LedgerError("post_late_reading 只接受真实读数")

        estimated = next(
            (r for r in self.store.readings_for(meter.key)
             if r.read_on == real.read_on and r.estimated),
            None,
        )
        # 基准口径：显式只看估算（真实读数即使已登记也排除），保证确定性
        before = _meter_consumption(self.store, meter, window_start,
                                    window_end, {real.key})
        added = self.store.add_reading(real)  # 幂等
        # 实抄口径：用真实读数取代被取代的估算
        after = _meter_consumption(
            self.store, meter, window_start, window_end,
            {estimated.key} if estimated else set())

        if not added or estimated is None or before is None or after is None:
            return None  # 重复导入、无估算被取代或缺数，均不产生调整单

        delta = round(after[0] - before[0], 6)
        component = (C.BULK_INPUT if meter.meter_type == "bulk"
                     else C.CUSTOMER_CONSUMPTION)
        adj = Adjustment(
            key=f"ADJ-{real.key}",
            zone_id=meter.zone_id,
            window_start=window_start,
            component=component,
            delta_m3=delta,
            reason=reason or f"真实读数 {real.key} 到达，取代估算 {estimated.key}",
            created_on=created_on or real.read_on,
            created_by=actor,
            linked_keys=(real.key,),
            supersedes=estimated.key,
        )
        self.store.add_adjustment(adj)
        return adj

    def add_manual_adjustment(self, adj: Adjustment) -> Adjustment:
        if adj.component not in C.ADJUSTABLE:
            raise LedgerError(f"调整单分量必须属于 {C.ADJUSTABLE}")
        self.store.add_adjustment(adj)
        return adj

    # ------------------------------------------------------------------
    # 影响追踪
    # ------------------------------------------------------------------
    def trace_impacts(self, zone_id: str, window_start: date) -> dict:
        """逐修订比较分量/漏损率变化，并列出每张调整单的漏损影响。"""
        series = self.list_versions(zone_id, window_start)
        revisions = []
        for v in series:
            revisions.append({
                "version_id": v.version_id,
                "revision": v.revision,
                "status": v.status.value,
                "fingerprint": v.snapshot.fingerprint,
                "losses_m3": v.snapshot.losses_m3,
                "loss_rate": v.snapshot.loss_rate,
                "components": dict(v.snapshot.components),
                "warnings": list(v.snapshot.warnings),
            })
        diffs = []
        for prev, cur in zip(series, series[1:]):
            comp_delta = {}
            for key in sorted(set(prev.snapshot.components) | set(cur.snapshot.components)):
                d = round(cur.snapshot.components.get(key, 0.0)
                          - prev.snapshot.components.get(key, 0.0), 6)
                if d:
                    comp_delta[key] = d
            new_adjustments = [
                c for c in cur.snapshot.contributions if c.source == "adjustment"
                and c.key not in {x.key for x in prev.snapshot.contributions
                                  if x.source == "adjustment"}
            ]
            diffs.append({
                "from_revision": prev.revision,
                "to_revision": cur.revision,
                "component_delta": comp_delta,
                "losses_delta_m3": round(cur.snapshot.losses_m3
                                         - prev.snapshot.losses_m3, 6),
                "loss_rate_delta": round(cur.snapshot.loss_rate
                                         - prev.snapshot.loss_rate, 6),
                "reopen_reason": cur.reopen_reason,
                "new_adjustments": [
                    {
                        "key": c.key,
                        "component": c.component,
                        "delta_m3": c.volume_m3,
                        "loss_impact_m3": round(
                            C.loss_sign(c.component) * c.volume_m3, 6),
                        "detail": c.detail,
                    }
                    for c in new_adjustments
                ],
            })
        # 每张调整单对漏损指标的方向性影响（含仅在草稿里出现的）
        adjustment_effects = []
        if series:
            latest = series[-1]
            for c in latest.snapshot.contributions:
                if c.source != "adjustment":
                    continue
                adjustment_effects.append({
                    "key": c.key,
                    "component": c.component,
                    "delta_m3": c.volume_m3,
                    "loss_impact_m3": round(C.loss_sign(c.component) * c.volume_m3, 6),
                    "detail": c.detail,
                })
        return {"revisions": revisions, "diffs": diffs,
                "adjustment_effects": adjustment_effects}

    @staticmethod
    def _vid(zone_id: str, start: date, revision: int) -> str:
        return f"{zone_id}@{start.isoformat()}#r{revision}"

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def save_versions(self, path: str | Path) -> None:
        rows = [v.to_dict()
                for series in self.versions.values() for v in series]
        Path(path).write_text(
            json.dumps({"versions": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def load_versions(self, path: str | Path) -> None:
        from .snapshots import version_from_dict
        if not Path(path).exists():
            return
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.versions.clear()
        for row in data.get("versions", []):
            v = version_from_dict(row)
            self.versions.setdefault(
                (v.zone_id, date.fromisoformat(v.window_start)), []).append(v)
        for series in self.versions.values():
            series.sort(key=lambda v: v.revision)
