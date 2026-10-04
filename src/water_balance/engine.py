"""分区水量平衡核算引擎（纯函数）。

恒等式::

    总表供入 + 跨分区调入 = 用户实计 + 规则估算 + 消防
                          + 合法未计量 + 跨分区调出 + 漏损(NRW)

读数语义：读数 r 在 ``read_on`` 当日抄录，表征自上一读数以来的用量，
覆盖半开区间 ``[prev_read_on, read_on)``；跨窗口/表计有效期边界的用量
按天均摊。换表归零（新值小于旧值）按新表起度 0 处理。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Optional, Sequence

from .contracts import (
    BalanceResult,
    EstimateRecord,
    ItemCode,
    LineItem,
    MissingDataError,
    NotFound,
)


# ---------------------------------------------------------------- 工具

def month_bounds(month: str) -> tuple[date, date]:
    try:
        start = datetime.strptime(month, "%Y-%m").date()
    except ValueError as exc:
        raise ValueError(f"月份格式应为 YYYY-MM: {month!r}") from exc
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start, end


def _d(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def _clip_segments(readings: Sequence[dict[str, Any]],
                   meter: dict[str, Any],
                   lo: date, hi: date) -> list[tuple[date, date, float, bool, str]]:
    """把读数展开成 (起, 止, 水量, 是否归零, reading_id) 片段并裁剪到
    表计有效期与窗口交集，跨界按天均摊。"""
    m_from = _d(meter["valid_from"])
    m_to = _d(meter["valid_to"])
    segs: list[tuple[date, date, float, bool, str]] = []
    prev: Optional[dict[str, Any]] = None
    ordered = sorted(readings, key=lambda x: x["read_on"])
    # 新表首读：以安装日起度 0 作为隐式前驱（仅窗口内新装/换表的情形）。
    # 窗口开始前已安装却没有窗口前读数，属于缺数，不得把累计量摊入窗口。
    if ordered and m_from >= lo and _d(ordered[0]["read_on"]) > m_from:
        prev = {"reading_id": f"@{meter['meter_id']}#install",
                "read_on": m_from.isoformat(), "value": 0.0}
    for r in ordered:
        if prev is not None:
            p_date = _d(prev["read_on"])
            r_date = _d(r["read_on"])
            seg_lo = max(p_date, m_from, lo)
            seg_hi = min(r_date, m_to or date.max, hi)
            if seg_hi > seg_lo:
                total_days = max((r_date - p_date).days, 1)
                reset = r["value"] < prev["value"]
                delta = float(r["value"]) if reset else float(r["value"]) - float(prev["value"])
                part = (seg_hi - seg_lo).days / total_days
                segs.append((seg_lo, seg_hi, delta * part, reset, r["reading_id"]))
        prev = r
    return segs


def _union_days(segments: Sequence[tuple[date, date, Any]], lo: date, hi: date) -> int:
    days = [False] * (hi - lo).days
    for s, e, *_ in segments:
        a = max(s, lo)
        b = min(e, hi)
        for i in range((a - lo).days, (b - lo).days):
            days[i] = True
    return sum(days)


# ---------------------------------------------------------------- 主核算

def compute_balance(db: dict[str, Any], zone_id: str, month: str) -> BalanceResult:
    if zone_id not in db["zones"]:
        raise NotFound(f"分区不存在: {zone_id}")
    lo, hi = month_bounds(month)

    sps = [sp for sp in db["service_points"].values() if sp["zone_id"] == zone_id]
    meters = list(db["meters"].values())
    meters_by_sp: dict[str, list[dict[str, Any]]] = {}
    for m in meters:
        meters_by_sp.setdefault(m["sp_id"], []).append(m)

    readings_by_meter: dict[str, list[dict[str, Any]]] = {}
    for r in db["readings"].values():
        readings_by_meter.setdefault(r["meter_id"], []).append(r)

    # 每类行项目的累计值与溯源
    vol: dict[str, float] = {c.value: 0.0 for c in ItemCode}
    refs: dict[str, list[str]] = {c.value: [] for c in ItemCode}
    estimates: list[EstimateRecord] = []
    missing: list[dict[str, Any]] = []
    reset_notes: list[dict[str, Any]] = []
    used_readings: set[str] = set()
    actual_by_sp: dict[str, float] = {}

    def add(code: ItemCode, amount: float, ref: Optional[str] = None) -> None:
        vol[code.value] += amount
        if ref and ref not in refs[code.value]:
            refs[code.value].append(ref)

    rule = _active_rule(db, lo, hi)

    for sp in sps:
        sp_lo = max(_d(sp["valid_from"]), lo)
        sp_hi = min(_d(sp["valid_to"]) or date.max, hi)
        if sp_hi <= sp_lo:
            continue  # 该供水点在窗口内无效
        target_days = (sp_hi - sp_lo).days

        chain = sorted(meters_by_sp.get(sp["sp_id"], []),
                       key=lambda m: m["valid_from"])
        sp_segments: list[tuple[date, date, float, bool, str]] = []
        for meter in chain:
            if _d(meter["valid_from"]) >= sp_hi:
                break
            if _d(meter["valid_to"]) and _d(meter["valid_to"]) <= sp_lo:
                continue
            segs = _clip_segments(readings_by_meter.get(meter["meter_id"], []),
                                  meter, sp_lo, sp_hi)
            sp_segments.extend(segs)

        actual_vol = round(sum(s[2] for s in sp_segments), 6)
        actual_by_sp[sp["sp_id"]] = actual_vol
        covered_days = _union_days(sp_segments, sp_lo, sp_hi)
        uncovered = target_days - covered_days
        for rid in {s[4] for s in sp_segments}:
            used_readings.add(rid)
        for s in sp_segments:
            if s[3]:
                reset_notes.append({"service_point_id": sp["sp_id"],
                                    "reading_id": s[4]})

        role = chain[0]["role"] if chain else None
        if uncovered > 0:
            if role == "master":
                missing.append({"service_point_id": sp["sp_id"],
                                "uncovered_days": uncovered,
                                "reason": "总表缺数不可估算"})
            else:
                est = _estimate(sp, [m["meter_id"] for m in chain], rule,
                                actual_vol, covered_days, uncovered, missing)
                if est is not None:
                    estimates.append(est)
                    add(ItemCode.ESTIMATED_METERED, est.volume_m3, est.rule_id)
        if actual_vol:
            code = (ItemCode.MASTER_SUPPLY if role == "master"
                    else ItemCode.BILLED_METERED)
            add(code, actual_vol)
            for rid in sorted({s[4] for s in sp_segments}):
                add(code, 0.0, rid)

    # 跨分区调水（成对腿，事件日落入 [lo, hi) 的月份）
    for t in db["transfers"].values():
        d = _d(t["transfer_date"])
        if lo <= d < hi:
            if t["to_zone"] == zone_id:
                add(ItemCode.TRANSFER_IN, float(t["volume_m3"]),
                    t["transfer_id"])
            if t["from_zone"] == zone_id:
                add(ItemCode.TRANSFER_OUT, float(t["volume_m3"]),
                    t["transfer_id"])

    # 消防 / 合法未计量
    for u in db["usages"].values():
        if u["zone_id"] != zone_id:
            continue
        days = _overlap_days(_d(u["period_start"]), _d(u["period_end"]), lo, hi)
        if days:
            amount = round(float(u["daily_m3"]) * days, 6)
            code = (ItemCode.FIRE_WATER if u["category"] == "fire_water"
                    else ItemCode.AUTHORIZED_UNMETERED)
            add(code, amount, u["usage_id"])

    if missing:
        raise MissingDataError(
            f"分区 {zone_id} {month} 存在无法核算的缺数", missing)

    nrw = round(
        vol[ItemCode.MASTER_SUPPLY.value] + vol[ItemCode.TRANSFER_IN.value]
        - vol[ItemCode.TRANSFER_OUT.value] - vol[ItemCode.BILLED_METERED.value]
        - vol[ItemCode.ESTIMATED_METERED.value] - vol[ItemCode.FIRE_WATER.value]
        - vol[ItemCode.AUTHORIZED_UNMETERED.value], 6)
    vol[ItemCode.NRW.value] = nrw

    system_input = vol[ItemCode.MASTER_SUPPLY.value] + vol[ItemCode.TRANSFER_IN.value]
    rate = round(nrw / system_input * 100.0, 6) if system_input > 0 else 0.0
    indicators = {
        "system_input_m3": round(system_input, 6),
        "authorized_consumption_m3": round(system_input - nrw, 6),
        "nrw_m3": nrw,
        "nrw_rate_pct": rate,
    }
    totals = {k: round(v, 6) for k, v in vol.items()}

    items = tuple(
        LineItem(code=c.value, label=_LABELS[c.value],
                 volume_m3=totals[c.value],
                 estimated=(c is ItemCode.ESTIMATED_METERED),
                 ref=",".join(refs[c.value][:20]),
                 detail={"reset_readings": reset_notes} if c is ItemCode.MASTER_SUPPLY and reset_notes else {})
        for c in ItemCode)

    digest = _digest(zone_id, lo, hi, used_readings, db, estimates, rule,
                     sps, meters)

    return BalanceResult(
        zone_id=zone_id, month=month, starts_on=lo, ends_on=hi,
        items=items, estimates=tuple(estimates), digest=digest,
        totals=totals, indicators=indicators,
        source_refs={k: tuple(v) for k, v in refs.items() if v},
        sp_actual=actual_by_sp)


_LABELS = {
    "master_supply": "总表供入",
    "transfer_in": "跨分区调入",
    "transfer_out": "跨分区调出",
    "billed_metered": "用户表实计",
    "estimated_metered": "规则估算水量",
    "fire_water": "消防用水",
    "authorized_unmetered": "合法未计量用水",
    "nrw": "漏损水量(NRW)",
}


def _active_rule(db: dict[str, Any], lo: date, hi: date
                 ) -> Optional[dict[str, Any]]:
    candidates = []
    for r in db["rules"].values():
        if not r.get("active") or not r.get("approved_by"):
            continue
        if _d(r["valid_from"]) <= lo and (
                not _d(r["valid_to"]) or _d(r["valid_to"]) >= hi):
            candidates.append(r)
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.get("approved_at") or "")


def _estimate(sp: dict[str, Any], meter_ids: list[str],
              rule: Optional[dict[str, Any]],
              actual_vol: float, covered_days: int, uncovered: int,
              missing: list[dict[str, Any]]) -> Optional[EstimateRecord]:
    if rule is None:
        missing.append({"service_point_id": sp["sp_id"],
                        "uncovered_days": uncovered,
                        "reason": "缺数且无已审批估算规则"})
        return None
    min_cov = float(rule.get("min_coverage", 0.0))
    window_days = covered_days + uncovered
    coverage = covered_days / window_days if window_days else 0.0
    if coverage < min_cov or covered_days == 0:
        missing.append({"service_point_id": sp["sp_id"],
                        "uncovered_days": uncovered,
                        "reason": f"实际覆盖率 {coverage:.0%} 低于规则下限 "
                                  f"{min_cov:.0%}，估算依据不足"})
        return None
    daily = actual_vol / covered_days
    volume = round(daily * float(rule["factor"]) * uncovered, 6)
    return EstimateRecord(
        service_point_id=sp["sp_id"],
        meter_ids=tuple(meter_ids),
        uncovered_days=uncovered,
        actual_alongside_m3=round(actual_vol, 6),
        volume_m3=volume,
        rule_id=rule["rule_id"],
        basis={"method": rule["method"], "factor": rule["factor"],
               "daily_actual_m3": round(daily, 6),
               "covered_days": covered_days})


def _overlap_days(a: date, b: date, lo: date, hi: date) -> int:
    return max((min(b, hi) - max(a, lo)).days, 0)


def _digest(zone_id: str, lo: date, hi: date,
            used_readings: set[str], db: dict[str, Any],
            estimates: list[EstimateRecord],
            rule: Optional[dict[str, Any]],
            sps: list[dict[str, Any]],
            meters: list[dict[str, Any]]) -> str:
    sp_ids = {sp["sp_id"] for sp in sps if sp["zone_id"] == zone_id}
    payload = {
        "zone": zone_id,
        "window": [lo.isoformat(), hi.isoformat()],
        "readings": sorted(
            [r["reading_id"], r["meter_id"], r["read_on"], float(r["value"])]
            for r in db["readings"].values()
            if r["reading_id"] in used_readings),
        "service_points": sorted(
            [sp["sp_id"], sp["valid_from"], sp["valid_to"]]
            for sp in sps if sp["zone_id"] == zone_id),
        "meters": sorted(
            [m["meter_id"], m["sp_id"], m["role"], m["valid_from"], m["valid_to"]]
            for m in meters if m["sp_id"] in sp_ids),
        "usages": sorted(
            [u["usage_id"], u["category"], u["period_start"],
             u["period_end"], float(u["daily_m3"])]
            for u in db["usages"].values() if u["zone_id"] == zone_id),
        "transfers": sorted(
            [t["transfer_id"], t["transfer_date"], t["from_zone"],
             t["to_zone"], float(t["volume_m3"])]
            for t in db["transfers"].values()
            if zone_id in (t["from_zone"], t["to_zone"])
            and lo <= _d(t["transfer_date"]) < hi),
        "rule": ([rule["rule_id"], rule["method"], float(rule["factor"]),
                  float(rule.get("min_coverage", 0.0))] if rule else None),
        "estimates": sorted(
            [e.service_point_id, e.rule_id, e.uncovered_days, e.volume_m3]
            for e in estimates),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 恒等式核对

def identity_residual(result: BalanceResult) -> float:
    """返回恒等式残差（应为 0，浮点容差内）。"""
    t = result.totals
    return round(
        t["master_supply"] + t["transfer_in"] - t["transfer_out"]
        - t["billed_metered"] - t["estimated_metered"]
        - t["fire_water"] - t["authorized_unmetered"] - t["nrw"], 6)


def verify_global_transfers(db: dict[str, Any], month: str) -> dict[str, Any]:
    """全局守恒：当月所有调入腿之和必须等于调出腿之和。"""
    lo, hi = month_bounds(month)
    tin = tout = 0.0
    legs: dict[str, float] = {}
    for t in db["transfers"].values():
        d = _d(t["transfer_date"])
        if lo <= d < hi:
            tin += float(t["volume_m3"])
            tout += float(t["volume_m3"])
            legs[t["transfer_id"]] = legs.get(t["transfer_id"], 0.0) \
                + 2 * float(t["volume_m3"])
    # 调水创建即成对，这里再次核对单内双腿
    unpaired = [tid for tid, v in legs.items() if v <= 0]
    return {"transfer_in_total": round(tin, 6),
            "transfer_out_total": round(tout, 6),
            "residual": round(tin - tout, 6),
            "unpaired": unpaired}
