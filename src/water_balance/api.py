"""HTTP API：基于标准库 http.server，无第三方依赖。

鉴权使用 ``X-User-Id`` 头（演示/内网部署），角色与状态约束全部在
服务层强制执行。所有写操作均为 POST，读操作为 GET。
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .contracts import BalanceError
from .service import WaterBalanceService
from .storage import Repository

log = logging.getLogger("water_balance.api")


def _make_handler(service: WaterBalanceService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "WaterBalance/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

        # -------------------------------------------------- 框架

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=1).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise BalanceError(f"请求体不是合法 JSON: {exc}")
            if not isinstance(data, dict):
                raise BalanceError("请求体必须是 JSON 对象")
            return data

        def _actor(self, data: dict[str, Any]) -> str:
            actor = self.headers.get("X-User-Id") or data.pop("as_user", None)
            if not actor:
                raise BalanceError("缺少 X-User-Id 头")
            return actor

        def _error_status(self, exc: Exception) -> int:
            mapping = {"not_found": 404, "conflict": 409,
                       "permission_denied": 403,
                       "invalid_transition": 422, "missing_data": 422}
            code = getattr(exc, "code", "")
            return mapping.get(code, 400)

        # -------------------------------------------------- 路由

        def do_GET(self) -> None:  # noqa: N802
            try:
                parts = urlsplit(self.path)
                path = parts.path.strip("/").split("/")
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                self._route_get(path, query)
            except Exception as exc:  # noqa: BLE001
                self._send(self._error_status(exc),
                           {"error": type(exc).__name__,
                            "code": getattr(exc, "code", "error"),
                            "message": str(exc),
                            "details": getattr(exc, "details", None)})

        def do_POST(self) -> None:  # noqa: N802
            try:
                data = self._read_json()
                path = urlsplit(self.path).path.strip("/").split("/")
                # 首个用户自举：无用户时服务层允许创建，之后强制管理员
                actor = self.headers.get("X-User-Id") \
                    or data.pop("as_user", None) \
                    or ("__bootstrap__" if path == ["users"] else None)
                if not actor:
                    raise BalanceError("缺少 X-User-Id 头")
                self._route_post(path, actor, data)
            except Exception as exc:  # noqa: BLE001
                self._send(self._error_status(exc),
                           {"error": type(exc).__name__,
                            "code": getattr(exc, "code", "error"),
                            "message": str(exc),
                            "details": getattr(exc, "details", None)})

        def _route_get(self, path: list[str], q: dict[str, str]) -> None:
            s = service
            if path[:1] == ["health"]:
                self._send(200, {"status": "ok"})
            elif path[:1] == ["zones"] and len(path) == 3 and path[2] == "balance":
                if "month" not in q:
                    raise BalanceError("缺少 month 查询参数")
                self._send(200, s.preview_balance(path[1], q["month"]))
            elif path[:1] == ["versions"] and len(path) == 1:
                self._send(200, s.list_versions(q.get("zone"), q.get("month")))
            elif path[:1] == ["versions"] and len(path) == 2:
                self._send(200, s.get_version(path[1]))
            elif path[:1] == ["adjustments"]:
                self._send(200, s.list_adjustments(q.get("zone"),
                                                   q.get("month"),
                                                   q.get("status")))
            elif path[:1] == ["verify"]:
                if "month" not in q:
                    raise BalanceError("缺少 month 查询参数")
                zones = q["zones"].split(",") if q.get("zones") else None
                self._send(200, s.verify(q["month"], zones))
            else:
                self._send(404, {"error": "NotFound", "message": self.path})

        def _route_post(self, path: list[str], actor: str,
                        d: dict[str, Any]) -> None:
            s = service

            def need(*keys: str) -> list[Any]:
                missing = [k for k in keys if k not in d]
                if missing:
                    raise BalanceError(f"缺少字段: {', '.join(missing)}")
                return [d[k] for k in keys]

            routes: dict[str, Callable[[], Any]] = {
                "users": lambda: s.create_user(
                    actor, *need("user_id", "name", "roles")),
                "zones": lambda: s.create_zone(
                    actor, *need("zone_id", "name")),
                "service-points": lambda: s.register_service_point(
                    actor, d["sp_id"], d["zone_id"], d["valid_from"],
                    d.get("valid_to"), d.get("label", "")),
                "service-points/close": lambda: s.close_service_point(
                    actor, *need("sp_id", "valid_to")),
                "meters": lambda: s.install_meter(
                    actor, d["meter_id"], d["sp_id"], d["role"],
                    d["valid_from"], d.get("valid_to"), d.get("kind", "")),
                "meters/replace": lambda: s.replace_meter(
                    actor, *need("sp_id", "new_meter_id", "on_date"),
                    d.get("kind", "")),
                "readings": lambda: s.add_reading(
                    actor, d["meter_id"], d["read_on"], float(d["value"])),
                "usages": lambda: s.add_usage(
                    actor, d["zone_id"], d["category"],
                    d["period_start"], d["period_end"], float(d["daily_m3"])),
                "rules": lambda: s.register_rule(
                    actor, d["rule_id"], d["method"], float(d["factor"]),
                    float(d.get("min_coverage", 0.0)), d["valid_from"],
                    d.get("valid_to"), d.get("description", "")),
                "transfers": lambda: s.create_transfer(
                    actor, d["from_zone"], d["to_zone"], d["transfer_date"],
                    float(d["volume_m3"])),
                "imports": lambda: s.import_batch(
                    actor, d["source_ref"], d["records"]),
                "recompute": lambda: s.recompute(
                    actor, d["zone_id"], d["month"], d.get("note", "")),
            }
            key = "/".join(path)
            if key in routes:
                self._send(200, routes[key]())
                return
            if len(path) == 3 and path[0] == "rules" and path[2] == "approve":
                self._send(200, s.approve_rule(actor, path[1]))
            elif len(path) == 3 and path[0] == "versions":
                vid = path[1]
                action = path[2]
                if action == "review":
                    self._send(200, s.review_version(
                        actor, vid, d.get("comment", "")))
                elif action == "issue":
                    self._send(200, s.issue_version(
                        actor, vid, d.get("variance_reasons")))
                elif action == "reopen":
                    self._send(200, s.reopen_version(
                        actor, vid, d.get("reason", "")))
                else:
                    self._send(404, {"error": "NotFound", "message": key})
            else:
                self._send(404, {"error": "NotFound", "message": key})

    return Handler


def create_server(db_path: str, host: str = "127.0.0.1",
                  port: int = 8080) -> ThreadingHTTPServer:
    service = WaterBalanceService(Repository(db_path))
    return ThreadingHTTPServer((host, port), _make_handler(service))


def main(argv: list[str] | None = None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="分区水量平衡 API 服务")
    parser.add_argument("--db", default="water_balance.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    server = create_server(args.db, args.host, args.port)
    log.info("监听 http://%s:%s （库 %s）", args.host, args.port, args.db)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
