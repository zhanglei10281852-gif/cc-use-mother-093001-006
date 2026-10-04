"""应用服务：主数据注册、幂等导入、核算版本状态机与调整单。

状态机（每个 分区×月份 一条修订链）::

    (无) ──recompute──▶ REVIEW ──review──▶ REVIEW(已复核) ──issue──▶ ISSUED
                        ▲                                                   │
                        └──────────────── recompute ◀── REOPENED ◀──reopen──┘
    ISSUED 版本不可变；新读数到达后必须 reopen，再 recompute 生成新修订，
    旧签发版本在新版本签发时转为 SUPERSEDED 永久留痕。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Optional

from .contracts import (
    AdjustmentStatus,
    BalanceError,
    Conflict,
    InvalidTransition,
    NotFound,
    PermissionDenied,
    Role,
    VersionState,
)
from .engine import (
    compute_balance,
    identity_residual,
    month_bounds,
    verify_global_transfers,
)
from .storage import Repository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _d(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


_NRW_SIGN = {
    "master_supply": 1.0,
    "transfer_in": 1.0,
    "transfer_out": -1.0,
    "billed_metered": -1.0,
    "estimated_metered": -1.0,
    "fire_water": -1.0,
    "authorized_unmetered": -1.0,
    "nrw": 0.0,
}


class WaterBalanceService:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ============================================================== 权限

    def _user(self, db: dict[str, Any], user_id: str) -> dict[str, Any]:
        user = db["users"].get(user_id)
        if not user or not user.get("active", True):
            raise PermissionDenied(f"用户不存在或已停用: {user_id}")
        return user

    def _require(self, db: dict[str, Any], user_id: str,
                 *roles: Role) -> dict[str, Any]:
        user = self._user(db, user_id)
        granted = set(user["roles"])
        if Role.ADMIN.value in granted or any(r.value in granted for r in roles):
            return user
        raise PermissionDenied(
            f"用户 {user_id} 缺少角色: {', '.join(r.value for r in roles)}")

    # ============================================================== 基础资料

    def create_user(self, actor_id: str, user_id: str, name: str,
                    roles: list[str]) -> dict[str, Any]:
        for r in roles:
            Role(r)
        with self.repo.transaction() as db:
            # 系统中尚无任何用户时，允许首次引导创建（自举管理员）
            if db["users"]:
                self._require(db, actor_id, Role.ADMIN)
            if user_id in db["users"]:
                raise Conflict(f"用户已存在: {user_id}")
            record = {"user_id": user_id, "name": name,
                      "roles": sorted(set(roles)), "active": True,
                      "created_at": _now()}
            db["users"][user_id] = record
            return dict(record)

    def create_zone(self, operator_id: str, zone_id: str,
                    name: str) -> dict[str, Any]:
        with self.repo.transaction() as db:
            self._require(db, operator_id, Role.OPERATOR)
            if zone_id in db["zones"]:
                raise Conflict(f"分区已存在: {zone_id}")
            record = {"zone_id": zone_id, "name": name,
                      "created_at": _now()}
            db["zones"][zone_id] = record
            return dict(record)

    def register_service_point(self, operator_id: str, sp_id: str,
                               zone_id: str, valid_from: Any,
                               valid_to: Any = None,
                               label: str = "") -> dict[str, Any]:
        vf, vt = _d(valid_from), _d(valid_to) if valid_to else None
        if vt and vt <= vf:
            raise ValueError("有效期结束日必须晚于开始日")
        with self.repo.transaction() as db:
            self._require(db, operator_id, Role.OPERATOR)
            if sp_id in db["service_points"]:
                raise Conflict(f"供水点已存在: {sp_id}")
            if zone_id not in db["zones"]:
                raise NotFound(f"分区不存在: {zone_id}")
            record = {"sp_id": sp_id, "zone_id": zone_id, "label": label,
                      "valid_from": vf.isoformat(),
                      "valid_to": vt.isoformat() if vt else None,
                      "created_at": _now()}
            db["service_points"][sp_id] = record
            return dict(record)

    def close_service_point(self, operator_id: str, sp_id: str,
                            valid_to: Any) -> dict[str, Any]:
        vt = _d(valid_to)
        with self.repo.transaction() as db:
            self._require(db, operator_id, Role.OPERATOR)
            sp = db["service_points"].get(sp_id)
            if not sp:
                raise NotFound(f"供水点不存在: {sp_id}")
            if vt <= _d(sp["valid_from"]):
                raise ValueError("停用日期必须晚于启用日期")
            sp["valid_to"] = vt.isoformat()
            # 表计有效期不得超出供水点有效期
            for m in db["meters"].values():
                if m["sp_id"] == sp_id and (
                        not m["valid_to"] or _d(m["valid_to"]) > vt):
                    m["valid_to"] = vt.isoformat()
            return dict(sp)

    def install_meter(self, operator_id: str, meter_id: str, sp_id: str,
                      role: str, valid_from: Any,
                      valid_to: Any = None,
                      meter_kind: str = "") -> dict[str, Any]:
        role = role if isinstance(role, str) else role.value
        if role not in ("master", "user"):
            raise ValueError("表计角色必须是 master 或 user")
        vf = _d(valid_from)
        vt = _d(valid_to) if valid_to else None
        if vt and vt <= vf:
            raise ValueError("表计有效期结束日必须晚于开始日")
        with self.repo.transaction() as db:
            self._require(db, operator_id, Role.OPERATOR)
            if meter_id in db["meters"]:
                raise Conflict(f"表计已存在: {meter_id}")
            if sp_id not in db["service_points"]:
                raise NotFound(f"供水点不存在: {sp_id}")
            sp = db["service_points"][sp_id]
            if _d(sp["valid_from"]) > vf or (
                    sp["valid_to"] and _d(sp["valid_to"]) < (vt or vf)):
                raise InvalidTransition("表计有效期超出供水点有效期")
            self._assert_no_meter_overlap(db, sp_id, vf, vt)
            record = {"meter_id": meter_id, "sp_id": sp_id, "role": role,
                      "kind": meter_kind,
                      "valid_from": vf.isoformat(),
                      "valid_to": vt.isoformat() if vt else None,
                      "created_at": _now()}
            db["meters"][meter_id] = record
            return dict(record)

    def replace_meter(self, operator_id: str, sp_id: str,
                      new_meter_id: str, on_date: Any,
                      meter_kind: str = "") -> dict[str, Any]:
        """换表：在 on_date 将该供水点所有未停用表计归零停用，新装表计自
        on_date 起有效。角色与原表一致。"""
        day = _d(on_date)
        with self.repo.transaction() as db:
            self._require(db, operator_id, Role.OPERATOR)
            if sp_id not in db["service_points"]:
                raise NotFound(f"供水点不存在: {sp_id}")
            if new_meter_id in db["meters"]:
                raise Conflict(f"表计已存在: {new_meter_id}")
            active = [m for m in db["meters"].values()
                      if m["sp_id"] == sp_id and not m["valid_to"]]
            if not active:
                raise Conflict(f"供水点 {sp_id} 没有在用表计，无需换表")
            roles = {m["role"] for m in active}
            if len(roles) > 1:
                raise InvalidTransition("同一供水点存在多角色在用表计，"
                                        "请显式停用后再安装")
            for m in active:
                if day <= _d(m["valid_from"]):
                    raise InvalidTransition("换表日期必须晚于旧表启用日期")
                m["valid_to"] = day.isoformat()
            record = {"meter_id": new_meter_id, "sp_id": sp_id,
                      "role": roles.pop(), "kind": meter_kind,
                      "valid_from": day.isoformat(), "valid_to": None,
                      "replaces": sorted(m["meter_id"] for m in active),
                      "created_at": _now()}
            db["meters"][new_meter_id] = record
            return dict(record)

    @staticmethod
    def _assert_no_meter_overlap(db: dict[str, Any], sp_id: str,
                                 vf: date, vt: Optional[date]) -> None:
        new_end = vt or date.max
        for m in db["meters"].values():
            if m["sp_id"] != sp_id:
                continue
            end = _d(m["valid_to"]) if m["valid_to"] else date.max
            if max(vf, _d(m["valid_from"])) < min(new_end, end):
                raise InvalidTransition(
                    f"表计有效期与 {m['meter_id']} 重叠，换表请使用 replace-meter")

    # ============================================================== 读数

    def add_reading(self, operator_id: str, meter_id: str, read_on: Any,
                    value: float, batch_id: Optional[str] = None,
                    recorded_at: Optional[str] = None) -> dict[str, Any]:
        day = _d(read_on)
        value = float(value)
        if value < 0:
            raise ValueError("读数不能为负")
        with self.repo.transaction() as db:
            self._require(db, operator_id, Role.OPERATOR)
            if meter_id not in db["meters"]:
                raise NotFound(f"表计不存在: {meter_id}")
            key = f"{meter_id}|{day.isoformat()}"
            existing = db["readings"].get(key)
            if existing:
                if abs(float(existing["value"]) - value) < 1e-9:
                    return dict(existing)  # 幂等：同值重复提交
                raise Conflict(
                    f"读数 {key} 已存在不同值；真实修正请走调整流程，不得改账",
                    {"existing": existing["value"], "submitted": value})
            rid = f"RD-{self.repo.next_seq(db, 'reading'):06d}"
            record = {"reading_id": rid, "meter_id": meter_id,
                      "read_on": day.isoformat(), "value": value,
                      "batch_id": batch_id,
                      "recorded_at": recorded_at or _now()}
            db["readings"][key] = record
            db["readings_index"].setdefault(meter_id, []).append(key)
            return dict(record)

    # ============================================================== 消防/未计量

    def add_usage(self, operator_id: str, zone_id: str, category: str,
                  period_start: Any, period_end: Any, daily_m3: float,
                  usage_id: Optional[str] = None) -> dict[str, Any]:
        category = category if isinstance(category, str) else category.value
        if category not in ("fire_water", "authorized_unmetered"):
            raise ValueError("用水类别必须是 fire_water 或 authorized_unmetered")
        ps, pe = _d(period_start), _d(period_end)
        if pe <= ps or float(daily_m3) < 0:
            raise ValueError("用水期间或日水量无效")
        with self.repo.transaction() as db:
            self._require(db, operator_id, Role.OPERATOR)
            if zone_id not in db["zones"]:
                raise NotFound(f"分区不存在: {zone_id}")
            natural = f"{zone_id}|{category}|{ps}|{pe}"
            for u in db["usages"].values():
                if (f"{u['zone_id']}|{u['category']}|{u['period_start']}|"
                        f"{u['period_end']}") == natural:
                    raise Conflict(f"同期用水记录已存在: {u['usage_id']}")
            uid = usage_id or f"UG-{self.repo.next_seq(db, 'usage'):06d}"
            if uid in db["usages"]:
                raise Conflict(f"用水记录 ID 已存在: {uid}")
            record = {"usage_id": uid, "zone_id": zone_id,
                      "category": category,
                      "period_start": ps.isoformat(),
                      "period_end": pe.isoformat(),
                      "daily_m3": float(daily_m3), "created_at": _now()}
            db["usages"][uid] = record
            return dict(record)

    # ============================================================== 估算规则

    def register_rule(self, operator_id: str, rule_id: str, method: str,
                      factor: float, min_coverage: float,
                      valid_from: Any, valid_to: Any = None,
                      description: str = "") -> dict[str, Any]:
        factor, min_coverage = float(factor), float(min_coverage)
        if factor <= 0 or not 0 <= min_coverage <= 1:
            raise ValueError("估算系数必须为正，覆盖率下限应在 [0,1]")
        vf = _d(valid_from)
        vt = _d(valid_to) if valid_to else None
        if vt and vt <= vf:
            raise ValueError("规则有效期结束日必须晚于开始日")
        with self.repo.transaction() as db:
            self._require(db, operator_id, Role.OPERATOR)
            if rule_id in db["rules"]:
                raise Conflict(f"规则已存在: {rule_id}")
            record = {"rule_id": rule_id, "method": method,
                      "factor": factor, "min_coverage": min_coverage,
                      "valid_from": vf.isoformat(),
                      "valid_to": vt.isoformat() if vt else None,
                      "description": description, "active": False,
                      "approved_by": None, "approved_at": None,
                      "created_at": _now()}
            db["rules"][rule_id] = record
            return dict(record)

    def approve_rule(self, approver_id: str, rule_id: str) -> dict[str, Any]:
        with self.repo.transaction() as db:
            self._require(db, approver_id, Role.ISSUER)
            rule = db["rules"].get(rule_id)
            if not rule:
                raise NotFound(f"估算规则不存在: {rule_id}")
            if rule["active"]:
                raise Conflict(f"规则已审批: {rule_id}")
            rule["active"] = True
            rule["approved_by"] = approver_id
            rule["approved_at"] = _now()
            return dict(rule)

    # ============================================================== 跨分区调水（成对）

    def create_transfer(self, operator_id: str, from_zone: str, to_zone: str,
                        transfer_date: Any, volume_m3: float,
                        transfer_id: Optional[str] = None) -> dict[str, Any]:
        if from_zone == to_zone:
            raise ValueError("调水双方必须是不同分区")
        if float(volume_m3) <= 0:
            raise ValueError("调水量必须为正")
        day = _d(transfer_date)
        with self.repo.transaction() as db:
            self._require(db, operator_id, Role.OPERATOR)
            for z in (from_zone, to_zone):
                if z not in db["zones"]:
                    raise NotFound(f"分区不存在: {z}")
            natural = f"{from_zone}|{to_zone}|{day.isoformat()}"
            for t in db["transfers"].values():
                nk = (f"{t['from_zone']}|{t['to_zone']}|{t['transfer_date']}"
                      f"|{t['volume_m3']}")
                if nk == f"{natural}|{float(volume_m3)}":
                    raise Conflict(f"相同调水已存在: {t['transfer_id']}")
            tid = transfer_id or f"TR-{self.repo.next_seq(db, 'transfer'):06d}"
            if tid in db["transfers"]:
                raise Conflict(f"调水单 ID 已存在: {tid}")
            # 同一事务写两条腿，任一方失败整单不成立
            record = {"transfer_id": tid, "from_zone": from_zone,
                      "to_zone": to_zone, "transfer_date": day.isoformat(),
                      "volume_m3": float(volume_m3),
                      "legs": [
                          {"leg_id": f"{tid}#OUT", "zone_id": from_zone,
                           "direction": "out"},
                          {"leg_id": f"{tid}#IN", "zone_id": to_zone,
                           "direction": "in"}],
                      "created_at": _now()}
            db["transfers"][tid] = record
            return dict(record)

    # ============================================================== 批量导入（幂等）

    def import_batch(self, operator_id: str, source_ref: str,
                     records: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(records, list) or not records:
            raise ValueError("导入记录不能为空")
        # 记录排序后取摘要：重发批次即使行顺序不同仍判定为同一批
        canonical = "\n".join(sorted(
            json.dumps(rec, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"))
            for rec in records))
        content_hash = hashlib.sha256(
            f"{source_ref}\n{canonical}".encode("utf-8")).hexdigest()

        prior = self.repo.find_batch(content_hash)
        if prior:
            return {"batch_id": prior["batch_id"], "status": "duplicate",
                    "content_hash": content_hash,
                    "inserted": 0, "duplicates": prior["inserted"],
                    "first_batch_id": prior["batch_id"],
                    "message": "相同内容此前已导入，本次幂等跳过"}

        # 先全量校验，再落库：任何一条非法则整批拒绝
        prepared: list[tuple[str, tuple]] = []
        errors: list[dict[str, Any]] = []
        for i, rec in enumerate(records):
            try:
                prepared.append(self._prepare_import_record(rec))
            except BalanceError as exc:
                errors.append({"index": i, "code": exc.code,
                               "message": str(exc), "details": exc.details})
            except (KeyError, ValueError, TypeError) as exc:
                errors.append({"index": i, "code": "invalid_record",
                               "message": str(exc)})
        if errors:
            raise Conflict("导入批次存在非法记录，整批未写入", errors)

        with self.repo.transaction() as db:
            self._require(db, operator_id, Role.OPERATOR)
            # 再次防并发重复
            for b in db["batches"].values():
                if b["content_hash"] == content_hash and b["status"] != "failed":
                    return {"batch_id": b["batch_id"], "status": "duplicate",
                            "content_hash": content_hash, "inserted": 0,
                            "duplicates": b["inserted"],
                            "first_batch_id": b["batch_id"]}
            # 业务键冲突检查；完全相同的既有行按重复跳过（幂等）
            duplicates: list[int] = []
            errors: list[dict[str, Any]] = []
            deduped = self._check_import_conflicts(
                db, prepared, errors, duplicates)
            if errors:
                raise Conflict("导入批次与既有数据冲突，整批未写入", errors)

            bid = f"BT-{self.repo.next_seq(db, 'batch'):06d}"
            stamp = _now()
            inserted = {"reading": 0, "usage": 0, "transfer": 0}
            for kind, payload in deduped:
                if kind == "reading":
                    meter_id, day, value = payload
                    key = f"{meter_id}|{day}"
                    rid = f"RD-{self.repo.next_seq(db, 'reading'):06d}"
                    db["readings"][key] = {
                        "reading_id": rid, "meter_id": meter_id,
                        "read_on": day, "value": value, "batch_id": bid,
                        "recorded_at": stamp}
                    db["readings_index"].setdefault(meter_id, []).append(key)
                elif kind == "usage":
                    _, zone_id, category, ps, pe, daily = payload
                    uid = f"UG-{self.repo.next_seq(db, 'usage'):06d}"
                    db["usages"][uid] = {
                        "usage_id": uid, "zone_id": zone_id,
                        "category": category, "period_start": ps,
                        "period_end": pe, "daily_m3": daily,
                        "batch_id": bid, "created_at": stamp}
                else:
                    _, fz, tz, day, vol = payload
                    tid = f"TR-{self.repo.next_seq(db, 'transfer'):06d}"
                    db["transfers"][tid] = {
                        "transfer_id": tid, "from_zone": fz, "to_zone": tz,
                        "transfer_date": day, "volume_m3": vol,
                        "legs": [
                            {"leg_id": f"{tid}#OUT", "zone_id": fz,
                             "direction": "out"},
                            {"leg_id": f"{tid}#IN", "zone_id": tz,
                             "direction": "in"}],
                        "batch_id": bid, "created_at": stamp}
                inserted[kind] += 1
            db["batches"][bid] = {
                "batch_id": bid, "source_ref": source_ref,
                "content_hash": content_hash, "status": "imported",
                "inserted": sum(inserted.values()),
                "duplicates": len(duplicates),
                "counts": inserted, "imported_by": operator_id,
                "imported_at": stamp}
            return {"batch_id": bid, "status": "imported",
                    "content_hash": content_hash,
                    "inserted": sum(inserted.values()), "counts": inserted,
                    "duplicates": len(duplicates)}

    @staticmethod
    def _prepare_import_record(rec: dict[str, Any]) -> tuple[str, tuple]:
        kind = rec.get("type")
        if kind == "reading":
            day = _d(rec["read_on"]).isoformat()
            value = float(rec["value"])
            if value < 0:
                raise ValueError("读数不能为负")
            return ("reading", (str(rec["meter_id"]), day, value))
        if kind == "usage":
            ps, pe = _d(rec["period_start"]), _d(rec["period_end"])
            if pe <= ps:
                raise ValueError("用水期间结束日必须晚于开始日")
            daily = float(rec["daily_m3"])
            if daily < 0:
                raise ValueError("日水量不能为负")
            cat = rec["category"]
            if cat not in ("fire_water", "authorized_unmetered"):
                raise ValueError(f"非法用水类别: {cat}")
            return ("usage", (None, str(rec["zone_id"]), cat,
                              ps.isoformat(), pe.isoformat(), daily))
        if kind == "transfer":
            fz, tz = str(rec["from_zone"]), str(rec["to_zone"])
            if fz == tz:
                raise ValueError("调水双方必须是不同分区")
            vol = float(rec["volume_m3"])
            if vol <= 0:
                raise ValueError("调水量必须为正")
            return ("transfer",
                    (None, fz, tz, _d(rec["transfer_date"]).isoformat(), vol))
        raise ValueError(f"未知导入类型: {kind!r}")

    @staticmethod
    def _check_import_conflicts(db: dict[str, Any],
                                prepared: list[tuple[str, tuple]],
                                errors: list[dict[str, Any]],
                                duplicates: list[int]
                                ) -> list[tuple[str, tuple]]:
        """返回去重后待写入的记录。与既有数据完全一致的行计入 duplicates；
        只有异值读数才算冲突。"""
        seen_readings: dict[str, float] = {}
        seen_usages: set[str] = set()
        seen_transfers: set[str] = set()
        deduped: list[tuple[str, tuple]] = []
        for i, (kind, p) in enumerate(prepared):
            if kind == "reading":
                meter_id, day, value = p
                if meter_id not in db["meters"]:
                    errors.append({"index": i, "code": "not_found",
                                   "message": f"表计不存在: {meter_id}"})
                    continue
                key = f"{meter_id}|{day}"
                if key in seen_readings:
                    if seen_readings[key] == value:
                        duplicates.append(i)
                    else:
                        errors.append({"index": i, "code": "conflict",
                                       "message": f"批次内读数同键异值: {key}"})
                    continue
                seen_readings[key] = value
                old = db["readings"].get(key)
                if old:
                    if abs(float(old["value"]) - value) < 1e-9:
                        duplicates.append(i)          # 已存在同值，幂等跳过
                    else:
                        errors.append({"index": i, "code": "conflict",
                                       "message": f"读数已存在不同值: {key}，"
                                                  "真实修正须走调整流程"})
                    continue
            elif kind == "usage":
                _, zone_id, cat, ps, pe, daily = p
                if zone_id not in db["zones"]:
                    errors.append({"index": i, "code": "not_found",
                                   "message": f"分区不存在: {zone_id}"})
                    continue
                nk = f"{zone_id}|{cat}|{ps}|{pe}"
                if nk in seen_usages:
                    duplicates.append(i)
                    continue
                seen_usages.add(nk)
                matched = next(
                    (u for u in db["usages"].values()
                     if (f"{u['zone_id']}|{u['category']}|"
                         f"{u['period_start']}|{u['period_end']}") == nk),
                    None)
                if matched:
                    if abs(float(matched["daily_m3"]) - daily) < 1e-9:
                        duplicates.append(i)
                    else:
                        errors.append({"index": i, "code": "conflict",
                                       "message": f"同期用水已存在不同日水量: {nk}"})
                    continue
            else:
                _, fz, tz, day, vol = p
                for z in (fz, tz):
                    if z not in db["zones"]:
                        errors.append({"index": i, "code": "not_found",
                                       "message": f"分区不存在: {z}"})
                nk = f"{fz}|{tz}|{day}|{vol}"
                if nk in seen_transfers:
                    duplicates.append(i)
                    continue
                seen_transfers.add(nk)
                matched = next(
                    (t for t in db["transfers"].values()
                     if (f"{t['from_zone']}|{t['to_zone']}|"
                         f"{t['transfer_date']}|{t['volume_m3']}") == nk),
                    None)
                if matched:
                    duplicates.append(i)
                    continue
            deduped.append((kind, p))
        return deduped

    # ============================================================== 核算与版本

    def _chain(self, db: dict[str, Any], zone_id: str,
               month: str) -> list[dict[str, Any]]:
        return db["version_index"].get(f"{zone_id}|{month}", [])

    @staticmethod
    def _baseline(chain: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """差异基线：修订链上最近一个曾签发的版本（签发或重开态）。"""
        for v in reversed(chain):
            if v["state"] in (VersionState.ISSUED.value,
                              VersionState.REOPENED.value):
                return v
        return None

    @staticmethod
    def _versions(db: dict[str, Any], keys: list[str]) -> list[dict[str, Any]]:
        return [db["versions"][k] for k in keys]

    def preview_balance(self, zone_id: str, month: str) -> dict[str, Any]:
        """只读试算，不落任何账。"""
        month_bounds(month)
        db = self.repo.snapshot()
        result = compute_balance(db, zone_id, month)
        return self._serialize_result(result)

    def recompute(self, operator_id: str, zone_id: str, month: str,
                  note: str = "") -> dict[str, Any]:
        month_bounds(month)
        with self.repo.transaction() as db:
            self._require(db, operator_id, Role.OPERATOR)
            chain_keys = self._chain(db, zone_id, month)
            chain = self._versions(db, chain_keys)
            if chain:
                current = chain[-1]
                if current["state"] == VersionState.ISSUED.value:
                    raise InvalidTransition(
                        f"{zone_id} {month} 已签发，须先由管理员重开才能重算")
            result = compute_balance(db, zone_id, month)
            base = self._baseline(chain)

            # 同输入摘要且无新签发基线变化 => 不产生重复修订
            if chain and result.digest == chain[-1]["input_digest"] and \
                    chain[-1]["state"] != VersionState.REOPENED.value:
                return dict(chain[-1])
            if base and base["state"] == VersionState.REOPENED.value and \
                    result.digest == base["input_digest"]:
                raise InvalidTransition(
                    "重开后核算输入未发生变化（摘要一致），无调整可生成；"
                    "如需新版本请先补录真实读数/单据")

            adjustments, variance = self._diff_against(db, base, result, zone_id,
                                                       month)
            rev = (chain[-1]["rev"] + 1) if chain else 1
            vid = f"VB-{self.repo.next_seq(db, 'version'):06d}"
            record = {
                "version_id": vid, "zone_id": zone_id, "month": month,
                "rev": rev, "state": VersionState.REVIEW.value,
                "input_digest": result.digest,
                "result": self._serialize_result(result),
                "input_summary": self._input_summary(db, result),
                "variance": variance,
                "adjustment_ids": [a["adjustment_id"] for a in adjustments],
                "supersedes": base["version_id"] if base else None,
                "note": note,
                "created_by": operator_id, "created_at": _now(),
                "reviewed_by": None, "reviewed_at": None,
                "issued_by": None, "issued_at": None,
                "reopened_from": None,
            }
            # 上一个复核中的草稿修订转为留痕；曾签发（含已重开）的基线
            # 保留状态，待新版本签发时再转为 SUPERSEDED
            if chain and chain[-1]["state"] == VersionState.REVIEW.value:
                chain[-1]["state"] = VersionState.SUPERSEDED.value
            key = f"{zone_id}|{month}|{rev}"
            db["versions"][key] = record
            chain_keys.append(key)
            db["version_index"][f"{zone_id}|{month}"] = chain_keys
            for adj in adjustments:
                db["adjustments"][adj["adjustment_id"]] = adj
            return dict(record)

    def review_version(self, reviewer_id: str, version_id: str,
                       comment: str = "") -> dict[str, Any]:
        with self.repo.transaction() as db:
            self._require(db, reviewer_id, Role.REVIEWER)
            version = self._get_version(db, version_id)
            if version["state"] != VersionState.REVIEW.value:
                raise InvalidTransition(
                    f"版本当前状态 {version['state']}，不可复核")
            residual = identity_residual(_materialize(version))
            if abs(residual) > 1e-6:
                raise InvalidTransition(f"总量恒等式残差 {residual}，复核不通过")
            version["state"] = VersionState.REVIEW.value
            version["reviewed_by"] = reviewer_id
            version["reviewed_at"] = _now()
            version["review_comment"] = comment
            version["reviewed"] = True
            return dict(version)

    def issue_version(self, issuer_id: str, version_id: str,
                      variance_reasons: Optional[dict[str, str]] = None
                      ) -> dict[str, Any]:
        with self.repo.transaction() as db:
            self._require(db, issuer_id, Role.ISSUER)
            version = self._get_version(db, version_id)
            if version["state"] != VersionState.REVIEW.value:
                raise InvalidTransition(
                    f"版本当前状态 {version['state']}，不可签发")
            if not version.get("reviewed"):
                raise InvalidTransition("版本未经复核，不可签发")
            residual = identity_residual(_materialize(version))
            if abs(residual) > 1e-6:
                raise InvalidTransition(f"总量恒等式残差 {residual}，不得签发")

            # 差异原因必须随签发保留：逐项确认或采用调整单自动原因
            adj_records = [db["adjustments"][a]
                           for a in version["adjustment_ids"]]
            reasons = self._resolve_reasons(adj_records, variance_reasons)
            version["variance_reasons"] = reasons

            chain_keys = self._chain(db, version["zone_id"], version["month"])
            for k in chain_keys:
                v = db["versions"][k]
                if v["version_id"] != version_id and v["state"] in (
                        VersionState.ISSUED.value,
                        VersionState.REOPENED.value):
                    v["state"] = VersionState.SUPERSEDED.value
            version["state"] = VersionState.ISSUED.value
            version["issued_by"] = issuer_id
            version["issued_at"] = _now()
            for adj in adj_records:
                adj["status"] = AdjustmentStatus.APPLIED.value
                adj["applied_in"] = version_id
            return dict(version)

    def reopen_version(self, admin_id: str, version_id: str,
                       reason: str) -> dict[str, Any]:
        if not reason or not reason.strip():
            raise ValueError("重开必须填写原因")
        with self.repo.transaction() as db:
            self._require(db, admin_id, Role.ADMIN)
            version = self._get_version(db, version_id)
            if version["state"] != VersionState.ISSUED.value:
                raise InvalidTransition(
                    f"版本当前状态 {version['state']}，仅已签发版本可重开")
            version["state"] = VersionState.REOPENED.value
            version["reopened_by"] = admin_id
            version["reopened_at"] = _now()
            version["reopen_reason"] = reason.strip()
            return dict(version)

    def get_version(self, version_id: str) -> dict[str, Any]:
        db = self.repo.snapshot()
        return dict(self._get_version(db, version_id))

    def list_versions(self, zone_id: Optional[str] = None,
                      month: Optional[str] = None) -> list[dict[str, Any]]:
        db = self.repo.snapshot()
        out = []
        for v in db["versions"].values():
            if zone_id and v["zone_id"] != zone_id:
                continue
            if month and v["month"] != month:
                continue
            out.append({"version_id": v["version_id"], "zone_id": v["zone_id"],
                        "month": v["month"], "rev": v["rev"],
                        "state": v["state"], "input_digest": v["input_digest"],
                        "created_by": v["created_by"],
                        "created_at": v["created_at"],
                        "issued_by": v.get("issued_by"),
                        "issued_at": v.get("issued_at"),
                        "supersedes": v.get("supersedes"),
                        "nrw_m3": v["result"]["indicators"]["nrw_m3"],
                        "nrw_rate_pct": v["result"]["indicators"]["nrw_rate_pct"]})
        return sorted(out, key=lambda x: (x["zone_id"], x["month"], x["rev"]))

    def list_adjustments(self, zone_id: Optional[str] = None,
                         month: Optional[str] = None,
                         status: Optional[str] = None) -> list[dict[str, Any]]:
        db = self.repo.snapshot()
        out = []
        for a in db["adjustments"].values():
            if zone_id and a["zone_id"] != zone_id:
                continue
            if month and a["month"] != month:
                continue
            if status and a["status"] != status:
                continue
            out.append(dict(a))
        return sorted(out, key=lambda x: (x["zone_id"], x["month"],
                                          x["adjustment_id"]))

    def verify(self, month: str,
               zones: Optional[list[str]] = None) -> dict[str, Any]:
        """重算指定月份全部（或给定）分区，核对恒等式与跨分区守恒。"""
        month_bounds(month)
        db = self.repo.snapshot()
        zone_ids = zones or sorted(db["zones"])
        rows = []
        ok = True
        for zid in zone_ids:
            if zid not in db["zones"]:
                raise NotFound(f"分区不存在: {zid}")
            res = compute_balance(db, zid, month)
            residual = identity_residual(res)
            row_ok = abs(residual) <= 1e-6
            ok = ok and row_ok
            rows.append({"zone_id": zid, "residual_m3": residual,
                         "ok": row_ok,
                         "nrw_m3": res.indicators["nrw_m3"],
                         "nrw_rate_pct": res.indicators["nrw_rate_pct"],
                         "digest": res.digest})
        transfers = verify_global_transfers(db, month)
        ok = ok and abs(transfers["residual"]) <= 1e-6
        return {"month": month, "identity_ok": ok, "zones": rows,
                "transfers": transfers}

    # ------------------------------------------------------------ 差异/调整单

    def _diff_against(self, db: dict[str, Any],
                      base: Optional[dict[str, Any]], result,
                      zone_id: str, month: str
                      ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if base is None:
            return [], {"base_version_id": None, "items": {},
                        "reasons": [], "estimate_settlements": [],
                        "late_readings": []}
        before = base["result"]["totals"]
        after = result.totals
        item_diffs: dict[str, dict[str, Any]] = {}
        adjustments: list[dict[str, Any]] = []
        reasons: list[str] = []
        for code, new_val in after.items():
            old_val = before.get(code, 0.0)
            delta = round(new_val - old_val, 6)
            if abs(delta) < 1e-9:
                continue
            impact = round(_NRW_SIGN.get(code, 0.0) * delta, 6)
            reason = self._explain_delta(db, base, code, delta, result)
            item_diffs[code] = {"before": old_val, "after": new_val,
                                "delta": delta, "nrw_impact_m3": impact,
                                "reason": reason}
            reasons.append(f"{code}: {reason}（{delta:+.3f} m³，"
                           f"对漏损影响 {impact:+.3f} m³）")
            adjustments.append(self._make_adjustment(
                db, zone_id, month, base, "line_item", code, delta, impact,
                reason, result))

        # 估算 → 真实读数的逐供水点结算
        old_est = {e["service_point_id"]: e
                   for e in base["result"].get("estimates", [])}
        settlements = []
        new_est = {e.service_point_id: e for e in result.estimates}
        for sp_id, old in old_est.items():
            actual_now = result.sp_actual.get(sp_id, 0.0)
            actual_before = round(old.get("actual_alongside_m3", 0.0), 6)
            arrived = round(actual_now - actual_before, 6)
            if sp_id not in new_est and (arrived > 0 or old["volume_m3"]):
                # 该供水点本月不再估算：真实读数覆盖了原先缺口。
                # 行项目差异（billed_metered/estimated_metered）已生成调整单，
                # 此处仅作归并说明。
                impact = round(-old["volume_m3"] + arrived, 6)
                reason = "迟到真实读数到达，估算水量被实际用量替换"
                settlements.append({
                    "service_point_id": sp_id,
                    "estimated_before_m3": old["volume_m3"],
                    "actual_arrived_m3": arrived,
                    "nrw_impact_m3": impact, "reason": reason})
            elif sp_id in new_est:
                e = new_est[sp_id]
                delta_est = round(e.volume_m3 - old["volume_m3"], 6)
                if abs(delta_est) >= 1e-9:
                    reason = "真实读数部分到达后，剩余缺口按规则重新估算"
                    settlements.append({
                        "service_point_id": sp_id,
                        "estimated_before_m3": old["volume_m3"],
                        "estimated_after_m3": e.volume_m3,
                        "actual_arrived_m3": arrived,
                        "nrw_impact_m3": round(-delta_est - arrived, 6),
                        "reason": reason})

        late_readings = self._late_sources(db, base, zone_id, month)
        rate_before = base["result"]["indicators"]["nrw_rate_pct"]
        rate_after = result.indicators["nrw_rate_pct"]
        variance = {
            "base_version_id": base["version_id"],
            "items": item_diffs,
            "estimate_settlements": settlements,
            "late_readings": late_readings,
            "nrw_m3_before": base["result"]["indicators"]["nrw_m3"],
            "nrw_m3_after": result.indicators["nrw_m3"],
            "nrw_rate_pct_before": rate_before,
            "nrw_rate_pct_after": rate_after,
            "nrw_rate_delta_pct": round(rate_after - rate_before, 6),
            "reasons": reasons,
        }
        return adjustments, variance

    def _make_adjustment(self, db: dict[str, Any], zone_id: str, month: str,
                         base: dict[str, Any], kind: str, item_code: str,
                         delta_m3: float, nrw_impact: float, reason: str,
                         result, sp_id: Optional[str] = None) -> dict[str, Any]:
        return {
            "adjustment_id": f"AD-{self.repo.next_seq(db, 'adjustment'):06d}",
            "zone_id": zone_id, "month": month,
            "from_version_id": base["version_id"],
            "to_digest": result.digest,
            "kind": kind, "item_code": item_code,
            "service_point_id": sp_id,
            "delta_m3": delta_m3, "nrw_impact_m3": nrw_impact,
            "reason": reason, "status": AdjustmentStatus.OPEN.value,
            "applied_in": None, "created_at": _now()}

    def _explain_delta(self, db: dict[str, Any], base: dict[str, Any],
                       code: str, delta: float, result) -> str:
        late = self._late_sources(db, base, base["zone_id"], base["month"])
        if late and code in ("billed_metered", "master_supply"):
            return f"签发后迟到读数 {len(late)} 笔入账"
        if code == "estimated_metered":
            return "缺数覆盖变化导致估算水量调整"
        if code in ("transfer_in", "transfer_out"):
            return "跨分区调水成对腿重估"
        if code in ("fire_water", "authorized_unmetered"):
            return "消防或合法未计量用水有效期/单据调整"
        if code == "nrw":
            return "上述修正传导后的漏损残值变化"
        return "重算输入变化"

    def _late_sources(self, db: dict[str, Any], base: dict[str, Any],
                      zone_id: str, month: str) -> list[dict[str, Any]]:
        issued_at = base.get("issued_at") or base.get("created_at")
        lo, hi = month_bounds(month)
        sp_ids = {sp["sp_id"] for sp in db["service_points"].values()
                  if sp["zone_id"] == zone_id}
        meter_ids = {m["meter_id"] for m in db["meters"].values()
                     if m["sp_id"] in sp_ids}
        out = []
        for r in db["readings"].values():
            # 闭账日当天读数表征窗口内最后一段用量，同样视为窗口输入
            if r["meter_id"] in meter_ids and issued_at and \
                    r.get("recorded_at", "") > issued_at and \
                    lo < _d(r["read_on"]) <= hi:
                out.append({"reading_id": r["reading_id"],
                            "meter_id": r["meter_id"],
                            "read_on": r["read_on"],
                            "value": r["value"],
                            "recorded_at": r["recorded_at"]})
        return sorted(out, key=lambda x: x["reading_id"])

    @staticmethod
    def _resolve_reasons(adjustments: list[dict[str, Any]],
                         overrides: Optional[dict[str, str]]) -> dict[str, str]:
        reasons = {}
        for a in adjustments:
            key = a["adjustment_id"]
            reasons[key] = (overrides or {}).get(key, a["reason"])
            if not reasons[key].strip():
                raise InvalidTransition(
                    f"调整单 {key} 缺少差异原因，不得签发")
        return reasons

    # ------------------------------------------------------------ 序列化/摘要

    @staticmethod
    def _serialize_result(result) -> dict[str, Any]:
        return {
            "zone_id": result.zone_id, "month": result.month,
            "window": [result.starts_on.isoformat(),
                       result.ends_on.isoformat()],
            "items": [{"code": i.code, "label": i.label,
                       "volume_m3": i.volume_m3, "estimated": i.estimated,
                       "ref": i.ref, "detail": i.detail}
                      for i in result.items],
            "totals": result.totals,
            "indicators": result.indicators,
            "estimates": [{"service_point_id": e.service_point_id,
                           "meter_ids": list(e.meter_ids),
                           "uncovered_days": e.uncovered_days,
                           "actual_alongside_m3": e.actual_alongside_m3,
                           "volume_m3": e.volume_m3, "rule_id": e.rule_id,
                           "basis": e.basis} for e in result.estimates],
            "source_refs": {k: list(v) for k, v in result.source_refs.items()},
            "sp_actual": result.sp_actual,
            "digest": result.digest,
        }

    @staticmethod
    def _input_summary(db: dict[str, Any], result) -> dict[str, Any]:
        refs = result.source_refs
        return {
            "digest": result.digest,
            "window": [result.starts_on.isoformat(),
                       result.ends_on.isoformat()],
            "reading_refs": sorted(set(refs.get("master_supply", ()))
                                   | set(refs.get("billed_metered", ()))),
            "usage_refs": sorted(set(refs.get("fire_water", ()))
                                 | set(refs.get("authorized_unmetered", ()))),
            "transfer_refs": sorted(set(refs.get("transfer_in", ()))
                                    | set(refs.get("transfer_out", ()))),
            "estimate_rule_ids": sorted({e.rule_id for e in result.estimates}),
            "n_estimates": len(result.estimates),
        }

    @staticmethod
    def _get_version(db: dict[str, Any], version_id: str) -> dict[str, Any]:
        for v in db["versions"].values():
            if v["version_id"] == version_id:
                return v
        raise NotFound(f"平衡版本不存在: {version_id}")


def _materialize(version: dict[str, Any]):
    """把存储的版本结果还原成带 ``totals`` 的轻量对象供恒等式核对。"""
    from types import SimpleNamespace
    return SimpleNamespace(totals=version["result"]["totals"],
                           indicators=version["result"]["indicators"])
