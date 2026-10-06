"""平台行为测试：有效期、换表、估算、调水成对、幂等、调整单、状态机、API。"""
import json
import sys
import threading
import unittest
import urllib.request
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from water_balance import (
    Adjustment, Component as C, EstimateRule, IdempotencyConflict, Ledger,
    LedgerError, Meter, MeterReplacement, Reading, Role, Store, Transfer,
    VersionStatus, WaterEvent, compute_balance, month_window, verify_identity,
    verify_transfer_pairs,
)

SEP = date(2026, 9, 1)
OCT = date(2026, 10, 1)


def bulk_zone(store, zone="DMA-1", v0=0.0, v1=1000.0):
    mk = f"MB-{zone}"
    store.add_meter(Meter(mk, "bulk", zone, date(2025, 1, 1)))
    store.add_reading(Reading(f"{mk}:0", mk, SEP, v0))
    store.add_reading(Reading(f"{mk}:1", mk, OCT, v1))


class ValidityAndProrationTests(unittest.TestCase):
    def test_reading_interval_is_prorated_across_window(self):
        # 30 天区间读数增长 300；与窗口 [9/1,10/1) 相交 20 天 => 200
        store = Store()
        store.add_meter(Meter("MB", "bulk", "Z", date(2025, 1, 1)))
        store.add_reading(Reading("r0", "MB", date(2026, 9, 11), 0.0))
        store.add_reading(Reading("r1", "MB", date(2026, 10, 11), 300.0))
        snap = compute_balance(store, "Z", SEP, OCT)
        self.assertAlmostEqual(snap.components[C.BULK_INPUT], 200.0, places=6)
        self.assertEqual(verify_identity(snap), [])

    def test_meter_validity_period_clips_usage(self):
        # 总表 9/16 才装表生效，窗口只有后 15 天有供水
        store = Store()
        store.add_meter(Meter("MB", "bulk", "Z", date(2026, 9, 16)))
        store.add_reading(Reading("r0", "MB", date(2026, 9, 16), 0.0))
        store.add_reading(Reading("r1", "MB", OCT, 300.0))
        snap = compute_balance(store, "Z", SEP, OCT)
        self.assertEqual(snap.components[C.BULK_INPUT], 300.0)
        self.assertEqual(snap.warnings, ())

    def test_meter_replacement_zero_reset_bridges(self):
        store = Store()
        store.add_meter(Meter("A", "customer", "Z", date(2025, 1, 1),
                              date(2026, 9, 15)))
        store.add_meter(Meter("B", "customer", "Z", date(2026, 9, 15)))
        store.add_replacement(MeterReplacement(
            "RP", "A", "B", date(2026, 9, 15), 480.0, 0.0))
        store.add_reading(Reading("A0", "A", SEP, 300.0))
        store.add_reading(Reading("B1", "B", OCT, 120.0))
        snap = compute_balance(store, "Z", SEP, OCT)
        # 旧表段 180 用 14 天全在窗内；新表段 120 用 15 天全在窗内
        self.assertAlmostEqual(
            snap.components[C.CUSTOMER_CONSUMPTION], 300.0, places=6)


class EstimateTests(unittest.TestCase):
    def test_missing_reading_uses_approved_rule(self):
        store = Store()
        bulk_zone(store)
        store.add_meter(Meter("C1", "customer", "Z", date(2025, 1, 1)))
        store.add_rule(EstimateRule(
            "R1", "Z", "customer", 10, method="flat", parameter=250.0,
            approved_by="王部长"))
        snap = compute_balance(store, "Z", SEP, OCT)
        self.assertEqual(snap.components[C.CUSTOMER_CONSUMPTION], 250.0)
        self.assertEqual(snap.estimated_volume_m3, 250.0)
        est = [c for c in snap.contributions if c.source == "estimate"]
        self.assertEqual(est[0].rule_key, "R1")
        self.assertTrue(any("审批人 王部长" in c.detail for c in est))

    def test_unapproved_rule_rejected(self):
        with self.assertRaises(ValueError):
            EstimateRule("R", "Z", "customer", 1, approved_by="")

    def test_missing_without_rule_warns_and_counts_zero(self):
        store = Store()
        bulk_zone(store)
        store.add_meter(Meter("C1", "customer", "Z", date(2025, 1, 1)))
        snap = compute_balance(store, "Z", SEP, OCT)
        self.assertEqual(snap.components[C.CUSTOMER_CONSUMPTION], 0.0)
        self.assertTrue(any("无审批估算规则" in w for w in snap.warnings))


