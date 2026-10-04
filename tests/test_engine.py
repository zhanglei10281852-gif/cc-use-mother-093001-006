import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from water_balance.engine import (
    compute_balance, identity_residual, month_bounds, verify_global_transfers,
)
from water_balance.contracts import ItemCode, MissingDataError, NotFound


def db_empty():
    return {
        "schema": 1, "zones": {}, "users": {}, "service_points": {},
        "meters": {}, "readings": {}, "readings_index": {}, "rules": {},
        "usages": {}, "transfers": {}, "batches": {}, "adjustments": {},
        "versions": {}, "version_index": {}, "counters": {},
    }


class EngineScenario:
    """以链式调用构造最小测试数据库。"""

    def __init__(self):
        self.db = db_empty()
        self.db["zones"]["Z"] = {"zone_id": "Z", "name": "Z"}
        self.db["zones"]["W"] = {"zone_id": "W", "name": "W"}
        self.n = 0

    def sp(self, sp_id, zone="Z", vf="2026-01-01", vt=None):
        self.db["service_points"][sp_id] = {
            "sp_id": sp_id, "zone_id": zone, "label": "",
            "valid_from": vf, "valid_to": vt}
        return self

    def meter(self, mid, sp_id, role, vf="2026-01-01", vt=None):
        self.db["meters"][mid] = {
            "meter_id": mid, "sp_id": sp_id, "role": role, "kind": "",
            "valid_from": vf, "valid_to": vt}
        return self

    def reading(self, mid, day, value):
        self.n += 1
        self.db["readings"][f"{mid}|{day}"] = {
            "reading_id": f"R{self.n}", "meter_id": mid,
            "read_on": day, "value": float(value),
            "batch_id": None, "recorded_at": "2026-10-01T00:00:00+00:00"}
        return self

    def usage(self, uid, zone, cat, ps, pe, daily):
        self.db["usages"][uid] = {
            "usage_id": uid, "zone_id": zone, "category": cat,
            "period_start": ps, "period_end": pe, "daily_m3": daily}
        return self

    def rule(self, rid="RULE", factor=1.0, cov=0.5, active=True):
        self.db["rules"][rid] = {
            "rule_id": rid, "method": "avg", "factor": factor,
            "min_coverage": cov, "valid_from": "2026-01-01",
            "valid_to": None, "active": active,
            "approved_by": "iss" if active else None,
            "approved_at": "2026-01-01T00:00:00+00:00" if active else None}
        return self

    def transfer(self, tid, fz, tz, day, vol):
        self.db["transfers"][tid] = {
            "transfer_id": tid, "from_zone": fz, "to_zone": tz,
            "transfer_date": day, "volume_m3": vol}
        return self


def master_user_pair(s: EngineScenario, master_end=1200.0,
                     user_end=900.0, month_start="2026-09-01",
                     month_end="2026-10-01"):
    s.sp("SPM").meter("MM", "SPM", "master")
    s.sp("SPU").meter("MU", "SPU", "user")
    s.reading("MM", month_start, 1000.0).reading("MM", month_end, master_end)
    s.reading("MU", month_start, 700.0).reading("MU", month_end, user_end)
    return s


