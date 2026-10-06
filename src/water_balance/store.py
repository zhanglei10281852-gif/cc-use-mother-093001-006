"""内存仓储 + JSON 持久化。

所有导入按业务自然键 ``key`` 幂等：重复导入同键且内容一致时忽略；
同键但内容冲突时抛出 :class:`IdempotencyConflict`，绝不静默覆盖。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

from .models import (
    Adjustment,
    EstimateRule,
    Meter,
    MeterReplacement,
    Reading,
    Transfer,
    WaterEvent,
)


class IdempotencyConflict(Exception):
    """同键记录重复导入但字段不一致。"""


_DATE_FIELDS = ("valid_from", "valid_to", "read_on", "replaced_on",
                "occurred_on", "created_on", "window_start")

_MODELS = {
    "meters": Meter,
    "readings": Reading,
    "replacements": MeterReplacement,
    "transfers": Transfer,
    "events": WaterEvent,
    "rules": EstimateRule,
    "adjustments": Adjustment,
}


def _coerce_dates(row: dict) -> dict:
    for k in list(row):
        if k in _DATE_FIELDS and isinstance(row[k], str):
            row[k] = date.fromisoformat(row[k])
    if "linked_keys" in row:
        row["linked_keys"] = tuple(row["linked_keys"])
    if "applied_revisions" in row:
        row["applied_revisions"] = tuple(row["applied_revisions"])
    return row


def ingest_dict(store: "Store", payload: dict) -> dict[str, int]:
    """把 ``{"meters": [...], ...}`` 形式的数据包幂等导入仓储。

    返回各类新增条数（重复且一致的导入不计入）。
    """
    counts: dict[str, int] = {}
    for section, model in _MODELS.items():
        adder = getattr(store, {
            "meters": "add_meter",
            "readings": "add_reading",
            "replacements": "add_replacement",
            "transfers": "add_transfer",
            "events": "add_event",
            "rules": "add_rule",
            "adjustments": "add_adjustment",
        }[section])
        n = 0
        for raw in payload.get(section, []):
            row = _coerce_dates(dict(raw))
            n += int(bool(adder(model(**row))))
        counts[section] = n
    return counts


def _dates_active(valid_from: Optional[date], valid_to: Optional[date],
                  start: date, end: date) -> bool:
    """记录有效区间与窗口 [start, end) 是否相交。"""
    if valid_from is not None and valid_from >= end:
        return False
    if valid_to is not None and valid_to <= start:
        return False
    return True


def _active_on(valid_from: Optional[date], valid_to: Optional[date],
               on_day: date) -> bool:
    """某日是否落在有效期内（valid_to 为开区间端点）。"""
    if valid_from is not None and valid_from > on_day:
        return False
    if valid_to is not None and valid_to <= on_day:
        return False
    return True


class Store:
    def __init__(self) -> None:
        self.meters: dict[str, Meter] = {}
        self.readings: dict[str, Reading] = {}
        self.replacements: dict[str, MeterReplacement] = {}
        self.transfers: dict[str, Transfer] = {}
        self.events: dict[str, WaterEvent] = {}
        self.rules: dict[str, EstimateRule] = {}
        self.adjustments: dict[str, Adjustment] = {}

    # ---- 通用幂等写入 -------------------------------------------------
    @staticmethod
    def _put(table: dict, key: str, value, comparable: tuple[str, ...]) -> bool:
        existing = table.get(key)
        if existing is not None:
            for attr in comparable:
                if getattr(existing, attr) != getattr(value, attr):
                    raise IdempotencyConflict(
                        f"键 {key} 已存在但字段 {attr} 不一致："
                        f"{getattr(existing, attr)!r} != {getattr(value, attr)!r}"
                    )
            return False  # 完全相同的重复导入，忽略
        table[key] = value
        return True

    def add_meter(self, meter: Meter) -> bool:
        return self._put(self.meters, meter.key, meter,
                         ("meter_type", "zone_id", "valid_from", "valid_to", "multiplier"))

    def add_reading(self, reading: Reading) -> bool:
        return self._put(self.readings, reading.key, reading,
                         ("meter_key", "read_on", "value", "estimated"))

    def add_replacement(self, item: MeterReplacement) -> bool:
        return self._put(self.replacements, item.key, item,
                         ("old_meter_key", "new_meter_key", "replaced_on",
                          "old_final_reading", "new_initial_reading"))

    def add_transfer(self, item: Transfer) -> bool:
        return self._put(self.transfers, item.key, item,
                         ("from_zone", "to_zone", "occurred_on", "volume_m3",
                          "valid_from", "valid_to"))

    def add_event(self, item: WaterEvent) -> bool:
        return self._put(self.events, item.key, item,
                         ("zone_id", "kind", "occurred_on", "volume_m3",
                          "valid_from", "valid_to"))

    def add_rule(self, rule: EstimateRule) -> bool:
        return self._put(self.rules, rule.key, rule,
                         ("zone_id", "scope", "priority", "valid_from", "valid_to",
                          "method", "parameter", "approved_by"))

    def add_adjustment(self, adj: Adjustment) -> bool:
        # 调整单同样按 key 幂等
        return self._put(self.adjustments, adj.key, adj,
                         ("zone_id", "window_start", "component", "delta_m3",
                          "reason", "created_on", "created_by", "supersedes"))

    # ---- 查询 ----------------------------------------------------------
    def meters_active(self, zone_id: str, start: date, end: date,
                      meter_type: Optional[str] = None) -> list[Meter]:
        out = []
        for m in self.meters.values():
            if m.zone_id != zone_id:
                continue
            if meter_type is not None and m.meter_type != meter_type:
                continue
            if _dates_active(m.valid_from, m.valid_to, start, end):
                out.append(m)
        return sorted(out, key=lambda m: (m.valid_from, m.key))

    def readings_for(self, meter_key: str) -> list[Reading]:
        return sorted(
            (r for r in self.readings.values() if r.meter_key == meter_key),
            key=lambda r: (r.read_on, r.key),
        )

    def replacements_for_meter(self, meter_key: str) -> list[MeterReplacement]:
        out = []
        for rep in self.replacements.values():
            if meter_key in (rep.old_meter_key, rep.new_meter_key):
                out.append(rep)
        return sorted(out, key=lambda r: (r.replaced_on, r.key))

    def transfers_in_window(self, zone_id: str, start: date, end: date) -> list[Transfer]:
        out = []
        for t in self.transfers.values():
            if zone_id not in (t.from_zone, t.to_zone):
                continue
            if start <= t.occurred_on < end and _active_on(
                    t.valid_from, t.valid_to, t.occurred_on):
                out.append(t)
        return sorted(out, key=lambda t: (t.occurred_on, t.key))

    def all_transfers_in_window(self, start: date, end: date) -> list[Transfer]:
        return sorted(
            (t for t in self.transfers.values() if start <= t.occurred_on < end),
            key=lambda t: (t.occurred_on, t.key),
        )

    def events_in_window(self, zone_id: str, start: date, end: date) -> list[WaterEvent]:
        out = []
        for e in self.events.values():
            if e.zone_id != zone_id:
                continue
            if start <= e.occurred_on < end and _active_on(
                    e.valid_from, e.valid_to, e.occurred_on):
                out.append(e)
        return sorted(out, key=lambda e: (e.occurred_on, e.key))

    def rules_for(self, zone_id: str, scope: str, on_date: date) -> list[EstimateRule]:
        out = []
        for rule in self.rules.values():
            if rule.scope != scope:
                continue
            if rule.zone_id not in (zone_id, "*"):
                continue
            if not _active_on(rule.valid_from, rule.valid_to, on_date):
                continue
            out.append(rule)
        out.sort(key=lambda r: (0 if r.zone_id == zone_id else 1, r.priority, r.key))
        return out

    def adjustments_for(self, zone_id: str, window_start: date) -> list[Adjustment]:
        return sorted(
            (a for a in self.adjustments.values()
             if a.zone_id == zone_id and a.window_start == window_start),
            key=lambda a: (a.created_on, a.key),
        )

    def zones(self) -> list[str]:
        found = {m.zone_id for m in self.meters.values()}
        found.update(e.zone_id for e in self.events.values())
        found.update(t for tr in self.transfers.values() for t in (tr.from_zone, tr.to_zone))
        found.update(a.zone_id for a in self.adjustments.values())
        return sorted(found)

    # ---- 持久化 --------------------------------------------------------
    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self._dump(), ensure_ascii=False, indent=2),
                              encoding="utf-8")

    def _dump(self) -> dict:
        def dump(items: Iterable) -> list[dict]:
            rows = []
            for item in items:
                row = asdict(item)
                for k, v in list(row.items()):
                    if isinstance(v, date):
                        row[k] = v.isoformat()
                    elif isinstance(v, tuple):
                        row[k] = list(v)
                rows.append(row)
            return rows

        return {
            "meters": dump(self.meters.values()),
            "readings": dump(self.readings.values()),
            "replacements": dump(self.replacements.values()),
            "transfers": dump(self.transfers.values()),
            "events": dump(self.events.values()),
            "rules": dump(self.rules.values()),
            "adjustments": dump(self.adjustments.values()),
        }

    @classmethod
    def load(cls, path: str | Path) -> "Store":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        store = cls()
        handlers = {
            "meters": (Meter, store.add_meter),
            "readings": (Reading, store.add_reading),
            "replacements": (MeterReplacement, store.add_replacement),
            "transfers": (Transfer, store.add_transfer),
            "events": (WaterEvent, store.add_event),
            "rules": (EstimateRule, store.add_rule),
            "adjustments": (Adjustment, store.add_adjustment),
        }
        for section, (model, adder) in handlers.items():
            for row in data.get(section, []):
                row = dict(row)
                for k, v in list(row.items()):
                    if k.endswith(("_on", "_from", "_to")) and isinstance(v, str):
                        row[k] = date.fromisoformat(v)
                if "linked_keys" in row:
                    row["linked_keys"] = tuple(row["linked_keys"])
                if "applied_revisions" in row:
                    row["applied_revisions"] = tuple(row["applied_revisions"])
                adder(model(**row))
        return store
