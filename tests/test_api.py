"""HTTP 路由契约测试：在随机端口启动标准库服务，走真实套接字。"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.client import IncompleteRead
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import app
from compliance.store import EventStore


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app.reset_service_for_tests(EventStore(None))
        cls.server = app.create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method: str, path: str, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except (IncompleteRead, json.JSONDecodeError):
                body = None
            return exc.code, body

    def test_full_lifecycle_over_http(self):
        s, b = self.call("GET", "/health")
        self.assertEqual(s, 200)
        self.assertEqual(b["status"], "ok")

        def ok(method, path, payload=None):
            status, body = self.call(method, path, payload)
            self.assertEqual(status, 200, body)
            return body

        ok("POST", "/shareholders", {
            "shareholder_id": "C", "name": "实控人", "kind": "natural_person",
            "is_controlling_person": True,
        })
        ok("POST", "/batches", {
            "batch_id": "b1", "shareholder_id": "C", "security_code": "S1",
            "source": "IPO_lockup", "total_qty": 50_000_000,
        })
        ok("POST", "/unlocks", {
            "unlock_id": "u1", "batch_id": "b1", "condition_type": "date",
            "unlock_date": "2026-01-01", "tranche_qty": 50_000_000,
        })
        ok("POST", "/rules", {
            "version": "v1", "effective_from": "2026-01-01", "name": "r",
        })
        ok("POST", "/plans", {
            "plan_id": "P9", "group_owner_id": "C", "security_code": "S1",
            "share_capital": 100_000_000, "effective_from": "2026-05-01",
            "proposed_qty": 3_000_000, "channels": ["secondary"],
        })
        ok("POST", "/plans/P9/submit")
        ok("POST", "/plans/P9/approve", {"approved_date": "2026-04-20"})

        # 送审后普通编辑锁定的契约：409 + plan_locked 由另一新计划验证状态冲突
        status, detail = self.call("GET", "/plans/P9")
        self.assertEqual(status, 200)
        ann = next(
            a for a in detail["pending_disclosures"]
            if a["kind"] == "pre_disclosure"
        )
        # 四段式公告确认路由
        confirmed = ok(
            "POST", f"/plans/P9/announcements/{ann['announcement_id']}/confirm",
            {"confirmed_date": "2026-04-21"},
        )
        self.assertEqual(confirmed["status"], "confirmed")

        trade = ok("POST", "/plans/P9/trades", {
            "report_id": "T1", "shareholder_id": "C", "batch_id": "b1",
            "channel": "secondary", "trade_date": "2026-05-02", "qty": 100_000,
        })
        self.assertEqual(trade["evaluation"]["result"], "approved")

        # 重复回报 → 409
        status, body = self.call("POST", "/plans/P9/trades", {
            "report_id": "T1", "shareholder_id": "C", "batch_id": "b1",
            "channel": "secondary", "trade_date": "2026-05-02", "qty": 100_000,
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "state_conflict")

        # 列表与详情
        listing = ok("GET", "/plans")
        self.assertTrue(any(p["plan_id"] == "P9" for p in listing["plans"]))
        detail = ok("GET", "/plans/P9?as_of=2026-05-03")
        self.assertEqual(detail["trade_replay"]["counted_qty"], 100_000)
        self.assertTrue(detail["available_qty"]["layers"])
        self.assertTrue(detail["decisions"])

        # 未知路由与错误请求
        status, _ = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        status, body = self.call("POST", "/plans", {"plan_id": "BROKEN"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_payload")


if __name__ == "__main__":
    unittest.main()
