"""引擎核心场景测试：合并额度、关系时效、最严格规则、送审保护、
乱序/撤回重放、敏感窗口、披露阈值公告流程与可解释视图。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from compliance.engine import ComplianceEngine
from compliance.models import DomainError, parse_ts
from compliance.store import EventStore

T0 = "2026-01-01T00:00:00+00:00"


def make_engine() -> ComplianceEngine:
    """标准台账：实控人 + 员工持股平台 + 亲属合并组，董事账户，总股本 1 亿。

    规则：
    - R90: 任意 90 日合并减持 <= 总股本 1%（1,000,000 股）
    - R30: 任意 30 日合并减持 <= 500,000 股
    - SW : 董事在业绩预告公告日前 30 日至后 1 日禁止交易
    - DIS: 计划累计每达 300,000 股需 2 日内公告确认
    """
    e = ComplianceEngine()
    e.set_company({"total_shares": 100_000_000})
    e.register_account({"account_id": "ctrl", "name": "实控人", "kind": "controller"})
    e.register_account({"account_id": "esop", "name": "员工持股平台",
                        "kind": "employee_platform"})
    e.register_account({"account_id": "rel", "name": "亲属", "kind": "relative"})
    e.register_account({"account_id": "dir1", "name": "董事甲", "kind": "director",
                        "roles": ["director"]})
    e.register_relationship({"account_a": "ctrl", "account_b": "esop",
                             "type": "employee_platform", "effective_from": T0})
    e.register_relationship({"account_a": "ctrl", "account_b": "rel",
                             "type": "family", "effective_from": T0})
    e.register_rule({"rule_id": "R90", "version": "v1", "type": "rolling_window_cap",
                     "effective_from": T0,
                     "params": {"window_days": 90, "max_pct_of_total_shares": 1.0}})
    e.register_rule({"rule_id": "R30", "version": "v1", "type": "rolling_window_cap",
                     "effective_from": T0,
                     "params": {"window_days": 30, "max_quantity": 500_000}})
    e.register_rule({"rule_id": "SW", "version": "v1", "type": "sensitive_window",
                     "effective_from": T0,
                     "params": {"event_types": ["earnings_forecast"],
                                "applies_to_roles": ["director"],
                                "days_before": 30, "days_after": 1}})
    e.register_rule({"rule_id": "DIS", "version": "v1", "type": "disclosure_threshold",
                     "effective_from": T0,
                     "params": {"threshold_quantity": 300_000, "deadline_days": 2}})
    return e


def add_lot(e: ComplianceEngine, lot_id: str, account: str, qty: int,
            unlock: str = T0, conditions=None) -> None:
    e.register_lot({"lot_id": lot_id, "account_id": account, "source": "IPO_lockup",
                    "quantity": qty, "unlock_date": unlock,
                    "conditions": conditions or []})


def make_active_plan(e: ComplianceEngine, plan_id: str, account: str, lot_id: str,
                     qty: int, start="2026-03-01", end="2026-06-01") -> None:
    e.create_plan({"plan_id": plan_id, "account_id": account, "lot_ids": [lot_id],
                   "planned_quantity": qty,
                   "window_start": f"{start}T00:00:00+00:00",
                   "window_end": f"{end}T00:00:00+00:00"})
    e.submit_plan(plan_id, parse_ts("2026-02-20T00:00:00+00:00"))
    e.approve_plan(plan_id, parse_ts("2026-02-25T00:00:00+00:00"))


def exec_at(report_id: str, plan_id: str, qty: int, day: str) -> dict:
    return {"report_id": report_id, "plan_id": plan_id, "quantity": qty,
            "trade_time": f"{day}T10:00:00+00:00",
            "as_of": f"{day}T15:00:00+00:00"}


class MergedQuotaTest(unittest.TestCase):
    def setUp(self):
        self.e = make_engine()
        add_lot(self.e, "lot-c", "ctrl", 5_000_000)
        add_lot(self.e, "lot-e", "esop", 5_000_000)
        make_active_plan(self.e, "p-ctrl", "ctrl", "lot-c", 2_000_000)
        make_active_plan(self.e, "p-esop", "esop", "lot-e", 2_000_000)

    def test_related_accounts_share_quota(self):
        """实控人、员工持股平台、亲属合并计算：他人成交占用本人额度。"""
        self.e.record_execution(exec_at("r1", "p-ctrl", 200_000, "2026-04-01"))
        self.e.record_execution(exec_at("r2", "p-esop", 200_000, "2026-04-02"))

        view = self.e.plan_view("p-ctrl", parse_ts("2026-04-03T00:00:00+00:00"))
        self.assertEqual(view["merge_group"]["members"], ["ctrl", "esop", "rel"])
        # 30 日窗口消耗 = 本人 20 万 + 平台 20 万
        r30 = [c for c in view["contributions"] if c["rule_id"] == "R30"][0]
        self.assertEqual(r30["total"], 400_000)
        by = {c["account_id"]: c["quantity"] for c in r30["by_account"]}
        self.assertEqual(by, {"ctrl": 200_000, "esop": 200_000})

    def test_strictest_rule_binds(self):
        """多条数量规则同时限制时采用最严格结果，证据标明约束来源。"""
        self.e.record_execution(exec_at("r1", "p-ctrl", 200_000, "2026-04-01"))
        self.e.record_execution(exec_at("r2", "p-esop", 200_000, "2026-04-02"))
        # R30 剩余 10 万，R90 剩余 60 万 -> 最严格为 R30
        decision = self.e.check_execution("p-ctrl", {
            "quantity": 150_000, "trade_time": "2026-04-03T10:00:00+00:00",
            "as_of": "2026-04-03T10:00:00+00:00"})
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reducible_quantity"], 100_000)
        binding = [l for l in decision["layers"] if l.get("binding")]
        self.assertEqual([l["layer"] for l in binding], ["rule_cap"])
        self.assertEqual(binding[0]["rule_id"], "R30")
        # 证据同时列出两条规则的版本与核算
        caps = {ev["rule_id"] for ev in decision["rule_evidence"]
                if ev["type"] == "rolling_window_cap"}
        self.assertEqual(caps, {"R90", "R30"})

        ok = self.e.record_execution(exec_at("r3", "p-ctrl", 100_000, "2026-04-03"))
        self.assertTrue(ok["decision"]["allowed"])

    def test_locked_lot_reduces_layer(self):
        e = make_engine()
        add_lot(e, "lot-l", "ctrl", 1_000_000, unlock="2026-05-01",
                conditions=["board_approval"])
        make_active_plan(e, "p-l", "ctrl", "lot-l", 500_000)
        decision = e.check_execution("p-l", {
            "quantity": 100_000, "trade_time": "2026-04-10T10:00:00+00:00",
            "as_of": "2026-04-10T10:00:00+00:00"})
        self.assertFalse(decision["allowed"])
        lot_layer = [l for l in decision["layers"] if l["layer"] == "lot_availability"][0]
        self.assertEqual(lot_layer["quantity"], 0)
        self.assertIn("解禁日期", lot_layer["lots"][0]["lock_reasons"][0])
        # 解禁日到达且条件满足后放行
        e.satisfy_lot_condition("lot-l", {"condition": "board_approval"})
        ok = e.check_execution("p-l", {
            "quantity": 100_000, "trade_time": "2026-05-02T10:00:00+00:00",
            "as_of": "2026-05-02T10:00:00+00:00"})
        self.assertTrue(ok["allowed"])


class RelationshipTimingTest(unittest.TestCase):
    def test_join_and_leave_keep_history(self):
        """关系变化只影响生效后的合并范围，历史成交保留当时归属。"""
        e = make_engine()
        e.register_account({"account_id": "rel2", "name": "远亲", "kind": "relative"})
        add_lot(e, "lot-c", "ctrl", 5_000_000)
        add_lot(e, "lot-r2", "rel2", 5_000_000)
        make_active_plan(e, "p-ctrl", "ctrl", "lot-c", 2_000_000)
        make_active_plan(e, "p-r2", "rel2", "lot-r2", 2_000_000,
                         start="2026-02-01", end="2026-06-01")

        # rel2 在并入合并组之前的成交：归属仅 {rel2}
        e.record_execution(exec_at("r1", "p-r2", 100_000, "2026-02-10"))
        e.register_relationship({"relationship_id": "rel-r2", "account_a": "ctrl",
                                 "account_b": "rel2", "type": "family",
                                 "effective_from": "2026-03-01"})
        # 并入后的成交：归属 {ctrl, esop, rel, rel2}
        e.record_execution(exec_at("r2", "p-r2", 100_000, "2026-03-10"))
        # 关系于 4/1 终止，此后的成交不再并入
        e.end_relationship("rel-r2", {"effective_to": "2026-04-01"})
        e.record_execution(exec_at("r3", "p-r2", 100_000, "2026-04-10"))

        # ctrl 的 90 日消耗（至 4/15）：3/10 的 10 万仍计入（当时归属含 ctrl），
        # 2/10（并入前）与 4/10（退出后）均不计入
        consumed = e.consumption("ctrl", parse_ts("2026-01-16T00:00:00+00:00"),
                                 parse_ts("2026-04-15T00:00:00+00:00"))
        self.assertEqual(consumed, 100_000)
        # rel2 自身始终看到自己全部 30 万
        consumed_r2 = e.consumption("rel2", parse_ts("2026-01-16T00:00:00+00:00"),
                                    parse_ts("2026-04-15T00:00:00+00:00"))
        self.assertEqual(consumed_r2, 300_000)
        # 归属已冻结在历史成交中
        self.assertEqual(e.executions["r1"].attribution, ["rel2"])
        self.assertEqual(e.executions["r2"].attribution, ["ctrl", "esop", "rel", "rel2"])
        self.assertEqual(e.executions["r3"].attribution, ["rel2"])


class ReviewGuardTest(unittest.TestCase):
    def test_review_state_rejects_edits(self):
        e = make_engine()
        add_lot(e, "lot-c", "ctrl", 5_000_000)
        e.create_plan({"plan_id": "p1", "account_id": "ctrl", "lot_ids": ["lot-c"],
                       "planned_quantity": 1_000_000,
                       "window_start": "2026-03-01T00:00:00+00:00",
                       "window_end": "2026-06-01T00:00:00+00:00"})
        # 草稿态可编辑，但需版本匹配
        with self.assertRaises(DomainError) as ctx:
            e.edit_plan("p1", {"planned_quantity": 900_000, "expected_revision": 9})
        self.assertEqual(ctx.exception.code, "revision_conflict")
        e.edit_plan("p1", {"planned_quantity": 900_000, "expected_revision": 1})
        self.assertEqual(e.plans["p1"].revision, 2)

        e.submit_plan("p1", parse_ts("2026-02-20T00:00:00+00:00"))
        # 送审中：普通编辑被拒绝，即使版本号正确
        with self.assertRaises(DomainError) as ctx:
            e.edit_plan("p1", {"planned_quantity": 800_000, "expected_revision": 2})
        self.assertEqual(ctx.exception.code, "under_review")
        self.assertEqual(e.plans["p1"].planned_quantity, 900_000)
        self.assertEqual(e.plans["p1"].revision, 2)


class ReplayTest(unittest.TestCase):
    def test_out_of_order_revoke_and_idempotent_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            e = ComplianceEngine(EventStore(store_path))
            e.set_company({"total_shares": 100_000_000})
            for acc, kind in (("ctrl", "controller"), ("esop", "employee_platform")):
                e.register_account({"account_id": acc, "name": acc, "kind": kind})
            e.register_relationship({"account_a": "ctrl", "account_b": "esop",
                                     "type": "employee_platform", "effective_from": T0})
            e.register_rule({"rule_id": "R90", "version": "v1",
                             "type": "rolling_window_cap", "effective_from": T0,
                             "params": {"window_days": 90, "max_quantity": 1_000_000}})
            add_lot(e, "lot-c", "ctrl", 5_000_000)
            make_active_plan(e, "p1", "ctrl", "lot-c", 2_000_000)

            # 乱序到达：4/10 先到，4/05 后到，再 4/08
            e.record_execution(exec_at("r-late", "p1", 100_000, "2026-04-10"))
            e.record_execution(exec_at("r-early", "p1", 100_000, "2026-04-05"))
            e.record_execution(exec_at("r-mid", "p1", 100_000, "2026-04-08"))
            # 同一回报重复提交：不重复扣减
            dup = e.record_execution(exec_at("r-mid", "p1", 100_000, "2026-04-08"))
            self.assertTrue(dup["duplicate"])
            # 撤回 4/05 的回报；重复撤回幂等
            e.revoke_execution("r-early", {"reason": "券商撤单",
                                           "as_of": "2026-04-11T09:00:00+00:00"})
            again = e.revoke_execution("r-early", {"reason": "重复撤回"})
            self.assertTrue(again["duplicate"])

            start, end = parse_ts("2026-01-11T00:00:00+00:00"), parse_ts("2026-04-11T00:00:00+00:00")
            self.assertEqual(e.consumption("ctrl", start, end), 200_000)
            self.assertEqual(e.plan_executed("p1"), 200_000)

            # 从事件日志整体重放：派生状态完全一致
            replayed = ComplianceEngine(EventStore(store_path))
            self.assertEqual(replayed.consumption("ctrl", start, end), 200_000)
            self.assertEqual(replayed.plan_executed("p1"), 200_000)
            self.assertEqual(len(replayed.decisions), len(e.decisions))
            self.assertEqual(replayed.executions["r-early"].revoked, True)
            self.assertEqual(replayed.plans["p1"].state, "active")


class SensitiveWindowTest(unittest.TestCase):
    def test_director_blocked_in_forecast_window(self):
        e = make_engine()
        add_lot(e, "lot-d", "dir1", 1_000_000)
        make_active_plan(e, "p-dir", "dir1", "lot-d", 500_000)
        e.register_sensitive_event({"event_id": "sev-1", "event_type": "earnings_forecast",
                                    "announce_date": "2026-05-01",
                                    "applies_to_roles": ["director"]})
        # 敏感期 [4/1, 5/2]：窗口内拒绝并给出冲突窗口证据
        denied = e.check_execution("p-dir", {
            "quantity": 10_000, "trade_time": "2026-04-15T10:00:00+00:00",
            "as_of": "2026-04-15T10:00:00+00:00"})
        self.assertFalse(denied["allowed"])
        self.assertEqual(len(denied["windows"]), 1)
        self.assertEqual(denied["windows"][0]["source"]["kind"], "sensitive")
        self.assertEqual(denied["windows"][0]["source"]["rule_id"], "SW")
        # 窗口外放行
        ok = e.check_execution("p-dir", {
            "quantity": 10_000, "trade_time": "2026-03-15T10:00:00+00:00",
            "as_of": "2026-03-15T10:00:00+00:00"})
        self.assertTrue(ok["allowed"])
        # 非董事账户不受该窗口限制
        add_lot(e, "lot-c", "ctrl", 1_000_000)
        make_active_plan(e, "p-c", "ctrl", "lot-c", 500_000)
        ok2 = e.check_execution("p-c", {
            "quantity": 10_000, "trade_time": "2026-04-15T10:00:00+00:00",
            "as_of": "2026-04-15T10:00:00+00:00"})
        self.assertTrue(ok2["allowed"])


class DisclosureWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.e = make_engine()
        add_lot(self.e, "lot-c", "ctrl", 5_000_000)
        make_active_plan(self.e, "p1", "ctrl", "lot-c", 2_000_000)

    def test_threshold_triggers_announcement_and_overdue_suspends(self):
        # 累计 25 万 < 30 万阈值：无公告
        r1 = self.e.record_execution(exec_at("r1", "p1", 250_000, "2026-04-01"))
        self.assertEqual(r1["decision"]["detail"]["announcements_created"], [])
        # 累计 35 万，跨越阈值：生成时限 2 天的公告
        r2 = self.e.record_execution(exec_at("r2", "p1", 100_000, "2026-04-02"))
        created = r2["decision"]["detail"]["announcements_created"]
        self.assertEqual(len(created), 1)
        ann = self.e.announcements[created[0]]
        self.assertEqual(ann.status, "pending")
        self.assertEqual(ann.crossing_no, 1)
        # 未逾期时不阻断放行
        ok = self.e.check_execution("p1", {
            "quantity": 10_000, "trade_time": "2026-04-03T10:00:00+00:00",
            "as_of": "2026-04-03T10:00:00+00:00"})
        self.assertTrue(ok["allowed"])
        # 逾期未确认：自动暂停，后续放行被拒绝
        self.e.sweep(parse_ts("2026-04-05T00:00:00+00:00"))
        self.assertEqual(self.e.announcements[created[0]].status, "overdue")
        self.assertEqual(self.e.plans["p1"].state, "suspended")
        with self.assertRaises(DomainError) as ctx:
            self.e.record_execution(exec_at("r3", "p1", 10_000, "2026-04-05"))
        self.assertEqual(ctx.exception.code, "execution_rejected")
        # 确认公告后恢复计划，放行恢复
        self.e.confirm_announcement(created[0], {"as_of": "2026-04-05T10:00:00+00:00"})
        self.e.resume_plan("p1", parse_ts("2026-04-05T11:00:00+00:00"))
        ok2 = self.e.record_execution(exec_at("r4", "p1", 10_000, "2026-04-05"))
        self.assertTrue(ok2["decision"]["allowed"])

    def test_resume_blocked_while_overdue(self):
        self.e.record_execution(exec_at("r1", "p1", 350_000, "2026-04-01"))
        self.e.sweep(parse_ts("2026-04-05T00:00:00+00:00"))
        self.assertEqual(self.e.plans["p1"].state, "suspended")
        with self.assertRaises(DomainError) as ctx:
            self.e.resume_plan("p1", parse_ts("2026-04-05T12:00:00+00:00"))
        self.assertEqual(ctx.exception.code, "overdue_disclosure")


class PlanViewTest(unittest.TestCase):
    def test_view_is_explainable(self):
        e = make_engine()
        add_lot(e, "lot-c", "ctrl", 5_000_000)
        add_lot(e, "lot-e", "esop", 5_000_000)
        make_active_plan(e, "p-ctrl", "ctrl", "lot-c", 2_000_000)
        make_active_plan(e, "p-esop", "esop", "lot-e", 2_000_000)
        e.record_execution(exec_at("r1", "p-ctrl", 200_000, "2026-04-01"))
        e.record_execution(exec_at("r2", "p-esop", 150_000, "2026-04-02"))

        view = e.plan_view("p-ctrl", parse_ts("2026-04-03T00:00:00+00:00"))
        # 逐层计算
        layers = {l["layer"]: l for l in view["reducible"]["layers"]}
        self.assertEqual(layers["plan_remaining"]["quantity"], 1_800_000)
        self.assertEqual(layers["lot_availability"]["quantity"], 4_800_000)
        self.assertEqual(view["reducible"]["final"], 150_000)  # R30: 50万-35万
        # 关联账户贡献
        r30 = [c for c in view["contributions"] if c["rule_id"] == "R30"][0]
        self.assertEqual({c["account_id"]: c["quantity"] for c in r30["by_account"]},
                         {"ctrl": 200_000, "esop": 150_000})
        # 决策历史含规则证据
        self.assertTrue(view["decisions"])
        exec_decisions = [d for d in view["decisions"] if d["action"] == "execute"]
        self.assertTrue(exec_decisions)
        evidence = exec_decisions[0]["rule_evidence"]
        self.assertTrue(any(ev["rule_id"] == "R90" and ev["version"] == "v1"
                            for ev in evidence))
        self.assertIn("pending_disclosures", view)
        self.assertIn("conflicting_windows", view)


class RuleVersionTest(unittest.TestCase):
    def test_version_effective_time(self):
        e = make_engine()
        add_lot(e, "lot-c", "ctrl", 5_000_000)
        make_active_plan(e, "p1", "ctrl", "lot-c", 2_000_000)
        # R30 自 4/1 起收紧为 30 万
        e.register_rule({"rule_id": "R30", "version": "v2", "type": "rolling_window_cap",
                         "effective_from": "2026-04-01T00:00:00+00:00",
                         "params": {"window_days": 30, "max_quantity": 300_000}})
        d1 = e.check_execution("p1", {"quantity": 400_000,
                                      "trade_time": "2026-03-15T10:00:00+00:00",
                                      "as_of": "2026-03-15T10:00:00+00:00"})
        self.assertTrue(d1["allowed"])  # v1 上限 50 万
        d2 = e.check_execution("p1", {"quantity": 400_000,
                                      "trade_time": "2026-04-15T10:00:00+00:00",
                                      "as_of": "2026-04-15T10:00:00+00:00"})
        self.assertFalse(d2["allowed"])  # v2 上限 30 万
        versions = {ev["rule_id"]: ev["version"] for ev in d2["rule_evidence"]
                    if ev["type"] == "rolling_window_cap"}
        self.assertEqual(versions["R30"], "v2")


if __name__ == "__main__":
    unittest.main()
