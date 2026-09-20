"""合规系统端到端测试：覆盖合并计算、最严格层、送审锁、成交重放、
窗口、披露闸门、规则版本时点与事件持久化重放。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from compliance import ComplianceService, EventStore
from compliance.errors import (
    NotFoundError,
    PlanLockedError,
    StateConflictError,
    ValidationError,
)

CAP = 100_000_000  # 公司总股本 1 亿


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = ComplianceService(EventStore(None))
        s = self.svc
        s.register_shareholder({
            "shareholder_id": "C", "name": "实控人", "kind": "natural_person",
            "is_controlling_person": True,
        })
        s.register_shareholder({
            "shareholder_id": "F", "name": "亲属账户", "kind": "family_account",
        })
        s.register_shareholder({
            "shareholder_id": "EP", "name": "员工持股平台", "kind": "employee_platform",
        })
        s.register_shareholder({
            "shareholder_id": "D", "name": "在职董事", "kind": "natural_person",
            "is_director": True,
        })
        s.declare_relationship({
            "rel_id": "rel-f", "subject_id": "F", "group_owner_id": "C",
            "rel_type": "family", "effective_from": "2026-01-01",
        })
        s.declare_relationship({
            "rel_id": "rel-ep", "subject_id": "EP", "group_owner_id": "C",
            "rel_type": "employee_platform", "effective_from": "2026-01-01",
        })
        # 董事 D 同时是实际控制人的一致行动人/亲属，纳入合并范围
        s.declare_relationship({
            "rel_id": "rel-d", "subject_id": "D", "group_owner_id": "C",
            "rel_type": "family", "effective_from": "2026-01-01",
        })
        # 批次与解禁
        s.record_batch({
            "batch_id": "b-c", "shareholder_id": "C", "security_code": "600000",
            "source": "IPO_lockup", "total_qty": 50_000_000,
        })
        s.record_unlock({
            "unlock_id": "u-c", "batch_id": "b-c", "condition_type": "date",
            "unlock_date": "2026-01-01", "tranche_qty": 50_000_000,
        })
        s.record_batch({
            "batch_id": "b-f", "shareholder_id": "F", "security_code": "600000",
            "source": "private_placement", "total_qty": 2_000_000,
            "issuer_lockup_until": "2026-03-31",
        })
        s.record_unlock({
            "unlock_id": "u-f", "batch_id": "b-f", "condition_type": "date",
            "unlock_date": "2026-01-01", "tranche_qty": 2_000_000,
        })
        s.record_batch({
            "batch_id": "b-ep", "shareholder_id": "EP", "security_code": "600000",
            "source": "incentive_award", "total_qty": 3_000_000,
        })
        s.record_unlock({
            "unlock_id": "u-ep-1", "batch_id": "b-ep", "condition_type": "performance",
            "unlock_date": "2026-01-01", "tranche_qty": 1_000_000,
            "satisfied": True,
        })
        s.record_unlock({
            "unlock_id": "u-ep-2", "batch_id": "b-ep", "condition_type": "performance",
            "unlock_date": "2026-01-01", "tranche_qty": 2_000_000,
            "satisfied": False, "note": "第二期考核未达标",
        })
        s.record_batch({
            "batch_id": "b-d", "shareholder_id": "D", "security_code": "600000",
            "source": "director_holding", "total_qty": 400_000,
        })
        s.record_unlock({
            "unlock_id": "u-d", "batch_id": "b-d", "condition_type": "date",
            "unlock_date": "2026-01-01", "tranche_qty": 400_000,
        })
        s.publish_rule({
            "version": "2026.1", "effective_from": "2026-01-01",
            "name": "减持监管与内部规则", "announcement_confirm_days": 2,
        })

    def make_active_plan(self, **overrides):
        """创建并核准一个 2026 年的 active 计划，确认预披露公告。"""
        s = self.svc
        params = {
            "plan_id": "P1",
            "group_owner_id": "C",
            "security_code": "600000",
            "share_capital": CAP,
            "effective_from": "2026-05-01",
            "effective_to": "2026-12-31",
            "proposed_qty": 3_000_000,
            "channels": ["secondary", "block"],
        }
        params.update(overrides)
        s.create_plan(params)
        s.submit_plan({"plan_id": "P1"})
        s.approve_plan({"plan_id": "P1", "approved_date": "2026-04-20"})
        pending = self._announcements("P1", kind="pre_disclosure")
        if pending:
            s.confirm_announcement({
                "plan_id": "P1", "announcement_id": pending[0]["announcement_id"],
                "confirmed_date": "2026-04-21",
            })
        return params

    def _announcements(self, plan_id, *, kind=None):
        return [
            a for a in self.svc.get_plan(plan_id)["pending_disclosures"]
            if kind is None or a["kind"] == kind
        ]

    # -------------------------------------------------------------- 合并与解禁

    def test_group_membership_and_lockup_layers(self):
        self.make_active_plan()
        q = self.svc.get_plan("P1", as_of="2026-02-01")
        # 2 月：F 受发行人锁定至 3/31 不可用；EP 仅第一期 100 万满足考核
        holdings = next(
            l for l in q["available_qty"]["layers"]
            if l["key"] == "unlocked_holdings"
        )
        per = {x["shareholder_id"]: x for x in holdings["evidence"]["per_account"]}
        self.assertEqual(per["C"]["available_qty"], 50_000_000)
        self.assertEqual(per["F"]["available_qty"], 0)
        f_batch = per["F"]["batches"][0]
        self.assertEqual(
            f_batch["unlocks"][0]["reason_not_counted"], "发行人锁定期未满"
        )
        self.assertEqual(per["EP"]["available_qty"], 1_000_000)
        ep_unlocks = per["EP"]["batches"][0]["unlocks"]
        self.assertEqual(
            [u["counted"] for u in ep_unlocks], [True, False]
        )

        # 4 月后 F 锁定解除
        q2 = self.svc.get_plan("P1", as_of="2026-04-01")
        holdings2 = next(
            l for l in q2["available_qty"]["layers"]
            if l["key"] == "unlocked_holdings"
        )
        per2 = {x["shareholder_id"]: x for x in holdings2["evidence"]["per_account"]}
        self.assertEqual(per2["F"]["available_qty"], 2_000_000)

    def test_relationship_change_only_affects_future(self):
        s = self.svc
        self.make_active_plan()
        # 6/10 终止亲属关系
        s.end_relationship({"rel_id": "rel-f", "effective_to": "2026-06-10"})
        # 关系期内成交保留
        r = s.report_trade({
            "report_id": "T-OLD", "plan_id": "P1", "shareholder_id": "F",
            "batch_id": "b-f", "channel": "secondary",
            "trade_date": "2026-05-15", "qty": 100_000,
        })
        self.assertEqual(r["evaluation"]["result"], "approved")
        # 终止后 F 不再是成员，不能再用其账户成交
        with self.assertRaises(ValidationError):
            s.report_trade({
                "report_id": "T-NEW", "plan_id": "P1", "shareholder_id": "F",
                "batch_id": "b-f", "channel": "secondary",
                "trade_date": "2026-06-11", "qty": 100,
            })
        q = s.get_plan("P1", as_of="2026-06-11")
        self.assertEqual(q["members"], ["C", "D", "EP"])
        # 历史成交仍计入计划与组合 90 日窗口（当时归属）
        rows = {r["report_id"]: r for r in q["trade_replay"]["rows"]}
        self.assertTrue(rows["T-OLD"]["counted"])
        contrib = {c["shareholder_id"]: c for c in q["contributions"]}
        self.assertFalse(contrib["F"]["included"])
        self.assertEqual(contrib["F"]["qty_in_plan"], 100_000)
        sec = next(
            l for l in q["available_qty"]["layers"]
            if l["key"] == "controller_secondary"
        )
        self.assertIn("T-OLD", [t["report_id"] for t in sec["evidence"]["trades"]])

    def test_future_relationship_not_counted_yet(self):
        s = self.svc
        s.register_shareholder({
            "shareholder_id": "X", "name": "未来入股方", "kind": "company",
        })
        s.declare_relationship({
            "rel_id": "rel-future", "subject_id": "X", "group_owner_id": "C",
            "rel_type": "family", "effective_from": "2026-09-01",
        })
        self.make_active_plan()
        q = self.svc.get_plan("P1", as_of="2026-08-01")
        self.assertNotIn("X", q["members"])
        q2 = self.svc.get_plan("P1", as_of="2026-09-02")
        self.assertIn("X", q2["members"])

    # -------------------------------------------------------------- 最严格层

    def test_most_restrictive_layer_wins(self):
        s = self.svc
        # 计划只拟减 50 万股 → 计划层最严；控制器集中竞价上限 100 万股
        self.make_active_plan(proposed_qty=500_000)
        q = s.get_plan("P1", as_of="2026-05-01")
        self.assertEqual(q["available_qty"]["binding_layer"], "plan_remaining")
        self.assertEqual(q["available_qty"]["allowed_qty"], 500_000)

    def test_controller_channel_caps_and_90day_rolling(self):
        s = self.svc
        self.make_active_plan(proposed_qty=10_000_000)
        s.report_trade({
            "report_id": "T1", "plan_id": "P1", "shareholder_id": "C",
            "batch_id": "b-c", "channel": "secondary",
            "trade_date": "2026-05-01", "qty": 800_000,
        })
        ev = s.evaluate({
            "plan_id": "P1", "date": "2026-05-10", "channel": "secondary",
            "qty": 300_000, "shareholder_id": "C",
        })
        self.assertEqual(ev["result"], "denied")  # 80万+30万 > 90日100万
        self.assertEqual(ev["binding_layer"], "controller_secondary")
        # 大宗交易独立 2% 额度
        ev2 = s.evaluate({
            "plan_id": "P1", "date": "2026-05-10", "channel": "block",
            "qty": 1_500_000, "shareholder_id": "C",
        })
        self.assertEqual(ev2["result"], "approved")
        # 滚动窗口：91 天后早期成交滑出窗口
        ev3 = s.evaluate({
            "plan_id": "P1", "date": "2026-08-01", "channel": "secondary",
            "qty": 900_000, "shareholder_id": "C",
        })
        self.assertEqual(ev3["result"], "approved")

    def test_director_annual_quarter_cap_scoped_to_account(self):
        s = self.svc
        self.make_active_plan(proposed_qty=10_000_000)
        # 董事 40 万股 × 25% = 10 万股
        ev = s.evaluate({
            "plan_id": "P1", "date": "2026-05-01", "channel": "secondary",
            "qty": 120_000, "shareholder_id": "D",
        })
        self.assertEqual(ev["result"], "denied")
        self.assertEqual(ev["binding_layer"], "director_annual")
        ev2 = s.evaluate({
            "plan_id": "P1", "date": "2026-05-01", "channel": "secondary",
            "qty": 100_000, "shareholder_id": "D",
        })
        self.assertEqual(ev2["result"], "approved")
        # 组合口径查询不被单一董事的账户额度绑定
        q = s.get_plan("P1", as_of="2026-05-01")
        self.assertEqual(q["available_qty"]["binding_layer"], "controller_secondary")

    # -------------------------------------------------------------- 送审锁与状态机

    def test_plan_in_review_cannot_be_edited(self):
        s = self.svc
        s.create_plan({
            "plan_id": "PD", "group_owner_id": "C", "security_code": "600000",
            "share_capital": CAP, "effective_from": "2026-05-01",
            "proposed_qty": 1_000_000, "channels": ["secondary"],
        })
        s.submit_plan({"plan_id": "PD"})
        with self.assertRaises(PlanLockedError):
            s.edit_plan({"plan_id": "PD", "proposed_qty": 2_000_000})
        # 撤回后可继续编辑
        s.withdraw_plan({"plan_id": "PD"})
        out = s.edit_plan({"plan_id": "PD", "proposed_qty": 2_000_000})
        self.assertEqual(out["version"]["proposed_qty"], 2_000_000)

    def test_change_review_keeps_active_version(self):
        s = self.svc
        self.make_active_plan()
        before = s.get_plan("P1")["current_version"]["version_no"]
        s.submit_plan_change({
            "plan_id": "P1", "proposed_qty": 9_000_000,
            "effective_from": "2026-07-01",
        })
        # 送审期间普通编辑锁定
        with self.assertRaises(PlanLockedError):
            s.edit_plan({"plan_id": "P1", "proposed_qty": 1})
        # 现行版本仍 active，可继续执行
        cur = s.get_plan("P1")["current_version"]
        self.assertEqual(cur["state"], "active")
        self.assertEqual(cur["proposed_qty"], 3_000_000)
        # 变更驳回后版本不变
        s.reject_plan_change({"plan_id": "P1"})
        self.assertEqual(
            s.get_plan("P1")["current_version"]["proposed_qty"], 3_000_000
        )

    def test_change_approved_takes_effect_from_its_date(self):
        s = self.svc
        self.make_active_plan()
        s.submit_plan_change({
            "plan_id": "P1", "proposed_qty": 9_000_000,
            "effective_from": "2026-07-01",
        })
        s.approve_plan_change({"plan_id": "P1", "approved_date": "2026-06-20"})
        q = s.get_plan("P1")
        self.assertEqual(q["current_version"]["proposed_qty"], 9_000_000)
        self.assertEqual(q["state"], "active")
        # 留下变更公告
        self.assertTrue(any(
            a["kind"] == "plan_change" for a in q["pending_disclosures"]
        ))

    def test_terminal_states(self):
        s = self.svc
        self.make_active_plan()
        s.suspend_plan({"plan_id": "P1", "date": "2026-06-01"})
        ev = s.evaluate({
            "plan_id": "P1", "date": "2026-06-02", "channel": "secondary",
            "qty": 100, "shareholder_id": "C",
        })
        self.assertIn("suspended", ev["reasons"][0])
        s.resume_plan({"plan_id": "P1", "date": "2026-06-03"})
        s.terminate_plan({"plan_id": "P1", "terminated_date": "2026-07-01"})
        self.assertEqual(s.get_plan("P1")["state"], "closed")
        with self.assertRaises(StateConflictError):
            s.report_trade({
                "report_id": "TX", "plan_id": "P1", "shareholder_id": "C",
                "batch_id": "b-c", "channel": "secondary",
                "trade_date": "2026-07-02", "qty": 100,
            })

    # -------------------------------------------------------------- 成交重放

    def test_out_of_order_replay_and_dedup(self):
        s = self.svc
        self.make_active_plan(proposed_qty=10_000_000)
        # 乱序：先晚报后早报
        s.report_trade({
            "report_id": "T2", "plan_id": "P1", "shareholder_id": "C",
            "batch_id": "b-c", "channel": "secondary",
            "trade_date": "2026-05-20", "qty": 500_000,
        })
        s.report_trade({
            "report_id": "T1", "plan_id": "P1", "shareholder_id": "C",
            "batch_id": "b-c", "channel": "secondary",
            "trade_date": "2026-05-10", "qty": 400_000,
        })
        # 同一回报不得重复扣减
        with self.assertRaises(StateConflictError):
            s.report_trade({
                "report_id": "T1", "plan_id": "P1", "shareholder_id": "C",
                "batch_id": "b-c", "channel": "secondary",
                "trade_date": "2026-05-10", "qty": 400_000,
            })
        q = s.get_plan("P1")
        self.assertEqual(q["trade_replay"]["counted_qty"], 900_000)
        self.assertEqual(
            [r["report_id"] for r in q["trade_replay"]["rows"]], ["T1", "T2"]
        )

    def test_withdraw_restores_quota(self):
        s = self.svc
        self.make_active_plan(proposed_qty=10_000_000)
        s.report_trade({
            "report_id": "T1", "plan_id": "P1", "shareholder_id": "C",
            "batch_id": "b-c", "channel": "secondary",
            "trade_date": "2026-05-10", "qty": 950_000,
        })
        # 90 日仅剩 5 万
        ev = s.evaluate({
            "plan_id": "P1", "date": "2026-05-11", "channel": "secondary",
            "qty": 60_000, "shareholder_id": "C",
        })
        self.assertEqual(ev["result"], "denied")
        s.withdraw_trade({"report_id": "T1"})
        ev2 = s.evaluate({
            "plan_id": "P1", "date": "2026-05-11", "channel": "secondary",
            "qty": 60_000, "shareholder_id": "C",
        })
        self.assertEqual(ev2["result"], "approved")
        # 撤回不能重复
        with self.assertRaises(StateConflictError):
            s.withdraw_trade({"report_id": "T1"})

    def test_replace_trade_replays_with_correction(self):
        s = self.svc
        self.make_active_plan(proposed_qty=10_000_000)
        s.report_trade({
            "report_id": "T1", "plan_id": "P1", "shareholder_id": "C",
            "batch_id": "b-c", "channel": "secondary",
            "trade_date": "2026-05-10", "qty": 950_000,
        })
        s.replace_trade({
            "report_id": "T1",
            "replacement": {
                "report_id": "T1-FIX", "channel": "secondary",
                "trade_date": "2026-05-10", "qty": 300_000,
            },
        })
        q = s.get_plan("P1")
        rows = {r["report_id"]: r for r in q["trade_replay"]["rows"]}
        self.assertFalse(rows["T1"]["counted"])
        self.assertTrue(rows["T1-FIX"]["counted"])
        self.assertEqual(rows["T1-FIX"]["replaces"], "T1")
        self.assertEqual(q["trade_replay"]["counted_qty"], 300_000)

    # -------------------------------------------------------------- 窗口期

    def test_blocking_window_denies_and_is_listed(self):
        s = self.svc
        self.make_active_plan()
        s.declare_window({
            "window_id": "w-earnings", "security_code": "600000",
            "title": "半年度业绩预告敏感期",
            "start_date": "2026-07-10", "end_date": "2026-07-20",
        })
        ev = s.evaluate({
            "plan_id": "P1", "date": "2026-07-15", "channel": "secondary",
            "qty": 100, "shareholder_id": "C",
        })
        self.assertEqual(ev["result"], "denied")
        self.assertTrue(any("业绩预告" in r for r in ev["reasons"]))
        q = s.get_plan("P1", as_of="2026-07-01")
        self.assertTrue(any(
            w["window_id"] == "w-earnings" and w["overlaps_plan_period"]
            for w in q["conflict_windows"]
        ))
        # 窗口外正常
        ev2 = s.evaluate({
            "plan_id": "P1", "date": "2026-07-21", "channel": "secondary",
            "qty": 100, "shareholder_id": "C",
        })
        self.assertEqual(ev2["result"], "approved")

    # -------------------------------------------------------------- 披露闸门

    def test_pre_disclosure_overdue_blocks_until_confirmed(self):
        s = self.svc
        # 拟减 3% > 1% 阈值，核准时自动创建预披露公告（2 个工作日时限）
        s.create_plan({
            "plan_id": "PP", "group_owner_id": "C", "security_code": "600000",
            "share_capital": CAP, "effective_from": "2026-04-21",
            "effective_to": "2026-12-31", "proposed_qty": 3_000_000,
            "channels": ["secondary"],
        })
        s.submit_plan({"plan_id": "PP"})
        s.approve_plan({"plan_id": "PP", "approved_date": "2026-04-20"})
        ann = self._announcements("PP", kind="pre_disclosure")[0]
        self.assertEqual(ann["due_date"], "2026-04-22")
        # 到期未确认 → 暂停放行
        ev = s.evaluate({
            "plan_id": "PP", "date": "2026-04-23", "channel": "secondary",
            "qty": 100, "shareholder_id": "C",
        })
        self.assertEqual(ev["result"], "denied")
        self.assertTrue(any("逾期" in r for r in ev["reasons"]))
        # 确认后恢复
        s.confirm_announcement({
            "plan_id": "PP", "announcement_id": ann["announcement_id"],
            "confirmed_date": "2026-04-23",
        })
        ev2 = s.evaluate({
            "plan_id": "PP", "date": "2026-04-23", "channel": "secondary",
            "qty": 100, "shareholder_id": "C",
        })
        self.assertEqual(ev2["result"], "approved")

    def test_holding_step_announcement_created(self):
        s = self.svc
        self.make_active_plan(proposed_qty=10_000_000)
        s.report_trade({
            "report_id": "S1", "plan_id": "P1", "shareholder_id": "C",
            "batch_id": "b-c", "channel": "block",
            "trade_date": "2026-05-05", "qty": 800_000,
        })
        self.assertEqual(
            [a["step_no"] for a in self._announcements("P1", kind="holding_step")],
            [],
        )
        s.report_trade({
            "report_id": "S2", "plan_id": "P1", "shareholder_id": "C",
            "batch_id": "b-c", "channel": "block",
            "trade_date": "2026-05-06", "qty": 300_000,
        })
        steps = self._announcements("P1", kind="holding_step")
        self.assertEqual([a["step_no"] for a in steps], [1])
        self.assertEqual(steps[0]["triggered_date"], "2026-05-06")
        # 幂等：撤回重放不会重复建同阶梯
        s.withdraw_trade({"report_id": "S2"})
        s.report_trade({
            "report_id": "S3", "plan_id": "P1", "shareholder_id": "C",
            "batch_id": "b-c", "channel": "block",
            "trade_date": "2026-05-07", "qty": 300_000,
        })
        steps2 = self._announcements("P1", kind="holding_step")
        self.assertEqual(len([a for a in steps2 if a["step_no"] == 1]), 1)

    # -------------------------------------------------------------- 规则版本时点

    def test_rule_versions_apply_by_business_date(self):
        s = self.svc
        self.make_active_plan(proposed_qty=10_000_000)
        # 8/1 起集中竞价上限收紧为 0.5%
        s.publish_rule({
            "version": "2026.2", "effective_from": "2026-08-01",
            "name": "收紧规则", "controller_secondary_pct": 0.5,
        })
        ev_old = s.evaluate({
            "plan_id": "P1", "date": "2026-07-31", "channel": "secondary",
            "qty": 900_000, "shareholder_id": "C",
        })
        ev_new = s.evaluate({
            "plan_id": "P1", "date": "2026-08-01", "channel": "secondary",
            "qty": 900_000, "shareholder_id": "C",
        })
        self.assertEqual(ev_old["result"], "approved")
        self.assertEqual(ev_new["result"], "denied")
        self.assertEqual(ev_new["layers"]["rule"]["version"], "2026.2")
        self.assertEqual(ev_new["allowed_qty"], 500_000)

    # -------------------------------------------------------------- 决策证据

    def test_decisions_recorded_with_rule_evidence(self):
        s = self.svc
        self.make_active_plan()
        s.report_trade({
            "report_id": "E1", "plan_id": "P1", "shareholder_id": "C",
            "batch_id": "b-c", "channel": "secondary",
            "trade_date": "2026-05-02", "qty": 10_000,
        })
        q = s.get_plan("P1")
        actions = [d["action"] for d in q["decisions"]]
        self.assertIn("submit", actions)
        self.assertIn("approve", actions)
        self.assertIn("trade_reported", actions)
        trade_decision = next(d for d in q["decisions"] if d["action"] == "trade_reported")
        self.assertEqual(trade_decision["result"], "approved")
        self.assertEqual(trade_decision["rule"]["version"], "2026.1")
        self.assertTrue(trade_decision["layers"]["layers"])

    # -------------------------------------------------------------- 持久化重放

    def test_event_store_reload(self):
        s = self.svc
        self.make_active_plan()
        s.report_trade({
            "report_id": "P-T1", "plan_id": "P1", "shareholder_id": "C",
            "batch_id": "b-c", "channel": "secondary",
            "trade_date": "2026-05-03", "qty": 12_345,
        })
        before = s.get_plan("P1")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            live = ComplianceService(EventStore(str(path)))
            # 用同一脚本重建数据
            self._rebuild_fixture(live)
            reloaded = ComplianceService(EventStore(str(path)))
            after = reloaded.get_plan("P1")
            self.assertEqual(after["state"], before["state"])
            self.assertEqual(
                after["trade_replay"]["counted_qty"],
                before["trade_replay"]["counted_qty"],
            )
            self.assertEqual(
                after["available_qty"]["allowed_qty"],
                before["available_qty"]["allowed_qty"],
            )

    def _rebuild_fixture(self, svc):
        # 简化重建：在新 store 上复刻关键对象与一笔计划成交
        cap = CAP
        svc.register_shareholder({
            "shareholder_id": "C", "name": "实控人", "kind": "natural_person",
            "is_controlling_person": True,
        })
        svc.record_batch({
            "batch_id": "b-c", "shareholder_id": "C", "security_code": "600000",
            "source": "IPO_lockup", "total_qty": 50_000_000,
        })
        svc.record_unlock({
            "unlock_id": "u-c", "batch_id": "b-c", "condition_type": "date",
            "unlock_date": "2026-01-01", "tranche_qty": 50_000_000,
        })
        svc.publish_rule({"version": "2026.1", "effective_from": "2026-01-01", "name": "x"})
        svc.create_plan({
            "plan_id": "P1", "group_owner_id": "C", "security_code": "600000",
            "share_capital": cap, "effective_from": "2026-05-01",
            "proposed_qty": 3_000_000, "channels": ["secondary", "block"],
        })
        svc.submit_plan({"plan_id": "P1"})
        svc.approve_plan({"plan_id": "P1", "approved_date": "2026-04-20"})
        ann = svc.get_plan("P1")["pending_disclosures"][0]
        svc.confirm_announcement({
            "plan_id": "P1", "announcement_id": ann["announcement_id"],
            "confirmed_date": "2026-04-21",
        })
        svc.report_trade({
            "report_id": "P-T1", "plan_id": "P1", "shareholder_id": "C",
            "batch_id": "b-c", "channel": "secondary",
            "trade_date": "2026-05-03", "qty": 12_345,
        })

    def test_missing_plan_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.svc.get_plan("NOPE")


if __name__ == "__main__":
    unittest.main()
