import json, sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))
from water_balance.contracts import LedgerEntry, SettlementWindow

window = SettlementWindow("DMA-7", date(2026, 9, 1), date(2026, 10, 1))
entry = LedgerEntry("E-1", window.zone_id, "inflow", 1250.0)
print(json.dumps({"zone": window.zone_id, "entry": entry.entry_id, "volume": entry.volume_m3}, ensure_ascii=False))
