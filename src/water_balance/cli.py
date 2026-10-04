"""命令行入口。

示例::

    python -m water_balance.cli --db demo.json bootstrap
    python -m water_balance.cli --db demo.json recompute DMA-7 2026-09 --as op1
    python -m water_balance.cli --db demo.json issue VB-000001 --as iss1
    python -m water_balance.cli --db demo.json verify 2026-09
    python -m water_balance.cli --db demo.json serve --port 8080
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .contracts import BalanceError
from .engine import identity_residual
from .service import WaterBalanceService
from .storage import Repository


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2,
                     default=_json_default))


def _ok(message: str) -> None:
    print(f"✓ {message}")


class Cli:
    def __init__(self, db_path: str):
        self.svc = WaterBalanceService(Repository(db_path))

    # ---------------------------------------------------------- 基础

    def cmd_user(self, a: argparse.Namespace) -> None:
        _print(self.svc.create_user(a.as_, a.user_id, a.name, a.roles))

    def cmd_zone(self, a: argparse.Namespace) -> None:
        _print(self.svc.create_zone(a.as_, a.zone_id, a.name))

    def cmd_sp(self, a: argparse.Namespace) -> None:
        _print(self.svc.register_service_point(
            a.as_, a.sp_id, a.zone_id, a.valid_from, a.valid_to, a.label or ""))

    def cmd_sp_close(self, a: argparse.Namespace) -> None:
        _print(self.svc.close_service_point(a.as_, a.sp_id, a.valid_to))

    def cmd_meter(self, a: argparse.Namespace) -> None:
        _print(self.svc.install_meter(
            a.as_, a.meter_id, a.sp_id, a.role, a.valid_from,
            a.valid_to, a.kind or ""))

    def cmd_replace(self, a: argparse.Namespace) -> None:
        _print(self.svc.replace_meter(
            a.as_, a.sp_id, a.new_meter_id, a.on_date, a.kind or ""))

    def cmd_reading(self, a: argparse.Namespace) -> None:
        _print(self.svc.add_reading(
            a.as_, a.meter_id, a.read_on, a.value))

    def cmd_usage(self, a: argparse.Namespace) -> None:
        _print(self.svc.add_usage(
            a.as_, a.zone_id, a.category, a.period_start,
            a.period_end, a.daily_m3))

    def cmd_rule(self, a: argparse.Namespace) -> None:
        _print(self.svc.register_rule(
            a.as_, a.rule_id, a.method, a.factor, a.min_coverage,
            a.valid_from, a.valid_to, a.description or ""))

    def cmd_approve_rule(self, a: argparse.Namespace) -> None:
        _print(self.svc.approve_rule(a.as_, a.rule_id))

    def cmd_transfer(self, a: argparse.Namespace) -> None:
        _print(self.svc.create_transfer(
            a.as_, a.from_zone, a.to_zone, a.date, a.volume))

    def cmd_import(self, a: argparse.Namespace) -> None:
        records = json.loads(Path(a.file).read_text(encoding="utf-8"))
        _print(self.svc.import_batch(a.as_, a.source_ref, records))

    # ---------------------------------------------------------- 核算

    def cmd_preview(self, a: argparse.Namespace) -> None:
        _print(self.svc.preview_balance(a.zone, a.month))

    def cmd_recompute(self, a: argparse.Namespace) -> None:
        v = self.svc.recompute(a.as_, a.zone, a.month, a.note or "")
        _print({"version_id": v["version_id"], "zone_id": v["zone_id"],
                "month": v["month"], "rev": v["rev"], "state": v["state"],
                "indicators": v["result"]["indicators"],
                "input_digest": v["input_digest"][:16],
                "variance": v["variance"] if v["variance"].get("items") else None,
                "adjustments": v["adjustment_ids"]})

    def cmd_review(self, a: argparse.Namespace) -> None:
        _print(self.svc.review_version(a.as_, a.version, a.comment or ""))

    def cmd_issue(self, a: argparse.Namespace) -> None:
        overrides = None
        if a.reasons:
            overrides = json.loads(Path(a.reasons).read_text(encoding="utf-8"))
        v = self.svc.issue_version(a.as_, a.version, overrides)
        _print({"version_id": v["version_id"], "state": v["state"],
                "issued_by": v["issued_by"], "issued_at": v["issued_at"],
                "variance_reasons": v.get("variance_reasons")})

    def cmd_reopen(self, a: argparse.Namespace) -> None:
        _print(self.svc.reopen_version(a.as_, a.version, a.reason))

    def cmd_versions(self, a: argparse.Namespace) -> None:
        _print(self.svc.list_versions(a.zone, a.month))

    def cmd_version_show(self, a: argparse.Namespace) -> None:
        _print(self.svc.get_version(a.version))

    def cmd_adjustments(self, a: argparse.Namespace) -> None:
        _print(self.svc.list_adjustments(a.zone, a.month, a.status))

    def cmd_verify(self, a: argparse.Namespace) -> None:
        result = self.svc.verify(a.month, a.zones.split(",") if a.zones else None)
        _print(result)
        if not result["identity_ok"]:
            sys.exit(1)

    # ---------------------------------------------------------- 示例数据

    def cmd_bootstrap(self, a: argparse.Namespace) -> None:
        s = self.svc
        s.create_user("__bootstrap__", "admin1", "管理员丁", ["admin"])
        s.create_user("admin1", "op1", "抄表员甲", ["operator"])
        s.create_user("admin1", "rev1", "复核员乙", ["reviewer"])
        s.create_user("admin1", "iss1", "签发人丙", ["issuer"])
        s.create_zone("op1", "DMA-7", "第七分区")
        s.create_zone("op1", "DMA-3", "第三分区")
        # 供水点与表计（2026-09 结算窗口）
        s.register_service_point("op1", "SP-M", "DMA-7",
                                 "2026-01-01", label="分区总表点")
        s.install_meter("op1", "M-MASTER", "SP-M", "master", "2026-01-01")
        s.register_service_point("op1", "SP-A", "DMA-7",
                                 "2026-01-01", label="用户A")
        s.install_meter("op1", "M-A", "SP-A", "user", "2026-01-01")
        s.register_service_point("op1", "SP-B", "DMA-7",
                                 "2026-01-01", label="用户B")
        s.install_meter("op1", "M-B1", "SP-B", "user", "2026-01-01",
                        meter_kind="机械表")
        # 读数：8 月起度 + 9 月底抄见（B 当月缺数，待估算）
        s.add_reading("op1", "M-MASTER", "2026-09-01", 10000.0)
        s.add_reading("op1", "M-MASTER", "2026-10-01", 11250.0)
        s.add_reading("op1", "M-A", "2026-09-01", 2000.0)
        s.add_reading("op1", "M-A", "2026-10-01", 2900.0)
        s.add_reading("op1", "M-B1", "2026-09-01", 500.0)
        # B 9 月 21 日才有抄见（覆盖 20/30 天），剩余 10 天按规则估算；
        # 10 月初的真实读数迟到后将以调整单形式替换估算。
        s.add_reading("op1", "M-B1", "2026-09-21", 630.0)
        # 消防用水 9 月 10 日演练 20 m3
        s.add_usage("op1", "DMA-7", "fire_water",
                    "2026-09-10", "2026-09-11", 20.0)
        # 估算规则（需审批后方可使用）
        s.register_rule("op1", "RULE-AVG", "actual_average", 1.0, 0.5,
                        "2026-01-01", description="按同窗口实际日均用量估算")
        s.approve_rule("iss1", "RULE-AVG")
        # 跨分区调水（成对入账）
        s.create_transfer("op1", "DMA-3", "DMA-7", "2026-09-15", 100.0)
        _ok("示例数据已写入（含 DMA-7/DMA-3、总表、用户A、待换表用户B、"
            "消防用水、已审批估算规则与跨分区调水）")

    def cmd_serve(self, a: argparse.Namespace) -> None:
        from .api import main as serve_main
        serve_main(["--db", str(self.svc.repo.path),
                    "--host", a.host, "--port", str(a.port)])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="water_balance",
                                description="分区水量平衡与漏损核算平台")
    p.add_argument("--db", default="water_balance.json", help="数据文件路径")
    sub = p.add_subparsers(dest="command", required=True)

    def add_as(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--as", dest="as_", required=True, help="操作人 ID")

    sp = sub.add_parser("user", help="创建用户"); add_as(sp)
    sp.add_argument("user_id"); sp.add_argument("name")
    sp.add_argument("roles", nargs="+",
                    choices=["operator", "reviewer", "issuer", "admin"])
    sp.set_defaults(func=Cli.cmd_user)

    sp = sub.add_parser("zone", help="创建分区"); add_as(sp)
    sp.add_argument("zone_id"); sp.add_argument("name")
    sp.set_defaults(func=Cli.cmd_zone)

    sp = sub.add_parser("sp", help="登记供水点（有效期）"); add_as(sp)
    sp.add_argument("sp_id"); sp.add_argument("zone_id")
    sp.add_argument("valid_from"); sp.add_argument("valid_to", nargs="?",
                                                   default=None)
    sp.add_argument("--label", default="")
    sp.set_defaults(func=Cli.cmd_sp)

    sp = sub.add_parser("sp-close", help="停用供水点"); add_as(sp)
    sp.add_argument("sp_id"); sp.add_argument("valid_to")
    sp.set_defaults(func=Cli.cmd_sp_close)

    sp = sub.add_parser("meter", help="安装表计（有效期，不允许重叠）"); add_as(sp)
    sp.add_argument("meter_id"); sp.add_argument("sp_id")
    sp.add_argument("role", choices=["master", "user"])
    sp.add_argument("valid_from"); sp.add_argument("valid_to", nargs="?",
                                                   default=None)
    sp.add_argument("--kind", default="")
    sp.set_defaults(func=Cli.cmd_meter)

    sp = sub.add_parser("replace-meter", help="换表：旧表停用归零，新表同角色启用")
    add_as(sp)
    sp.add_argument("sp_id"); sp.add_argument("new_meter_id")
    sp.add_argument("on_date"); sp.add_argument("--kind", default="")
    sp.set_defaults(func=Cli.cmd_replace)

    sp = sub.add_parser("reading", help="录入读数（同值幂等，异值拒绝改账）")
    add_as(sp)
    sp.add_argument("meter_id"); sp.add_argument("read_on")
    sp.add_argument("value", type=float)
    sp.set_defaults(func=Cli.cmd_reading)

    sp = sub.add_parser("usage", help="登记消防/合法未计量用水（按日水量×天数）")
    add_as(sp)
    sp.add_argument("zone_id")
    sp.add_argument("category",
                    choices=["fire_water", "authorized_unmetered"])
    sp.add_argument("period_start"); sp.add_argument("period_end")
    sp.add_argument("daily_m3", type=float)
    sp.set_defaults(func=Cli.cmd_usage)

    sp = sub.add_parser("rule", help="登记估算规则（需审批后生效）"); add_as(sp)
    sp.add_argument("rule_id"); sp.add_argument("method")
    sp.add_argument("factor", type=float)
    sp.add_argument("min_coverage", type=float)
    sp.add_argument("valid_from"); sp.add_argument("valid_to", nargs="?",
                                                   default=None)
    sp.add_argument("--description", default="")
    sp.set_defaults(func=Cli.cmd_rule)

    sp = sub.add_parser("approve-rule", help="审批估算规则"); add_as(sp)
    sp.add_argument("rule_id"); sp.set_defaults(func=Cli.cmd_approve_rule)

    sp = sub.add_parser("transfer", help="登记跨分区调水（成对入账）"); add_as(sp)
    sp.add_argument("from_zone"); sp.add_argument("to_zone")
    sp.add_argument("date"); sp.add_argument("volume", type=float)
    sp.set_defaults(func=Cli.cmd_transfer)

    sp = sub.add_parser("import", help="批量导入 JSON 文件（内容哈希幂等）")
    add_as(sp)
    sp.add_argument("source_ref"); sp.add_argument("file")
    sp.set_defaults(func=Cli.cmd_import)

    sp = sub.add_parser("preview", help="只读试算，不落账")
    sp.add_argument("zone"); sp.add_argument("month")
    sp.set_defaults(func=Cli.cmd_preview)

    sp = sub.add_parser("recompute", help="重算并生成/推进复核版本"); add_as(sp)
    sp.add_argument("zone"); sp.add_argument("month")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=Cli.cmd_recompute)

    sp = sub.add_parser("review", help="复核版本"); add_as(sp)
    sp.add_argument("version"); sp.add_argument("--comment", default="")
    sp.set_defaults(func=Cli.cmd_review)

    sp = sub.add_parser("issue", help="签发版本（须先复核；差异原因随版本保存）")
    add_as(sp)
    sp.add_argument("version")
    sp.add_argument("--reasons", help="差异原因 JSON 文件：{调整单ID: 原因}")
    sp.set_defaults(func=Cli.cmd_issue)

    sp = sub.add_parser("reopen", help="管理员重开已签发版本"); add_as(sp)
    sp.add_argument("version"); sp.add_argument("reason")
    sp.set_defaults(func=Cli.cmd_reopen)

    sp = sub.add_parser("versions", help="列出平衡版本")
    sp.add_argument("--zone"); sp.add_argument("--month")
    sp.set_defaults(func=Cli.cmd_versions)

    sp = sub.add_parser("version", help="查看版本完整快照（含输入摘要/差异）")
    sp.add_argument("version"); sp.set_defaults(func=Cli.cmd_version_show)

    sp = sub.add_parser("adjustments", help="列出调整单")
    sp.add_argument("--zone"); sp.add_argument("--month")
    sp.add_argument("--status", choices=["open", "applied"])
    sp.set_defaults(func=Cli.cmd_adjustments)

    sp = sub.add_parser("verify", help="重算任意月份并核对恒等式/调水守恒")
    sp.add_argument("month"); sp.add_argument("--zones",
                                              help="逗号分隔，默认全部")
    sp.set_defaults(func=Cli.cmd_verify)

    sp = sub.add_parser("bootstrap", help="写入演示数据"); add_as(sp)
    sp.set_defaults(func=Cli.cmd_bootstrap)

    sp = sub.add_parser("serve", help="启动 HTTP API")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)
    sp.set_defaults(func=Cli.cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cli = Cli(args.db)
    try:
        args.func(cli, args)
    except BalanceError as exc:
        print(f"错误[{exc.code}] {exc}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False, indent=2),
                  file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError) as exc:
        print(f"错误 {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