class BasicBalanceTests(unittest.TestCase):
    def test_identity_and_nrw(self):
        s = master_user_pair(EngineScenario())
        r = compute_balance(s.db, "Z", "2026-09")
        self.assertEqual(r.volume(ItemCode.MASTER_SUPPLY), 200.0)
        self.assertEqual(r.volume(ItemCode.BILLED_METERED), 200.0)
        self.assertEqual(r.volume(ItemCode.NRW), 0.0)
        self.assertEqual(identity_residual(r), 0.0)

    def test_fire_and_unmetered_reduce_nrw(self):
        s = master_user_pair(EngineScenario(), master_end=1300.0,
                             user_end=900.0)
        s.usage("F1", "Z", "fire_water", "2026-09-01", "2026-09-11", 2.0)
        s.usage("U1", "Z", "authorized_unmetered",
                "2026-09-01", "2026-10-01", 1.0)
        r = compute_balance(s.db, "Z", "2026-09")
        self.assertEqual(r.volume(ItemCode.FIRE_WATER), 20.0)
        self.assertEqual(r.volume(ItemCode.AUTHORIZED_UNMETERED), 30.0)
        # 300 - 200(用户) - 20 - 30 = 50
        self.assertEqual(r.volume(ItemCode.NRW), 50.0)
        self.assertAlmostEqual(r.indicators["nrw_rate_pct"],
                               50 / 300 * 100, places=6)

    def test_usage_crossing_window_prorated(self):
        s = master_user_pair(EngineScenario())
        # 8/22–9/7 共16天，窗口内 9/1–9/7 6 天
        s.usage("F2", "Z", "fire_water", "2026-08-22", "2026-09-07", 3.0)
        r = compute_balance(s.db, "Z", "2026-09")
        self.assertEqual(r.volume(ItemCode.FIRE_WATER), 18.0)

    def test_transfer_legs(self):
        s = master_user_pair(EngineScenario())
        s.transfer("T1", "W", "Z", "2026-09-15", 40.0)
        s.transfer("T2", "Z", "W", "2026-09-20", 10.0)
        r = compute_balance(s.db, "Z", "2026-09")
        self.assertEqual(r.volume(ItemCode.TRANSFER_IN), 40.0)
        self.assertEqual(r.volume(ItemCode.TRANSFER_OUT), 10.0)
        chk = verify_global_transfers(s.db, "2026-09")
        self.assertEqual(chk["residual"], 0.0)

    def test_transfer_inside_window_by_event_date(self):
        s = master_user_pair(EngineScenario())
        s.transfer("T1", "W", "Z", "2026-10-02", 40.0)
        r = compute_balance(s.db, "Z", "2026-09")
        self.assertEqual(r.volume(ItemCode.TRANSFER_IN), 0.0)

    def test_unknown_zone(self):
        with self.assertRaises(NotFound):
            compute_balance(db_empty(), "NOPE", "2026-09")

    def test_month_bounds(self):
        self.assertEqual(month_bounds("2026-02"),
                         (date(2026, 2, 1), date(2026, 3, 1)))
        self.assertEqual(month_bounds("2026-12"),
                         (date(2026, 12, 1), date(2027, 1, 1)))
        with self.assertRaises(ValueError):
            month_bounds("2026/09")


class MissingAndEstimateTests(unittest.TestCase):
    def _gap_scenario(self):
        s = EngineScenario()
        s.sp("SPM").meter("MM", "SPM", "master")
        s.sp("SPU").meter("MU", "SPU", "user")
        s.reading("MM", "2026-09-01", 1000.0)
        s.reading("MM", "2026-10-01", 1300.0)
        # 用户只有 9/1–9/21 的 20 天实计 100 m³
        s.reading("MU", "2026-09-01", 0.0)
        s.reading("MU", "2026-09-21", 100.0)
        return s

    def test_missing_without_rule_rejected(self):
        s = self._gap_scenario()
        with self.assertRaises(MissingDataError) as ctx:
            compute_balance(s.db, "Z", "2026-09")
        self.assertEqual(ctx.exception.details[0]["uncovered_days"], 10)

    def test_estimate_applied_when_approved_rule(self):
        s = self._gap_scenario().rule(cov=0.5)
        r = compute_balance(s.db, "Z", "2026-09")
        self.assertEqual(len(r.estimates), 1)
        # 日均 100/20 = 5；缺口 10 天 => 50；300 - 实计100 - 估算50 = 150
        self.assertEqual(r.volume(ItemCode.ESTIMATED_METERED), 50.0)
        self.assertEqual(r.volume(ItemCode.NRW), 150.0)
        self.assertEqual(identity_residual(r), 0.0)

    def test_unapproved_rule_ignored(self):
        s = self._gap_scenario().rule(active=False)
        with self.assertRaises(MissingDataError):
            compute_balance(s.db, "Z", "2026-09")

    def test_coverage_below_floor_rejected(self):
        s = EngineScenario()
        s.sp("SPM").meter("MM", "SPM", "master")
        s.sp("SPU").meter("MU", "SPU", "user")
        s.reading("MM", "2026-09-01", 1000.0)
        s.reading("MM", "2026-10-01", 1300.0)
        # 仅覆盖 5/30 天
        s.reading("MU", "2026-09-01", 0.0)
        s.reading("MU", "2026-09-06", 20.0)
        s.rule(cov=0.5)
        with self.assertRaises(MissingDataError):
            compute_balance(s.db, "Z", "2026-09")

    def test_master_gap_never_estimated(self):
        s = EngineScenario()
        s.sp("SPM").meter("MM", "SPM", "master")
        s.sp("SPU").meter("MU", "SPU", "user")
        s.reading("MM", "2026-09-01", 1000.0)
        s.reading("MM", "2026-09-20", 1200.0)
        s.reading("MU", "2026-09-01", 0.0)
        s.reading("MU", "2026-10-01", 100.0)
        s.rule()
        with self.assertRaises(MissingDataError) as ctx:
            compute_balance(s.db, "Z", "2026-09")
        self.assertIn("总表", ctx.exception.details[0]["reason"])

    def test_full_coverage_no_estimate(self):
        s = master_user_pair(EngineScenario(), master_end=1200,
                             user_end=900).rule()
        r = compute_balance(s.db, "Z", "2026-09")
        self.assertEqual(r.estimates, ())
        self.assertEqual(r.volume(ItemCode.ESTIMATED_METERED), 0.0)


