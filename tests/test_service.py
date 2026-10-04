import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from water_balance.contracts import (
    InvalidTransition, PermissionDenied, VersionState,
)
from water_balance.service import WaterBalanceService
from water_balance.storage import Repository


def make_service():
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    svc = WaterBalanceService(Repository(tmp.name))
    return svc, Path(tmp.name)


def bootstrap(svc):
    svc.create_user("__b__", "admin1", "管理员", ["admin"])
    svc.create_user("admin1", "op", "操作员", ["operator"])
    svc.create_user("admin1", "rev", "复核员", ["reviewer"])
    svc.create_user("admin1", "iss", "签发员", ["issuer"])
    svc.create_zone("op", "Z", "甲分区")
    svc.create_zone("op", "W", "乙分区")
    svc.register_service_point("op", "SPM", "Z", "2026-01-01")
    svc.install_meter("op", "MM", "SPM", "master", "2026-01-01")
    svc.register_service_point("op", "SPU", "Z", "2026-01-01")
    svc.install_meter("op", "MU", "SPU", "user", "2026-01-01")


def basic_readings(svc, master_end=1300.0, user_end=1000.0):
    svc.add_reading("op", "MM", "2026-09-01", 1000.0)
    svc.add_reading("op", "MM", "2026-10-01", master_end)
    svc.add_reading("op", "MU", "2026-09-01", 700.0)
    svc.add_reading("op", "MU", "2026-10-01", user_end)


def issue_first(svc, month="2026-09"):
    v = svc.recompute("op", "Z", month, "首算")
    svc.review_version("rev", v["version_id"], "通过")
    svc.issue_version("iss", v["version_id"])
    return v["version_id"]


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        bootstrap(self.svc)

    def test_role_separation(self):
        basic_readings(self.svc)
        vid = self.svc.recompute("op", "Z", "2026-09")["version_id"]
        with self.assertRaises(PermissionDenied):
            self.svc.review_version("op", vid)
        with self.assertRaises(PermissionDenied):
            self.svc.issue_version("rev", vid)
        with self.assertRaises(PermissionDenied):
            self.svc.reopen_version("iss", vid, "x")
        # 签发需要先复核
        self.svc.review_version("rev", vid)
        with self.assertRaises(PermissionDenied):
            self.svc.issue_version("op", vid)
        self.svc.issue_version("iss", vid)
        # 复核员不能重开
        with self.assertRaises(PermissionDenied):
            self.svc.reopen_version("rev", vid, "原因")

    def test_unknown_user_rejected(self):
        with self.assertRaises(PermissionDenied):
            self.svc.create_zone("ghost", "X", "x")

    def test_admin_bypass(self):
        # admin 可执行任何角色操作
        self.svc.register_service_point("admin1", "SPX", "Z",
                                        "2026-01-01")
        self.assertIn("SPX", self.svc.repo.snapshot()["service_points"])


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        bootstrap(self.svc)
        basic_readings(self.svc)

    def test_full_lifecycle_and_immutability(self):
        vid = issue_first(self.svc)
        version = self.svc.get_version(vid)
        self.assertEqual(version["state"], VersionState.ISSUED.value)
        # 已签发不可直接重算
        with self.assertRaises(InvalidTransition):
            self.svc.recompute("op", "Z", "2026-09")
        # 未复核不可签发
        v2 = self.svc.recompute("op", "W", "2026-09", "空分区也算")
        with self.assertRaises(InvalidTransition):
            self.svc.issue_version("iss", v2["version_id"])

    def test_cannot_review_non_review_version(self):
        vid = issue_first(self.svc)
        with self.assertRaises(InvalidTransition):
            self.svc.review_version("rev", vid)

    def test_reopen_requires_reason(self):
        vid = issue_first(self.svc)
        with self.assertRaises(ValueError):
            self.svc.reopen_version("admin1", vid, "  ")

    def test_recompute_is_idempotent_on_same_inputs(self):
        v1 = self.svc.recompute("op", "Z", "2026-09")
        v2 = self.svc.recompute("op", "Z", "2026-09")
        self.assertEqual(v1["version_id"], v2["version_id"])

    def test_reopen_without_change_rejected(self):
        vid = issue_first(self.svc)
        self.svc.reopen_version("admin1", vid, "例行检查")
        with self.assertRaises(InvalidTransition):
            self.svc.recompute("op", "Z", "2026-09", "无变化")


class AdjustmentFlowTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        bootstrap(self.svc)
        # B 点：月中读数 + 已审批估算规则
        self.svc.register_service_point("op", "SPB", "Z", "2026-01-01")
        self.svc.install_meter("op", "MB", "SPB", "user", "2026-01-01")
        self.svc.register_rule("op", "R1", "avg", 1.0, 0.5,
                               "2026-01-01", description="日均法")
        self.svc.approve_rule("iss", "R1")
        self.svc.add_reading("op", "MM", "2026-09-01", 1000.0)
        self.svc.add_reading("op", "MM", "2026-10-01", 1300.0)
        self.svc.add_reading("op", "MU", "2026-09-01", 700.0)
        self.svc.add_reading("op", "MU", "2026-10-01", 900.0)
        self.svc.add_reading("op", "MB", "2026-09-01", 100.0)
        self.svc.add_reading("op", "MB", "2026-09-21", 200.0)

    def test_estimate_then_late_reading_creates_adjustments(self):
        import time
        v1 = self.svc.recompute("op", "Z", "2026-09", "首算含估算")
        self.assertEqual(
            v1["result"]["totals"]["estimated_metered"], 50.0)  # 5/天×10
        self.svc.review_version("rev", v1["version_id"])
        self.svc.issue_version("iss", v1["version_id"])
        nrw1 = v1["result"]["indicators"]["nrw_m3"]

        time.sleep(1.05)
        # 迟到真实读数：B 全月实计 160
        self.svc.add_reading("op", "MB", "2026-10-01", 260.0)
        self.svc.reopen_version("admin1", v1["version_id"],
                                "B点迟到读数")
        v2 = self.svc.recompute("op", "Z", "2026-09", "真实替换估算")
        self.assertEqual(v2["rev"], 2)
        diffs = v2["variance"]["items"]
        self.assertAlmostEqual(
            diffs["billed_metered"]["delta"] +
            diffs["estimated_metered"]["delta"], 10.0)
        # 漏损下降 10 m³
        self.assertAlmostEqual(
            v2["result"]["indicators"]["nrw_m3"], nrw1 - 10.0)
        # 旧版本保持只读，差异与摘要完整
        old = self.svc.get_version(v1["version_id"])
        self.assertEqual(old["state"], VersionState.REOPENED.value)
        self.assertEqual(
            old["result"]["totals"]["estimated_metered"], 50.0)
        self.assertIn("digest", old["input_summary"])
        self.assertTrue(old["input_summary"]["reading_refs"])

        # 调整单存在且 OPEN；签发后转 APPLIED
        open_adj = self.svc.list_adjustments("Z", "2026-09", "open")
        self.assertTrue(open_adj)
        self.svc.review_version("rev", v2["version_id"])
        self.svc.issue_version("iss", v2["version_id"])
        applied = self.svc.list_adjustments("Z", "2026-09", "applied")
        self.assertEqual(len(applied), len(open_adj))
        # 差异原因随版本保存
        issued = self.svc.get_version(v2["version_id"])
        self.assertEqual(len(issued["variance_reasons"]), len(open_adj))
        # 链上状态
        chain = [v for v in self.svc.list_versions("Z", "2026-09")]
        states = {v["rev"]: v["state"] for v in chain}
        self.assertEqual(states[1], VersionState.SUPERSEDED.value)
        self.assertEqual(states[2], VersionState.ISSUED.value)

    def test_each_adjustment_carries_nrw_impact(self):
        import time
        vid = issue_first.__wrapped__ if False else None
        v1 = self.svc.recompute("op", "Z", "2026-09")
        self.svc.review_version("rev", v1["version_id"])
        self.svc.issue_version("iss", v1["version_id"])
        time.sleep(1.05)
        self.svc.add_reading("op", "MB", "2026-10-01", 260.0)
        self.svc.reopen_version("admin1", v1["version_id"], "x")
        v2 = self.svc.recompute("op", "Z", "2026-09", "x")
        impacts = sum(
            abs(a["nrw_impact_m3"])
            for a in self.svc.list_adjustments("Z", "2026-09", "open")
            if a["kind"] == "line_item" and a["item_code"] != "nrw")
        self.assertGreater(impacts, 0)


class ReadingIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        bootstrap(self.svc)
        basic_readings(self.svc)

    def test_same_reading_idempotent(self):
        r1 = self.svc.add_reading("op", "MM", "2026-11-01", 100.0)
        r2 = self.svc.add_reading("op", "MM", "2026-11-01", 100.0)
        self.assertEqual(r1["reading_id"], r2["reading_id"])

    def test_different_value_rejected_not_overwritten(self):
        from water_balance.contracts import Conflict
        self.svc.add_reading("op", "MM", "2026-11-01", 100.0)
        with self.assertRaises(Conflict):
            self.svc.add_reading("op", "MM", "2026-11-01", 120.0)
        db = self.svc.repo.snapshot()
        self.assertEqual(db["readings"]["MM|2026-11-01"]["value"], 100.0)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        bootstrap(self.svc)
        basic_readings(self.svc)

    def test_paired_legs_and_global_balance(self):
        self.svc.create_transfer("op", "W", "Z", "2026-09-10", 50.0)
        t = self.svc.repo.snapshot()["transfers"]
        legs = list(t.values())[0]["legs"]
        self.assertEqual(len(legs), 2)
        report = self.svc.verify("2026-09")
        self.assertTrue(report["identity_ok"])
        self.assertEqual(report["transfers"]["residual"], 0.0)

    def test_same_transfer_idempotent_conflict(self):
        from water_balance.contracts import Conflict
        self.svc.create_transfer("op", "W", "Z", "2026-09-10", 50.0)
        with self.assertRaises(Conflict):
            self.svc.create_transfer("op", "W", "Z", "2026-09-10", 50.0)

    def test_self_transfer_rejected(self):
        with self.assertRaises(ValueError):
            self.svc.create_transfer("op", "Z", "Z", "2026-09-10", 5.0)


class MeterLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        bootstrap(self.svc)

    def test_overlapping_meter_rejected(self):
        from water_balance.contracts import InvalidTransition
        with self.assertRaises(InvalidTransition):
            self.svc.install_meter("op", "MM2", "SPM", "master",
                                   "2026-06-01")

    def test_replace_meter_closes_old_and_inherits_role(self):
        m = self.svc.replace_meter("op", "SPM", "MM2", "2026-09-15")
        self.assertEqual(m["role"], "master")
        db = self.svc.repo.snapshot()
        self.assertEqual(db["meters"]["MM"]["valid_to"], "2026-09-15")

    def test_sp_close_caps_meter_validity(self):
        self.svc.close_service_point("op", "SPU", "2026-08-01")
        db = self.svc.repo.snapshot()
        self.assertEqual(db["meters"]["MU"]["valid_to"], "2026-08-01")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        bootstrap(self.svc)

    def test_hash_idempotence_order_insensitive(self):
        records = [
            {"type": "reading", "meter_id": "MM",
             "read_on": "2026-11-01", "value": 10.0},
            {"type": "transfer", "from_zone": "W", "to_zone": "Z",
             "transfer_date": "2026-11-02", "volume_m3": 3.0},
        ]
        a = self.svc.import_batch("op", "SRC", records)
        b = self.svc.import_batch("op", "SRC", list(reversed(records)))
        self.assertEqual(a["status"], "imported")
        self.assertEqual(b["status"], "duplicate")

    def test_invalid_record_aborts_whole_batch(self):
        from water_balance.contracts import Conflict
        good = {"type": "reading", "meter_id": "MM",
                "read_on": "2026-12-01", "value": 10.0}
        bad = {"type": "reading", "meter_id": "GHOST",
               "read_on": "2026-12-01", "value": 1.0}
        with self.assertRaises(Conflict):
            self.svc.import_batch("op", "MIX", [good, bad])
        db = self.svc.repo.snapshot()
        self.assertNotIn("MM|2026-12-01", db["readings"])

    def test_existing_identical_rows_count_as_duplicates(self):
        self.svc.add_reading("op", "MM", "2026-11-01", 10.0)
        out = self.svc.import_batch(
            "op", "SRC",
            [{"type": "reading", "meter_id": "MM",
              "read_on": "2026-11-01", "value": 10.0}])
        self.assertEqual(out["inserted"], 0)
        self.assertEqual(out["duplicates"], 1)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        bootstrap(self.svc)
        basic_readings(self.svc)

    def test_verify_recomputes_history(self):
        # 第二路总表供入 100 m³，无对应用户水量，形成漏损
        self.svc.register_service_point("op", "SPM2", "Z", "2026-01-01")
        self.svc.install_meter("op", "MM2", "SPM2", "master", "2026-01-01")
        self.svc.add_reading("op", "MM2", "2026-09-01", 0.0)
        self.svc.add_reading("op", "MM2", "2026-10-01", 100.0)
        report = self.svc.verify("2026-09")
        self.assertTrue(report["identity_ok"])
        z = next(r for r in report["zones"] if r["zone_id"] == "Z")
        self.assertEqual(z["nrw_m3"], 100.0)

    def test_preview_does_not_persist(self):
        before = len(self.svc.repo.snapshot()["versions"])
        self.svc.preview_balance("Z", "2026-09")
        self.assertEqual(len(self.svc.repo.snapshot()["versions"]), before)


if __name__ == "__main__":
    unittest.main()
