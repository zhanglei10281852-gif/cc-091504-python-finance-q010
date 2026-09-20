"""HTTP 层冒烟测试：JSON 路由、错误映射与关键流程。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import create_server


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server("127.0.0.1", 0, runtime_dir=None)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_flow_over_http(self):
        # 登记台账
        self.call("POST", "/company", {"total_shares": 100_000_000})
        status, _ = self.call("POST", "/accounts", {
            "account_id": "ctrl", "name": "实控人", "kind": "controller"})
        self.assertEqual(status, 201)
        self.call("POST", "/accounts", {
            "account_id": "esop", "name": "员工持股平台", "kind": "employee_platform"})
        self.call("POST", "/relationships", {
            "account_a": "ctrl", "account_b": "esop",
            "type": "employee_platform", "effective_from": "2026-01-01"})
        self.call("POST", "/rules", {
            "rule_id": "R90", "version": "v1", "type": "rolling_window_cap",
            "effective_from": "2026-01-01",
            "params": {"window_days": 90, "max_pct_of_total_shares": 1.0}})
        self.call("POST", "/lots", {
            "lot_id": "lot-1", "account_id": "ctrl", "source": "IPO_lockup",
            "quantity": 5_000_000, "unlock_date": "2026-01-01"})

        # 计划生命周期
        status, plan = self.call("POST", "/plans", {
            "plan_id": "p1", "account_id": "ctrl", "lot_ids": ["lot-1"],
            "planned_quantity": 1_000_000,
            "window_start": "2026-03-01", "window_end": "2026-06-01"})
        self.assertEqual(status, 201)
        self.assertEqual(plan["state"], "draft")
        status, _ = self.call("POST", "/plans/p1/submit?as_of=2026-02-20")
        self.assertEqual(status, 200)
        # 送审中编辑被拒
        status, err = self.call("POST", "/plans/p1/edit", {
            "planned_quantity": 900_000, "expected_revision": 1})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "under_review")
        self.call("POST", "/plans/p1/approve?as_of=2026-02-25")

        # 成交与重复回报幂等
        status, res = self.call("POST", "/executions", {
            "report_id": "r1", "plan_id": "p1", "quantity": 100_000,
            "trade_time": "2026-04-01T10:00:00+00:00"})
        self.assertEqual(status, 201)
        self.assertTrue(res["decision"]["allowed"])
        status, res = self.call("POST", "/executions", {
            "report_id": "r1", "plan_id": "p1", "quantity": 100_000,
            "trade_time": "2026-04-01T10:00:00+00:00"})
        self.assertEqual(status, 201)
        self.assertTrue(res["duplicate"])

        # 负责人视图
        status, view = self.call("GET", "/plans/p1?as_of=2026-04-02T00:00:00Z")
        self.assertEqual(status, 200)
        self.assertEqual(view["plan"]["state"], "active")
        self.assertEqual(view["reducible"]["final"], 900_000)
        self.assertTrue(view["decisions"])
        self.assertEqual(view["merge_group"]["members"], ["ctrl", "esop"])

        # 未知路径与未知计划
        status, _ = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        status, _ = self.call("GET", "/plans/ghost")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
