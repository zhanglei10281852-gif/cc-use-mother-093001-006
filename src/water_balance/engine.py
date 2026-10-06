"""水量平衡计算引擎。

对 (分区, 结算窗口) 计算各分量：

* 总表/用户表用量 = 读数区间在窗口上的按天线性分摊；换表归零由
  ``MeterReplacement`` 的旧表末次/新表起算锚点自然衔接；
* 调水按同一笔业务事实分别进入调出/调入两侧；
* 消防与合法未计量按事件汇总；
* 缺数按经审批规则估算，并在摘要中标记 estimated；
* 调整单按分量追加 delta，不改写任何历史读数。

输出 :class:`BalanceSnapshot`，内含逐项输入摘要与内容指纹。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Optional

from .contracts import Component as C
from .models import Adjustment, EstimateRule
from .snapshots import BalanceSnapshot, Contribution
from .store import Store

_ROUND = 6
IDENTITY_TOLERANCE = 1e-6


@dataclass(frozen=True)
class _Anchor:
    on: date
    value: float
    estimated: bool
    key: str
    kind: str  # reading | replacement


def _anchors_for(store: Store, meter_key: str,
                 excluded: Optional[set[str]] = None) -> list[_Anchor]:
    excluded = excluded or set()
    anchors = [
        _Anchor(r.read_on, r.value, r.estimated, r.key, "reading")
        for r in store.readings_for(meter_key)
        if r.key not in excluded
    ]
    for rep in store.replacements_for_meter(meter_key):
        if rep.key in excluded:
            continue
        if rep.old_meter_key == meter_key:
            anchors.append(_Anchor(rep.replaced_on, rep.old_final_reading,
                                   False, rep.key, "replacement"))
        if rep.new_meter_key == meter_key:
            anchors.append(_Anchor(rep.replaced_on, rep.new_initial_reading,
                                   False, rep.key, "replacement"))
    # 同一日可能同时有读数与换表锚点：去重，读数优先于换表锚点
    by_day: dict[date, _Anchor] = {}
    for a in sorted(anchors, key=lambda x: (x.on, 0 if x.kind == "reading" else 1)):
        by_day.setdefault(a.on, a)
    return [by_day[d] for d in sorted(by_day)]


def _meter_consumption(store: Store, meter, start: date, end: date,
                       superseded: set[str]) -> Optional[tuple[float, bool, list[Contribution]]]:
    """返回 (用量, 是否含估算, 贡献明细)；真缺数时返回 None。

    各读数区间与窗口（再按表计有效期收窄）取交集，按天线性分摊。
    窗口结束前没有任何末次锚点（末读数早于应覆盖终点）才算缺数，
    交由审批估算规则；窗口内的读数迟到只产生按天裁剪，不臆造区间。
    """
    anchors = _anchors_for(store, meter.key, superseded)
    lo_bound = max(start, meter.valid_from)
    hi_bound = min(end, meter.valid_to) if meter.valid_to is not None else end
    if hi_bound <= lo_bound:
        return 0.0, False, []
    closing = hi_bound - timedelta(days=1)  # 窗口末日收盘读数视同结算点
    if not anchors or anchors[-1].on < closing:
        return None  # 真缺数：窗口结束前连末日收盘读数都没有
    total = 0.0
    estimated = False
    details: list[Contribution] = []
    if anchors[0].on > lo_bound:
        gap = (anchors[0].on - lo_bound).days
        details.append(Contribution(
            "", "reading", meter.key, 0.0, True,
            f"窗口前段 {lo_bound}->{anchors[0].on} 共 {gap} 天无读数覆盖"))
    for a0, a1 in zip(anchors, anchors[1:]):
        if a1.on <= lo_bound:
            continue
        if a0.on >= hi_bound:
            break
        seg_lo = max(a0.on, lo_bound)
        seg_hi = min(a1.on, hi_bound)
        span = (a1.on - a0.on).days
        delta = a1.value - a0.value
        if delta < 0:
            details.append(Contribution(
                C.LOSSES, "reading", meter.key, 0.0, False,
                f"读数区间 {a0.on}->{a1.on} 表底倒退，已跳过"))
            continue
        # 末日收盘读数视同窗口结算点：覆盖最后一天，但不超过区间实际天数
        is_closing = a1.on == closing
        if is_closing and seg_hi == closing:
            seg_hi = hi_bound
        overlap = (seg_hi - seg_lo).days
        if is_closing:
            overlap = min(overlap, span)
        vol = round(delta * overlap / span, _ROUND)
        seg_est = a0.estimated or a1.estimated
        estimated = estimated or seg_est
        total += vol
        src = "reading" if a0.kind == "reading" and a1.kind == "reading" else "replacement"
        details.append(Contribution(
            "", src, meter.key, vol, seg_est,
            f"{a0.on}({a0.value:g})->{a1.on}({a1.value:g}) 分摊 {overlap}/{span} 天"))
    return round(total, _ROUND), estimated, details


def _estimate(rule: EstimateRule, days: int, history_volume: Optional[float]) -> float:
    if rule.method == "flat":
        return round(rule.parameter, _ROUND)
    if rule.method == "daily_average":
        return round(rule.parameter * days, _ROUND)
    # history：参数为上一可比结算期用量
    ref = rule.parameter if rule.parameter > 0 else (history_volume or 0.0)
    return round(ref, _ROUND)


HistoryProvider = Callable[[str], Optional[float]]


def compute_balance(store: Store, zone_id: str, start: date, end: date, *,
                    history: Optional[HistoryProvider] = None) -> BalanceSnapshot:
    warnings: list[str] = []
    contributions: list[Contribution] = []
    totals = {comp: 0.0 for comp in C.ADJUSTABLE}
    estimated_total = 0.0
    adjustment_total = 0.0

    # 追加式账本：被调整单关联的迟到真实读数不参与锚点计算；估算读数
    # 保留为基准，真实与估算的差异由调整单 delta 承载，旧账因此不变。
    window_adjustments = [
        a for a in store.adjustments.values()
        if a.zone_id == zone_id and a.window_start == start
    ]
    superseded = {
        key for a in window_adjustments for key in a.linked_keys
        if key in store.readings
    }

    def run_meters(meter_type: str, component: str, scope: str) -> None:
        nonlocal estimated_total
        for meter in store.meters_active(zone_id, start, end, meter_type):
            result = _meter_consumption(store, meter, start, end, superseded)
            if result is None:
                rules = store.rules_for(zone_id, scope, start)
                if not rules:
                    warnings.append(f"表计 {meter.key} 在窗口 [{start},{end}) 缺数且无审批估算规则")
                    continue
                rule = rules[0]
                vol = _estimate(rule, (end - start).days,
                                history(meter.key) if history else None)
                warnings.append(f"表计 {meter.key} 缺数，按规则 {rule.key}({rule.method}) 估算 {vol:g} m³")
                totals[component] += vol
                estimated_total += vol
                contributions.append(Contribution(
                    component, "estimate", meter.key, vol, True,
                    f"规则 {rule.key} / {rule.method} / 审批人 {rule.approved_by}",
                    rule_key=rule.key))
                continue
            vol, estimated, details = result
            totals[component] += vol
            if estimated:
                estimated_total += vol
            for d in details:
                contributions.append(Contribution(component, d.source, d.key, d.volume_m3,
                                                  d.estimated, d.detail))

    run_meters("bulk", C.BULK_INPUT, "bulk")
    run_meters("customer", C.CUSTOMER_CONSUMPTION, "customer")

    # 调水：成对入账
    for t in store.transfers_in_window(zone_id, start, end):
        if t.to_zone == zone_id:
            totals[C.TRANSFER_IN] += t.volume_m3
            contributions.append(Contribution(
                C.TRANSFER_IN, "transfer", t.key, t.volume_m3, False,
                f"自 {t.from_zone} 调入 @ {t.occurred_on}"))
        if t.from_zone == zone_id:
            totals[C.TRANSFER_OUT] += t.volume_m3
            contributions.append(Contribution(
                C.TRANSFER_OUT, "transfer", t.key, t.volume_m3, False,
                f"调出至 {t.to_zone} @ {t.occurred_on}"))

    # 消防 / 合法未计量事件
    for ev in store.events_in_window(zone_id, start, end):
        comp = C.FIRE_USE if ev.kind == "fire" else C.AUTHORIZED_UNMETERED
        totals[comp] += ev.volume_m3
        contributions.append(Contribution(
            comp, "event", ev.key, ev.volume_m3, False,
            f"{ev.kind} @ {ev.occurred_on}"))

    # 调整单：追加 delta，永不改账
    for adj in store.adjustments_for(zone_id, start):
        if adj.component not in totals:
            warnings.append(f"调整单 {adj.key} 指向未知分量 {adj.component}，已忽略")
            continue
        totals[adj.component] = round(totals[adj.component] + adj.delta_m3, _ROUND)
        adjustment_total += abs(adj.delta_m3)
        contributions.append(Contribution(
            adj.component, "adjustment", adj.key, adj.delta_m3, False,
            f"{adj.reason}（{adj.created_by} @ {adj.created_on}）",
            linked_keys=adj.linked_keys))

    totals = {k: round(v, _ROUND) for k, v in totals.items()}
    net_input = round(totals[C.BULK_INPUT] + totals[C.TRANSFER_IN]
                      - totals[C.TRANSFER_OUT], _ROUND)
    authorized = round(sum(totals[c] for c in C.AUTHORIZED), _ROUND)
    losses = round(net_input - authorized, _ROUND)
    if net_input > 0:
        loss_rate = round(losses / net_input, _ROUND)
    else:
        loss_rate = 0.0
        if net_input <= 0:
            warnings.append("净输入为零或为负，漏损率无法定义，按 0 列示")

    components = dict(totals)
    components[C.LOSSES] = losses
    fingerprint = _fingerprint(zone_id, start, end, components, contributions)

    return BalanceSnapshot(
        components=components,
        net_input=net_input,
        authorized_consumption=authorized,
        losses_m3=losses,
        loss_rate=loss_rate,
        warnings=tuple(warnings),
        contributions=tuple(contributions),
        fingerprint=fingerprint,
        estimated_volume_m3=round(estimated_total, _ROUND),
        adjustment_volume_m3=round(adjustment_total, _ROUND),
    )


def _fingerprint(zone_id: str, start: date, end: date, components: dict[str, float],
                 contributions: list[Contribution]) -> str:
    payload = {
        "zone": zone_id,
        "window": [start.isoformat(), end.isoformat()],
        "components": components,
        "contributions": [
            [c.component, c.source, c.key, round(c.volume_m3, _ROUND), c.estimated]
            for c in contributions
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def verify_transfer_pairs(store: Store, start: date, end: date,
                          tolerance: float = IDENTITY_TOLERANCE) -> list[str]:
    """全局核对：每笔调水两侧分区在册，且各分区账上 Σ调入 = Σ调出。"""
    errors: list[str] = []
    known = {m.zone_id for m in store.meters.values()}
    raw_in = raw_out = 0.0
    for t in store.all_transfers_in_window(start, end):
        raw_in += t.volume_m3
        raw_out += t.volume_m3
        if known and (t.from_zone not in known or t.to_zone not in known):
            errors.append(f"调水 {t.key} 的分区不在册：{t.from_zone}->{t.to_zone}")
    # 以各分区实际入账分量复核（确保两侧分录都真正进入平衡）
    zones = {z for t in store.all_transfers_in_window(start, end)
             for z in (t.from_zone, t.to_zone)}
    booked_in = booked_out = 0.0
    for zone in zones:
        snap = compute_balance(store, zone, start, end)
        booked_in += snap.components[C.TRANSFER_IN]
        booked_out += snap.components[C.TRANSFER_OUT]
    if abs(booked_in - raw_in) > tolerance or abs(booked_out - raw_out) > tolerance:
        errors.append(
            f"调水未成对入账：业务事实 调入/调出 {raw_in:g}/{raw_out:g}，"
            f"各分区账上 {booked_in:g}/{booked_out:g}")
    if abs(booked_in - booked_out) > tolerance:
        errors.append(f"调水总量不平衡：Σ调入 {booked_in:g} != Σ调出 {booked_out:g}")
    return errors


def verify_identity(snapshot: BalanceSnapshot,
                    tolerance: float = IDENTITY_TOLERANCE) -> list[str]:
    """核对总量恒等式：净输入 = 合法用水 + 漏损。"""
    rhs = round(snapshot.authorized_consumption + snapshot.losses_m3, _ROUND)
    if abs(snapshot.net_input - rhs) > tolerance:
        return [f"总量恒等式不成立：净输入 {snapshot.net_input:g} != "
                f"合法用水 {snapshot.authorized_consumption:g} + 漏损 {snapshot.losses_m3:g}"]
    return []
