"""命令行入口：导入数据、重算任意历史月份、状态流转、核对与追踪。

示例：
  python run_cli.py import data.json --db led.db
  python run_cli.py recompute DMA-7 2026-09 --actor 张工
  python run_cli.py issue DMA-7 2026-09 --actor 李复核 --role reviewer
  python run_cli.py verify 2026-09
  python run_cli.py trace DMA-7 2026-09
  python run_cli.py serve --port 8080
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .ledger import Ledger, LedgerError, month_window
from .models import Reading
from .snapshots import Role, VersionStatus, to_jsonable
from .store import IdempotencyConflict, Store, ingest_dict
from .engine import verify_identity, verify_transfer_pairs

DEFAULT_DB = "balance_db.json"
DEFAULT_VERSIONS = "balance_versions.json"


def _parse_month(text: str) -> tuple[date, date]:
    year, month = text.split("-")
    return month_window(int(year), int(month))


def _parse_date(text: str) -> date:
    return date.fromisoformat(text)


class Workspace:
    def __init__(self, db: str, versions: str) -> None:
        self.db_path = db
        self.versions_path = versions
        self.store = Store.load(db) if Path(db).exists() else Store()
        self.ledger = Ledger(self.store)
        self.ledger.load_versions(versions)

    def save(self) -> None:
        self.store.save(self.db_path)
        self.ledger.save_versions(self.versions_path)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_import(ws: Workspace, args) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    try:
        counts = ingest_dict(ws.store, payload)
    except IdempotencyConflict as exc:
        print(f"幂等冲突：{exc}", file=sys.stderr)
        return 2
    ws.save()
    _print({"imported": counts})
    return 0


def cmd_recompute(ws: Workspace, args) -> int:
    start, end = _parse_month(args.month)
    try:
        v = ws.ledger.recompute(args.zone, start, end, actor=args.actor)
    except LedgerError as exc:
        print(f"拒绝重算：{exc}", file=sys.stderr)
        return 3
    ws.save()
    _print(v.to_dict())
    return 0


def cmd_show(ws: Workspace, args) -> int:
    start, _ = _parse_month(args.month)
    revision = int(args.revision) if args.revision else None
    _print(ws.ledger.get(args.zone, start, revision).to_dict())
    return 0


def _do_transition(ws, args, to_status, role) -> int:
    start, _ = _parse_month(args.month)
    try:
        v = ws.ledger.transition(args.zone, start, to_status,
                                 args.actor, role, args.note or "")
    except LedgerError as exc:
        print(f"状态流转被拒绝：{exc}", file=sys.stderr)
        return 3
    ws.save()
    _print({"version_id": v.version_id, "status": v.status.value,
            "history": [e.__dict__ for e in v.status_history]})
    return 0


def cmd_submit(ws, args):
    return _do_transition(ws, args, VersionStatus.UNDER_REVIEW, Role.ANALYST)


def cmd_sendback(ws, args):
    return _do_transition(ws, args, VersionStatus.DRAFT, Role(args.role))


def cmd_issue(ws, args):
    return _do_transition(ws, args, VersionStatus.ISSUED, Role(args.role))


def cmd_reopen(ws, args) -> int:
    start, end = _parse_month(args.month)
    try:
        v = ws.ledger.reopen(args.zone, start, args.actor, Role(args.role),
                             args.reason, end=end)
    except LedgerError as exc:
        print(f"重开被拒绝：{exc}", file=sys.stderr)
        return 3
    ws.save()
    _print(v.to_dict())
    return 0


def cmd_late_reading(ws, args) -> int:
    start, end = _parse_month(args.month)
    real = Reading(key=args.key, meter_key=args.meter,
                   read_on=_parse_date(args.date), value=float(args.value),
                   estimated=False, source="late")
    try:
        adj = ws.ledger.post_late_reading(real, start, end,
                                          actor=args.actor, reason=args.reason or "")
    except (LedgerError, IdempotencyConflict) as exc:
        print(f"迟到读数登记失败：{exc}", file=sys.stderr)
        return 3
    ws.save()
    _print({"adjustment": to_jsonable(adj)})
    return 0


def cmd_verify(ws, args) -> int:
    start, end = _parse_month(args.month)
    identity_checks = []
    for (zone, win_start), series in ws.ledger.versions.items():
        if win_start != start:
            continue
        latest = series[-1]
        errs = verify_identity(latest.snapshot)
        identity_checks.append({"zone": zone, "version_id": latest.version_id,
                                "identity_ok": not errs, "errors": errs})
    _print({"month": args.month,
            "identity_checks": identity_checks,
            "transfer_pair_errors": verify_transfer_pairs(ws.store, start, end)})
    return 0


def cmd_trace(ws, args) -> int:
    start, _ = _parse_month(args.month)
    try:
        report = ws.ledger.trace_impacts(args.zone, start)
    except LedgerError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    _print(report)
    return 0


def cmd_demo(ws: Workspace, args) -> int:
    """端到端冒烟：构造 DMA-7 的 2026-09 场景并走完全部状态。"""
    from .models import Adjustment, Meter, MeterReplacement, Reading, Transfer, WaterEvent
    from datetime import date as _date
    s = ws.store
    s.add_meter(Meter("MB-7", "bulk", "DMA-7", _date(2025, 1, 1)))
    s.add_meter(Meter("MC-7a", "customer", "DMA-7", _date(2025, 1, 1),
                      _date(2026, 9, 15)))
    s.add_meter(Meter("MC-7b", "customer", "DMA-7", _date(2026, 9, 15)))
    s.add_replacement(MeterReplacement(
        "RP-1", "MC-7a", "MC-7b", _date(2026, 9, 15), 8400.0, 0.0))
    # 总表 9 月用量 12400；旧表段 5040，新表段 3840
    s.add_reading(Reading("MB-7:0831", "MB-7", _date(2026, 8, 31), 10000.0))
    s.add_reading(Reading("MB-7:0930", "MB-7", _date(2026, 9, 30), 22400.0))
    s.add_reading(Reading("MC-7a:0831", "MC-7a", _date(2026, 8, 31), 3000.0))
    s.add_reading(Reading("MC-7b:0930", "MC-7b", _date(2026, 9, 30), 3600.0))
    # MC-7c：月末先按估算读数 800 出账，真实读数 760 迟到
    s.add_meter(Meter("MC-7c", "customer", "DMA-7", _date(2025, 1, 1)))
    s.add_reading(Reading("MC-7c:0831", "MC-7c", _date(2026, 8, 31), 0.0))
    s.add_reading(Reading("MC-7c:0930-est", "MC-7c", _date(2026, 9, 30),
                          800.0, estimated=True, source="estimate"))
    s.add_transfer(Transfer("T-1", "DMA-8", "DMA-7", _date(2026, 9, 10),
                            600.0, _date(2026, 9, 10)))
    # 对侧分区 DMA-8 的主数据（调水成对，两端均须在册）
    s.add_meter(Meter("MB-8", "bulk", "DMA-8", _date(2025, 1, 1)))
    s.add_event(WaterEvent("F-1", "DMA-7", "fire", _date(2026, 9, 20),
                           150.0, _date(2026, 9, 20)))
    start, end = _parse_month("2026-09")

    v1 = ws.ledger.recompute("DMA-7", start, end, actor="张工")
    ws.ledger.submit("DMA-7", start, "张工", "9月初稿")
    ws.ledger.issue("DMA-7", start, "李复核", Role.REVIEWER, "复核无误")

    # 迟到真实读数 760 取代估算 800：自动生成 -40 的调整单，旧版不动
    adj = ws.ledger.post_late_reading(
        Reading("MC-7c:0930-real", "MC-7c", _date(2026, 9, 30), 760.0,
                source="late"),
        start, end, actor="张工", reason="用户实抄到达")
    v2 = ws.ledger.reopen("DMA-7", start, "赵管理员", Role.ADMIN,
                          "实抄修正", end=end)
    ws.save()
    _print({
        "issued_r1": {"version_id": v1.version_id,
                      "status": v1.status.value,
                      "fingerprint": v1.snapshot.fingerprint,
                      "loss_rate": v1.snapshot.loss_rate},
        "adjustment": to_jsonable(adj),
        "reopened_r2": v2.to_dict(),
        "trace": ws.ledger.trace_impacts("DMA-7", start),
        "verify": {"identity": verify_identity(v2.snapshot),
                   "transfer_pairs": verify_transfer_pairs(ws.store, start, end)},
    })
    return 0


def cmd_serve(ws, args) -> int:
    from .api import build_server
    server = build_server(ws.db_path, ws.versions_path, args.port)
    print(f"API 监听 http://127.0.0.1:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="water-balance",
                                description="分区水量平衡与漏损核算")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--versions", default=DEFAULT_VERSIONS)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("import", help="幂等导入数据包 JSON")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("recompute", help="重算指定历史月份")
    sp.add_argument("zone")
    sp.add_argument("month", help="YYYY-MM")
    sp.add_argument("--actor", default="cli")
    sp.set_defaults(func=cmd_recompute)

    sp = sub.add_parser("show", help="查看版本（默认最新修订）")
    sp.add_argument("zone")
    sp.add_argument("month")
    sp.add_argument("--revision", default=None)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("submit", help="提交复核（核算员）")
    sp.add_argument("zone"); sp.add_argument("month")
    sp.add_argument("--actor", required=True); sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_submit)

    sp = sub.add_parser("sendback", help="退回草稿（复核人/管理员）")
    sp.add_argument("zone"); sp.add_argument("month")
    sp.add_argument("--actor", required=True)
    sp.add_argument("--role", choices=[r.value for r in Role], default="reviewer")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_sendback)

    sp = sub.add_parser("issue", help="签发（复核人/签发人）")
    sp.add_argument("zone"); sp.add_argument("month")
    sp.add_argument("--actor", required=True)
    sp.add_argument("--role", choices=[r.value for r in Role], default="reviewer")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_issue)

    sp = sub.add_parser("reopen", help="管理员重开已签发版本")
    sp.add_argument("zone"); sp.add_argument("month")
    sp.add_argument("--actor", required=True)
    sp.add_argument("--role", choices=[r.value for r in Role], default="admin")
    sp.add_argument("--reason", required=True)
    sp.set_defaults(func=cmd_reopen)

    sp = sub.add_parser("late-reading", help="登记迟到真实读数并生成调整单")
    sp.add_argument("zone"); sp.add_argument("month")
    sp.add_argument("--key", required=True); sp.add_argument("--meter", required=True)
    sp.add_argument("--date", required=True, help="YYYY-MM-DD")
    sp.add_argument("--value", required=True)
    sp.add_argument("--actor", required=True); sp.add_argument("--reason", default="")
    sp.set_defaults(func=cmd_late_reading)

    sp = sub.add_parser("verify", help="核对恒等式与调水成对")
    sp.add_argument("month")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("trace", help="追踪修正对漏损指标的影响")
    sp.add_argument("zone"); sp.add_argument("month")
    sp.set_defaults(func=cmd_trace)

    sp = sub.add_parser("serve", help="启动 HTTP API")
    sp.add_argument("--port", type=int, default=8080)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("demo", help="端到端冒烟场景（会写入指定 --db）")
    sp.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ws = Workspace(args.db, args.versions)
    return args.func(ws, args)


if __name__ == "__main__":
    raise SystemExit(main())