class TransferTests(unittest.TestCase):
    def test_transfer_posts_both_sides(self):
        store = Store()
        bulk_zone(store, "DMA-1")
        bulk_zone(store, "DMA-2", v1=500.0)
        store.add_transfer(Transfer(
            "T1", "DMA-1", "DMA-2", date(2026, 9, 12), 300.0,
            date(2026, 9, 12)))
        s1 = compute_balance(store, "DMA-1", SEP, OCT)
        s2 = compute_balance(store, "DMA-2", SEP, OCT)
        self.assertEqual(s1.components[C.TRANSFER_OUT], 300.0)
        self.assertEqual(s1.components[C.TRANSFER_IN], 0.0)
        self.assertEqual(s2.components[C.TRANSFER_IN], 300.0)
        self.assertEqual(verify_transfer_pairs(store, SEP, OCT), [])
        # 两侧净输入变化对称
        self.assertEqual(s1.net_input, 700.0)
        self.assertEqual(s2.net_input, 800.0)

    def test_self_transfer_rejected(self):
        with self.assertRaises(ValueError):
            Transfer("T", "Z", "Z", SEP, 1.0, SEP)

    def test_transfer_to_unregistered_zone_is_flagged(self):
        store = Store()
        bulk_zone(store, "DMA-1")
        store.add_transfer(Transfer(
            "T1", "DMA-1", "DMA-X", date(2026, 9, 12), 300.0,
            date(2026, 9, 12)))
        errs = verify_transfer_pairs(store, SEP, OCT)
        self.assertTrue(any("DMA-X" in e for e in errs))


class IdempotencyTests(unittest.TestCase):
    def test_duplicate_import_is_ignored_conflict_raises(self):
        store = Store()
        m = Meter("MB", "bulk", "Z", date(2025, 1, 1))
        self.assertTrue(store.add_meter(m))
        self.assertFalse(store.add_meter(Meter("MB", "bulk", "Z", date(2025, 1, 1))))
        with self.assertRaises(IdempotencyConflict):
            store.add_meter(Meter("MB", "bulk", "Z", date(2026, 1, 1)))
        # 调整单同样幂等
        adj = Adjustment("A1", "Z", SEP, C.FIRE_USE, 5.0, "x", OCT, "u")
        self.assertTrue(store.add_adjustment(adj))
        self.assertFalse(store.add_adjustment(adj))


