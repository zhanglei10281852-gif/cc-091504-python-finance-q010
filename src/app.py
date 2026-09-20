"""HTTP 适配层（标准库）。

路由 -> :class:`compliance.service.ComplianceService` 方法的薄封装；
所有响应保持 JSON，持久化事件日志默认写入 ``.runtime/events.jsonl``
（可用环境变量 ``COMPLIANCE_STORE`` 覆盖路径）。
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from compliance.errors import ComplianceError
from compliance.service import ComplianceService
from compliance.store import EventStore

SERVICE_NAME = "限售股减持合规服务"

_STORE_LOCK = threading.Lock()
_SERVICE: ComplianceService | None = None


def _store_path() -> Path:
    env = os.getenv("COMPLIANCE_STORE")
    if env:
        return Path(env)
    return Path(os.getenv("RUNTIME_DIR", ".runtime")) / "events.jsonl"


def get_service() -> ComplianceService:
    global _SERVICE
    with _STORE_LOCK:
        if _SERVICE is None:
            _SERVICE = ComplianceService(EventStore(str(_store_path())))
        return _SERVICE


def reset_service_for_tests(store: EventStore | None = None) -> ComplianceService:
    """测试钩子：替换进程内单例。"""

    global _SERVICE
    with _STORE_LOCK:
        _SERVICE = ComplianceService(store or EventStore(None))
        return _SERVICE


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


# --------------------------------------------------------------------- 路由表

_POST_ROUTES = {
    "/shareholders": "register_shareholder",
    "/relationships": "declare_relationship",
    "/relationships/end": "end_relationship",
    "/batches": "record_batch",
    "/unlocks": "record_unlock",
    "/windows": "declare_window",
    "/rules": "publish_rule",
    "/plans": "create_plan",
    "/trades/withdraw": "withdraw_trade",
    "/trades/replace": "replace_trade",
}

# /plans/{id}/{action}
_PLAN_ACTIONS = {
    "edit": "edit_plan",
    "submit": "submit_plan",
    "approve": "approve_plan",
    "reject": "reject_plan",
    "withdraw": "withdraw_plan",
    "suspend": "suspend_plan",
    "resume": "resume_plan",
    "terminate": "terminate_plan",
    "trades": "report_trade",
    "evaluate": "evaluate",
}


def _plan_subroute(parts: list[str]) -> tuple[str, dict] | None:
    """解析 /plans/... 子路径，返回 (服务方法名, 路径参数)。"""

    if len(parts) < 3:
        return None
    plan_id, head = parts[1], parts[2]
    inject: dict = {"plan_id": plan_id}
    if head == "changes":
        if len(parts) == 3:
            return "submit_plan_change", inject
        if len(parts) == 4 and parts[3] in {"approve", "reject"}:
            return (
                "approve_plan_change" if parts[3] == "approve"
                else "reject_plan_change"
            ), inject
        return None
    if head == "announcements":
        # /plans/{id}/announcements/{announcement_id}/confirm
        if len(parts) == 5 and parts[4] == "confirm":
            inject["announcement_id"] = parts[3]
            return "confirm_announcement", inject
        return None
    if len(parts) == 3 and head in _PLAN_ACTIONS:
        return _PLAN_ACTIONS[head], inject
    return None


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ComplianceHTTP/1.0"

    # ------------------------------------------------------------ 基础响应

    def _send_json(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComplianceError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ComplianceError("请求体必须是 JSON 对象")
        return data

    def log_message(self, format: str, *args: object) -> None:
        return

    # ------------------------------------------------------------ GET

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/health":
                self._send_json(200, health_payload())
                return
            if path == "/plans":
                self._send_json(200, {"plans": get_service().list_plans()})
                return
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] == "plans":
                qs = parse_qs(parsed.query)
                as_of = qs.get("as_of", [None])[0]
                self._send_json(200, get_service().get_plan(parts[1], as_of=as_of))
                return
            self._send_json(404, {"error": {"code": "not_found", "message": path}})
        except ComplianceError as exc:
            self._send_json(exc.http_status, exc.to_payload())
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": {"code": "internal", "message": str(exc)}})

    # ------------------------------------------------------------ POST

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            service = get_service()
            body = self._read_body()
            parts = [p for p in path.split("/") if p]

            method_name = _POST_ROUTES.get(path)
            inject: dict = {}
            if method_name is None and parts and parts[0] == "plans":
                routed = _plan_subroute(parts)
                if routed is not None:
                    method_name, inject = routed

            if method_name is None:
                self._send_json(
                    404, {"error": {"code": "not_found", "message": path}}
                )
                return

            result = getattr(service, method_name)({**inject, **body})
            self._send_json(200, result if isinstance(result, dict) else {"result": result})
        except ComplianceError as exc:
            self._send_json(exc.http_status, exc.to_payload())
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": {"code": "internal", "message": str(exc)}})


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), RequestHandler)
