from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from compliance.engine import ComplianceEngine, load_enums
from compliance.models import DomainError, parse_ts

SERVICE_NAME = '限售股减持合规服务'

ROOT = Path(__file__).resolve().parents[1]


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


def build_engine(runtime_dir: str | Path | None = None) -> ComplianceEngine:
    from compliance.store import EventStore

    store = None
    if runtime_dir:
        store = EventStore(Path(runtime_dir) / "events.jsonl")
    return ComplianceEngine(store, load_enums(ROOT / "reference"))


# 路由表：(方法, 路径正则, 处理器)。处理器签名 (engine, match, body, query) -> (status, obj)
def _routes():
    def q_as_of(query: dict):
        values = query.get("as_of")
        return parse_ts(values[0]) if values else None

    return [
        ("GET", r"^/health$", lambda e, m, b, q: (200, health_payload())),
        ("GET", r"^/enums$", lambda e, m, b, q: (200, e.enums)),
        ("POST", r"^/company$", lambda e, m, b, q: (200, e.set_company(b))),
        ("GET", r"^/accounts$", lambda e, m, b, q: (200, e.list_accounts())),
        ("POST", r"^/accounts$", lambda e, m, b, q: (201, e.register_account(b))),
        ("POST", r"^/relationships$", lambda e, m, b, q: (201, e.register_relationship(b))),
        ("POST", r"^/relationships/(?P<rid>[^/]+)/end$",
         lambda e, m, b, q: (200, e.end_relationship(m.group("rid"), b))),
        ("POST", r"^/lots$", lambda e, m, b, q: (201, e.register_lot(b))),
        ("POST", r"^/lots/(?P<lid>[^/]+)/satisfy$",
         lambda e, m, b, q: (200, e.satisfy_lot_condition(m.group("lid"), b))),
        ("GET", r"^/rules$", lambda e, m, b, q: (200, e.list_rules())),
        ("POST", r"^/rules$", lambda e, m, b, q: (201, e.register_rule(b))),
        ("POST", r"^/sensitive-events$",
         lambda e, m, b, q: (201, e.register_sensitive_event(b))),
        ("POST", r"^/windows$", lambda e, m, b, q: (201, e.register_window(b))),
        ("GET", r"^/plans$", lambda e, m, b, q: (200, e.list_plans())),
        ("POST", r"^/plans$", lambda e, m, b, q: (201, e.create_plan(b))),
        ("GET", r"^/plans/(?P<pid>[^/]+)$",
         lambda e, m, b, q: (200, e.plan_view(m.group("pid"), q_as_of(q)))),
        ("GET", r"^/plans/(?P<pid>[^/]+)/decisions$",
         lambda e, m, b, q: (200, e.plan_decisions(m.group("pid")))),
        ("POST", r"^/plans/(?P<pid>[^/]+)/submit$",
         lambda e, m, b, q: (200, e.submit_plan(m.group("pid"), q_as_of(q) or _b_as_of(b)))),
        ("POST", r"^/plans/(?P<pid>[^/]+)/approve$",
         lambda e, m, b, q: (200, e.approve_plan(m.group("pid"), q_as_of(q) or _b_as_of(b)))),
        ("POST", r"^/plans/(?P<pid>[^/]+)/edit$",
         lambda e, m, b, q: (200, e.edit_plan(m.group("pid"), b))),
        ("POST", r"^/plans/(?P<pid>[^/]+)/suspend$",
         lambda e, m, b, q: (200, e.suspend_plan(m.group("pid"), b))),
        ("POST", r"^/plans/(?P<pid>[^/]+)/resume$",
         lambda e, m, b, q: (200, e.resume_plan(m.group("pid"), q_as_of(q) or _b_as_of(b)))),
        ("POST", r"^/plans/(?P<pid>[^/]+)/close$",
         lambda e, m, b, q: (200, e.close_plan(m.group("pid"), q_as_of(q) or _b_as_of(b)))),
        ("POST", r"^/plans/(?P<pid>[^/]+)/check$",
         lambda e, m, b, q: (200, e.check_execution(m.group("pid"), b))),
        ("POST", r"^/executions$", lambda e, m, b, q: (201, e.record_execution(b))),
        ("POST", r"^/executions/(?P<rid>[^/]+)/revoke$",
         lambda e, m, b, q: (200, e.revoke_execution(m.group("rid"), b))),
        ("GET", r"^/announcements$",
         lambda e, m, b, q: (200, e.list_announcements(
             (q.get("status") or [None])[0], q_as_of(q)))),
        ("POST", r"^/announcements/(?P<aid>[^/]+)/confirm$",
         lambda e, m, b, q: (200, e.confirm_announcement(m.group("aid"), b))),
        ("POST", r"^/admin/sweep$",
         lambda e, m, b, q: (200, e.sweep(q_as_of(q) or _b_as_of(b)))),
    ]


def _b_as_of(body: dict):
    return parse_ts(body["as_of"]) if body.get("as_of") else None


def make_handler(engine: ComplianceEngine):
    routes = [(method, re.compile(pattern), fn) for method, pattern, fn in _routes()]

    class RequestHandler(BaseHTTPRequestHandler):
        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            body: dict = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        self._json(400, {"error": {"code": "bad_json",
                                                   "message": "请求体不是合法 JSON"}})
                        return
                    if not isinstance(body, dict):
                        self._json(400, {"error": {"code": "bad_json",
                                                   "message": "请求体必须是 JSON 对象"}})
                        return
            for rmethod, pattern, fn in routes:
                if rmethod != method:
                    continue
                match = pattern.match(parsed.path)
                if not match:
                    continue
                try:
                    status, obj = fn(engine, match, body, query)
                except DomainError as exc:
                    self._json(exc.status, exc.to_dict())
                except (ValueError, TypeError) as exc:
                    self._json(400, {"error": {"code": "bad_request", "message": str(exc)}})
                except Exception as exc:  # noqa: BLE001 - 服务边界兜底
                    self._json(500, {"error": {"code": "internal", "message": str(exc)}})
                else:
                    self._json(status, obj)
                return
            self._json(404, {"error": {"code": "not_found",
                                       "message": f"{method} {parsed.path} 不存在"}})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def _json(self, status: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return RequestHandler


def create_server(host: str, port: int, runtime_dir: str | Path | None = None,
                  engine: ComplianceEngine | None = None) -> ThreadingHTTPServer:
    # runtime_dir 为 None 时使用纯内存事件存储（不落盘）
    engine = engine or build_engine(runtime_dir)
    return ThreadingHTTPServer((host, port), make_handler(engine))
