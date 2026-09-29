import sys, unittest
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from water_balance.contracts import LedgerEntry, SettlementWindow


class BalanceContractTests(unittest.TestCase):
    def test_window_retains_zone(self):
        item = SettlementWindow("Z", date(2026, 1, 1), date(2026, 2, 1))
        self.assertEqual(item.zone_id, "Z")

    def test_negative_volume_is_rejected(self):
        with self.assertRaises(ValueError):
            LedgerEntry("E", "Z", "outflow", -1)


if __name__ == "__main__": unittest.main()