class MeterAndValidityTests(unittest.TestCase):
    def test_reset_to_zero_on_new_meter(self):
        s = EngineScenario()
        s.sp("SPM").meter("MM", "SPM", "master")
        s.sp("SPU")
        s.db["meters"]["U1"] = {"meter_id": "U1", "sp_id": "SPU",
                                "role": "user", "kind": "",
                                "valid_from": "2026-01-01",
                                "valid_to": "2026-09-15"}
        s.db["meters"]["U2"] = {"meter_id": "U2", "sp_id": "SPU",
                                "role": "user", "kind": "",
                                "valid_from": "2026-09-15",
                                "valid_to": None}
        s.reading("MM", "2026-09-01", 800.0)
        s.reading("MM", "2026-10-01", 1000.0)
        s.reading("U1", "2026-09-01", 800.0)
        s.reading("U1", "2026-09-15", 860.0)
        s.reading("U2", "2026-10-01", 30.0)
        r = compute_balance(s.db, "Z", "2026-09")
        # 旧表 60 + 新表 30
        self.assertEqual(r.volume(ItemCode.BILLED_METERED), 90.0)
        self.assertEqual(r.volume(ItemCode.NRW), 110.0)

    def test_readings_crossing_window_prorated(self):
        s = EngineScenario()
        s.sp("SPM").meter("MM", "SPM", "master")
        s.sp("SPU").meter("MU", "SPU", "user")
        # 56 天周期 1120 m³ => 窗口内 30 天 = 600
        s.reading("MM", "2026-09-01", 0.0)
        s.reading("MM", "2026-10-01", 600.0)
        s.reading("MU", "2026-08-15", 0.0)
        s.reading("MU", "2026-10-15", 560.0)  # 61 天, 窗口占 30 天
        r = compute_balance(s.db, "Z", "2026-09")
        self.assertAlmostEqual(r.volume(ItemCode.BILLED_METERED),
                               560 * 30 / 61, places=4)
        self.assertEqual(identity_residual(r), 0.0)

    def test_meter_expired_before_window_excluded(self):
        s = EngineScenario()
        s.sp("SPM").meter("MM", "SPM", "master")
        s.sp("SPU", vt="2026-08-01")
        s.meter("MU", "SPU", "user", vt="2026-08-01")
        s.reading("MM", "2026-09-01", 0.0)
        s.reading("MM", "2026-10-01", 100.0)
        r = compute_balance(s.db, "Z", "2026-09")
        self.assertEqual(r.volume(ItemCode.BILLED_METERED), 0.0)
        self.assertEqual(r.volume(ItemCode.NRW), 100.0)

    def test_digest_tracks_inputs(self):
        s = master_user_pair(EngineScenario())
        r1 = compute_balance(s.db, "Z", "2026-09")
        s.reading("MM", "2026-10-01", 1234.0)
        # 同键同值无变化；异值不允许直接改库，这里模拟另一张新读数
        r2 = compute_balance(s.db, "Z", "2026-09")
        self.assertNotEqual(r1.digest, r2.digest)


if __name__ == "__main__":
    unittest.main()