class LedgerLifecycleTests(unittest.TestCase):
    def _seed(self):
        store = Store()
        bulk_zone(store, "DMA-7", v1=12400.0)
        store.add_meter(Meter("C1", "customer", "DMA-7", date(2025, 1, 1)))
        store.add_reading(Reading("C1:0", "C1", SEP, 100.0))
        store.add_reading(Reading("C1:1", "C1", OCT, 9100.0))
        store.add_event(WaterEvent("F1", "DMA-7", "fire", date(2026, 9, 20),
                                   150.0, date(2026, 9, 20)))
        return store

    def test_full_balance_and_identity(self):
        snap = compute_balance(self._seed(), "DMA-7", SEP, OCT)
        # 净输入 12400 = 用户 9000 + 消防 150 + 漏损 3250
        self.assertEqual(snap.net_input, 12400.0)
        self.assertEqual(snap.losses_m3, 3250.0)
        self.assertAlmostEqual(snap.loss_rate, 3250 / 12400, places=6)
        self.assertEqual(verify_identity(snap), [])

    def test_status_permissions_and_freeze(self):
        ledger = Ledger(self._seed())
        v = ledger.recompute("DMA-7", SEP, OCT, actor="张工")
        # 核算员不能直接签发
        with self.assertRaises(LedgerError):
            ledger.issue("DMA-7", SEP, "张工", Role.ANALYST)
        # 复核人不能提交核算员的动作
        with self.assertRaises(LedgerError):
            ledger.transition("DMA-7", SEP, VersionStatus.UNDER_REVIEW,
                              "李复核", Role.REVIEWER)
        ledger.submit("DMA-7", SEP, "张工")
        # 复核中不能重算
        with self.assertRaises(LedgerError):
            ledger.recompute("DMA-7", SEP, OCT)
        ledger.issue("DMA-7", SEP, "李复核", Role.REVIEWER, "ok")
        self.assertEqual(v.status, VersionStatus.ISSUED)
        # 签发后冻结
        with self.assertRaises(LedgerError):
            ledger.recompute("DMA-7", SEP, OCT)
        # 非管理员不能重开
        with self.assertRaises(LedgerError):
            ledger.reopen("DMA-7", SEP, "李复核", Role.REVIEWER, "要改")
        # 重开必须写原因
        with self.assertRaises(LedgerError):
            ledger.reopen("DMA-7", SEP, "赵管理", Role.ADMIN, "")

    def test_reopen_creates_new_revision_and_keeps_history(self):
        ledger = Ledger(self._seed())
        v1 = ledger.recompute("DMA-7", SEP, OCT, actor="张工")
        ledger.submit("DMA-7", SEP, "张工")
        ledger.issue("DMA-7", SEP, "李复核", Role.REVIEWER)
        fp1 = v1.snapshot.fingerprint
        ledger.store.add_adjustment(Adjustment(
            "ADJ1", "DMA-7", SEP, C.FIRE_USE, 20.0, "消防补录",
            date(2026, 10, 5), "张工"))
        v2 = ledger.reopen("DMA-7", SEP, "赵管理", Role.ADMIN, "消防补录修正")
        self.assertEqual(v1.status, VersionStatus.REOPENED)
        self.assertEqual(v2.revision, 2)
        self.assertEqual(v2.status, VersionStatus.DRAFT)
        self.assertNotEqual(fp1, v2.snapshot.fingerprint)
        # r1 的快照原封不动
        self.assertEqual(ledger.get("DMA-7", SEP, 1).snapshot.fingerprint, fp1)
        self.assertEqual(v2.reopen_reason, "消防补录修正")
        # 状态历史完整可追溯
        self.assertEqual(
            [e.to_status for e in v1.status_history],
            ["under_review", "issued", "reopened"])

    def test_late_reading_makes_adjustment_not_rewrite(self):
        store = self._seed()
        # C1 先有估算月末读数 9100；真实读数 9060 迟到
        store.add_reading(Reading("C1:1-est", "C1", OCT, 9100.0,
                                  estimated=True, source="estimate"))
        store.readings.pop("C1:1")
        ledger = Ledger(store)
        v1 = ledger.recompute("DMA-7", SEP, OCT, actor="张工")
        before_rate = v1.snapshot.loss_rate
        ledger.submit("DMA-7", SEP, "张工")
        ledger.issue("DMA-7", SEP, "李复核", Role.REVIEWER)

        adj = ledger.post_late_reading(
            Reading("C1:1-real", "C1", OCT, 9060.0, source="late"),
            SEP, OCT, actor="张工", reason="实抄到达")
        self.assertEqual(adj.component, C.CUSTOMER_CONSUMPTION)
        self.assertEqual(adj.delta_m3, -40.0)
        self.assertEqual(adj.supersedes, "C1:1-est")
        v2 = ledger.reopen("DMA-7", SEP, "赵管理", Role.ADMIN, "实抄修正")
        # 旧账未改：r1 漏损率不变；r2 漏损 +40（用水下调），漏损率上升
        self.assertEqual(ledger.get("DMA-7", SEP, 1).snapshot.loss_rate, before_rate)
        self.assertEqual(v2.snapshot.losses_m3 - v1.snapshot.losses_m3, 40.0)
        # 再次导入同一真实读数：幂等，不重复产生影响
        adj2 = ledger.post_late_reading(
            Reading("C1:1-real", "C1", OCT, 9060.0, source="late"),
            SEP, OCT, actor="张工", reason="实抄到达")
        self.assertIsNone(adj2)
        # 影响追踪
        trace = ledger.trace_impacts("DMA-7", SEP)
        diff = trace["diffs"][0]
        self.assertEqual(diff["losses_delta_m3"], 40.0)
        # 用户用水调整 delta=-40，对漏损的方向性影响为 +40
        self.assertEqual(diff["new_adjustments"][0]["loss_impact_m3"], 40.0)

    def test_snapshot_keeps_input_summary_and_diff_reason(self):
        ledger = Ledger(self._seed())
        v = ledger.recompute("DMA-7", SEP, OCT)
        keys = {(c.source, c.key) for c in v.snapshot.contributions}
        self.assertIn(("reading", "MB-DMA-7"), keys)
        self.assertIn(("event", "F1"), keys)


