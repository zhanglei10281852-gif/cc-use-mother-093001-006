"""HTTP API（标准库实现，无第三方依赖）。

路由：
  POST /import                                 幂等导入数据包
  POST /zones/{zone}/months/{YYYY-MM}/recompute
  GET  /zones/{zone}/months/{YYYY-MM}/versions[/{revision}]
  POST /zones/{zone}/months/{YYYY-MM}/transition
  POST /zones/{zone}/months/{YYYY-MM}/reopen
  POST /zones/{zone}/months/{YYYY-MM}/late-reading
  GET  /zones/{zone}/months/{YYYY-MM}/trace
  GET  /months/{YYYY-MM}/verify

写操作需在 JSON 体中提供 actor/role；每次写后持久化。
"""
from __future__ import annotations

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .engine import verify_identity, verify_transfer_pairs
from .ledger import Ledger, LedgerError, month_window
from .models import Reading
from .snapshots import Role, VersionStatus, to_jsonable
from .store import IdempotencyConflict, Store, ingest_dict


class _Service:
    def __init__(self, db_path: str, versions_path: str) -> None:
        self.db_path = db_path
        self.versions_path = versions_path
        self.store = Store.load(db_path) if Path(db_path).exists() else Store()
        self.ledger = Ledger(self.store)
        self.ledger.load_versions(versions_path)
        self.lock = threading.Lock()

    def save(self) -> None:
        self.store.save(self.db_path)
        self.ledger.save_versions(self.versions_path)


def _month(seg: str):
    year, month = seg.split("-")
    return month_window(int(year), int(month))


def build_server(db_path: str, versions_path: str, port: int = 8080) -> ThreadingHTTPServer:
    service = _Service(db_path, versions_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "WaterBalance/1.0"

        def log_message(self, fmt, *args):  # 静默，保持 CLI 输出干净
            pass

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        # GET ----------------------------------------------------------
        def do_GET(self) -> None:
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                if (len(parts) == 5 and parts[0] == "zones"
                        and parts[2] == "months" and parts[4] == "versions"):
                    start, _ = _month(parts[3])
                    rows = [v.to_dict()
                            for v in service.ledger.list_versions(parts[1], start)]
                    return self._send(200, {"versions": rows})
                if (len(parts) == 6 and parts[0] == "zones"
                        and parts[2] == "months" and parts[4] == "versions"):
                    start, _ = _month(parts[3])
                    v = service.ledger.get(parts[1], start, int(parts[5]))
                    return self._send(200, v.to_dict())
                if (len(parts) == 5 and parts[0] == "zones"
                        and parts[2] == "months" and parts[4] == "trace"):
                    start, _ = _month(parts[3])
                    return self._send(200, service.ledger.trace_impacts(parts[1], start))
                if len(parts) == 3 and parts[0] == "months" and parts[2] == "verify":
                    start, end = _month(parts[1])
                    checks = []
                    for (zone, win_start), series in service.ledger.versions.items():
                        if win_start != start:
                            continue
                        latest = series[-1]
                        errs = verify_identity(latest.snapshot)
                        checks.append({"zone": zone, "version_id": latest.version_id,
                                       "identity_ok": not errs, "errors": errs})
                    return self._send(200, {
                        "month": parts[1],
                        "identity_checks": checks,
                        "transfer_pair_errors": verify_transfer_pairs(
                            service.store, start, end),
                    })
            except (LedgerError, ValueError) as exc:
                return self._send(404, {"error": str(exc)})
            self._send(404, {"error": "未知路由"})

        # POST ---------------------------------------------------------
        def do_POST(self) -> None:
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                body = self._body()
                with service.lock:
                    return self._route_post(parts, body)
            except IdempotencyConflict as exc:
                return self._send(409, {"error": f"幂等冲突：{exc}"})
            except LedgerError as exc:
                return self._send(409, {"error": str(exc)})
            except (ValueError, KeyError) as exc:
                return self._send(400, {"error": str(exc)})

        def _route_post(self, parts, body) -> None:
            if parts == ["import"]:
                counts = ingest_dict(service.store, body)
                service.save()
                return self._send(200, {"imported": counts})

            if (len(parts) == 5 and parts[0] == "zones"
                    and parts[2] == "months"):
                zone, month_seg = parts[1], parts[3]
                start, end = _month(month_seg)
                action = parts[4]
                if action == "recompute":
                    v = service.ledger.recompute(zone, start, end,
                                                 actor=body.get("actor", "api"))
                    service.save()
                    return self._send(200, v.to_dict())
                if action == "transition":
                    v = service.ledger.transition(
                        zone, start, VersionStatus(body["to_status"]),
                        body["actor"], Role(body["role"]), body.get("note", ""))
                    service.save()
                    return self._send(200, v.to_dict())
                if action == "reopen":
                    v = service.ledger.reopen(
                        zone, start, body["actor"], Role(body["role"]),
                        body["reason"], end=end)
                    service.save()
                    return self._send(200, v.to_dict())
                if action == "late-reading":
                    real = Reading(key=body["key"], meter_key=body["meter_key"],
                                   read_on=date.fromisoformat(body["read_on"]),
                                   value=float(body["value"]),
                                   estimated=False, source="late")
                    adj = service.ledger.post_late_reading(
                        real, start, end, body["actor"],
                        body.get("reason", ""),
                        date.fromisoformat(body["created_on"])
                        if body.get("created_on") else None)
                    service.save()
                    return self._send(200, {
                        "adjustment": to_jsonable(adj)})
            self._send(404, {"error": "未知路由"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