class PersistenceTests(unittest.TestCase):
    def test_store_and_versions_round_trip(self):
        store = Store()
        bulk_zone(store, "Z", v1=100.0)
        ledger = Ledger(store)
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.json"
            vp = Path(tmp) / "ver.json"
            v = ledger.recompute("Z", SEP, OCT, actor="u")
            ledger.submit("Z", SEP, "u")
            ledger.issue("Z", SEP, "r", Role.REVIEWER)
            store.save(db)
            ledger.save_versions(vp)

            store2 = Store.load(db)
            ledger2 = Ledger(store2)
            ledger2.load_versions(vp)
            got = ledger2.get("Z", SEP)
            self.assertEqual(got.status, VersionStatus.ISSUED)
            self.assertEqual(got.snapshot.fingerprint, v.snapshot.fingerprint)
            self.assertEqual(got.snapshot.components, v.snapshot.components)


class ApiTests(unittest.TestCase):
    def test_http_flow(self):
        from water_balance.api import build_server
        with TemporaryDirectory() as tmp:
            server = build_server(str(Path(tmp) / "db.json"),
                                  str(Path(tmp) / "v.json"), 0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def call(method, path, body=None):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}",
                        data=json.dumps(body).encode() if body is not None else None,
                        headers={"Content-Type": "application/json"},
                        method=method)
                    with urllib.request.urlopen(req) as resp:
                        return resp.status, json.loads(resp.read())

                payload = {
                    "meters": [
                        {"key": "MB", "meter_type": "bulk", "zone_id": "Z",
                         "valid_from": "2025-01-01"},
                        {"key": "C1", "meter_type": "customer", "zone_id": "Z",
                         "valid_from": "2025-01-01"},
                    ],
                    "readings": [
                        {"key": "MB:0", "meter_key": "MB", "read_on": "2026-09-01",
                         "value": 0.0},
                        {"key": "MB:1", "meter_key": "MB", "read_on": "2026-10-01",
                         "value": 1000.0},
                        {"key": "C1:0", "meter_key": "C1", "read_on": "2026-09-01",
                         "value": 0.0},
                        {"key": "C1:1", "meter_key": "C1", "read_on": "2026-10-01",
                         "value": 800.0},
                    ],
                }
                _, imported = call("POST", "/import", payload)
                self.assertEqual(imported["imported"]["meters"], 2)
                # 重复导入幂等
                _, again = call("POST", "/import", payload)
                self.assertEqual(again["imported"]["meters"], 0)

                _, v = call("POST", "/zones/Z/months/2026-09/recompute",
                            {"actor": "张工"})
                self.assertEqual(v["snapshot"]["losses_m3"], 200.0)
                call("POST", "/zones/Z/months/2026-09/transition",
                     {"to_status": "under_review", "actor": "张工",
                      "role": "analyst"})
                # 越权签发被拒
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    call("POST", "/zones/Z/months/2026-09/transition",
                         {"to_status": "issued", "actor": "张工",
                          "role": "analyst"})
                self.assertEqual(ctx.exception.code, 409)
                call("POST", "/zones/Z/months/2026-09/transition",
                     {"to_status": "issued", "actor": "李复核",
                      "role": "reviewer"})
                _, trace = call("GET", "/zones/Z/months/2026-09/trace")
                self.assertEqual(trace["revisions"][0]["status"], "issued")
                _, verify = call("GET", "/months/2026-09/verify")
                self.assertTrue(verify["identity_checks"][0]["identity_ok"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
