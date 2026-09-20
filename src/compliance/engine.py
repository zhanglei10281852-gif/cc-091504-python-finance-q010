"""合规引擎：维护股东关系、证券批次、限售来源、解禁条件、已披露计划、
窗口期、成交回报与监管规则版本，并在计划提交、变更、执行与终止时给出
可解释的放行结论。

核心语义
--------
1. 合并额度：股东关系构成合并组（连通分量），按业务时间解析。成交回报
   记录时冻结当时归属（attribution = 成交时刻合并组成员）。账户 A 的
   合并消耗 = 所有 attribution 含 A 的未撤回成交之和 —— 关系变化只影响
   生效后的合并范围，历史成交保留当时归属。
2. 最严格原则：可减数量 = min(计划剩余, 批次可用, 各数量规则剩余额度)。
3. 送审保护：review 状态的计划拒绝普通编辑；可编辑状态要求
   expected_revision 乐观并发校验。
4. 可重放：成交事件按 (trade_time, seq) 排序重放；report_id 幂等，
   撤回为补偿事件，同一回报不会重复扣减。
5. 披露阈值：计划累计成交跨越阈值即生成有时限的公告；逾期未确认自动
   暂停计划，确认后方可恢复。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

from .models import (
    ACCOUNT_KINDS,
    PLAN_STATES,
    RULE_TYPES,
    Account,
    Announcement,
    DomainError,
    Execution,
    Lot,
    Plan,
    Relationship,
    RuleVersion,
    SensitiveEvent,
    Window,
    iso,
    now_utc,
    parse_ts,
)
from .store import EventStore

DEFAULT_ENUMS = {
    "restriction_sources": ["IPO_lockup", "director_holding", "private_placement", "incentive_award"],
    "plan_states": list(PLAN_STATES),
    "relationship_types": ["controller", "family", "employee_platform"],
}


class ComplianceEngine:
    def __init__(self, store: EventStore | None = None, enums: dict | None = None):
        self.store = store or EventStore()
        self.enums = {**DEFAULT_ENUMS, **(enums or {})}
        self._lock = threading.RLock()
        self._seq = 0
        self._counters: dict[str, int] = {}

        self.accounts: dict[str, Account] = {}
        self.relationships: dict[str, Relationship] = {}
        self.lots: dict[str, Lot] = {}
        self.rules: dict[str, RuleVersion] = {}  # key: "rule_id@version"
        self.sensitive_events: dict[str, SensitiveEvent] = {}
        self.windows: dict[str, Window] = {}
        self.plans: dict[str, Plan] = {}
        self.executions: dict[str, Execution] = {}  # key: report_id
        self.announcements: dict[str, Announcement] = {}
        self.decisions: list[dict] = []
        self.company: dict = {"total_shares": None}

        for ev in self.store.events:
            self._apply(ev)
            self._seq = max(self._seq, int(ev.get("seq", 0)))

    # ------------------------------------------------------------------
    # 事件基础设施
    # ------------------------------------------------------------------

    def _gen_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    def _note_id(self, entity_id: str) -> None:
        """重放时恢复 ID 计数器，避免与服务生成的 ID 冲突。"""
        if "-" not in entity_id:
            return
        prefix, _, suffix = entity_id.rpartition("-")
        if suffix.isdigit():
            self._counters[prefix] = max(self._counters.get(prefix, 0), int(suffix))

    def _emit(self, type_: str, data: dict) -> dict:
        self._seq += 1
        ev = {"seq": self._seq, "type": type_, "at": iso(now_utc()), "data": data}
        self.store.append(ev)
        self._apply(ev)
        return ev

    def _apply(self, ev: dict) -> None:
        t, d = ev["type"], ev["data"]
        if t == "account_registered":
            a = Account.from_dict(d)
            self.accounts[a.account_id] = a
            self._note_id(a.account_id)
        elif t == "relationship_registered":
            r = Relationship.from_dict(d)
            self.relationships[r.relationship_id] = r
            self._note_id(r.relationship_id)
        elif t == "relationship_ended":
            rel = self.relationships[d["relationship_id"]]
            rel.effective_to = parse_ts(d["effective_to"])
        elif t == "lot_registered":
            lot = Lot.from_dict(d)
            self.lots[lot.lot_id] = lot
            self._note_id(lot.lot_id)
        elif t == "lot_condition_satisfied":
            lot = self.lots[d["lot_id"]]
            if d["condition"] not in lot.conditions_satisfied:
                lot.conditions_satisfied.append(d["condition"])
        elif t == "rule_registered":
            r = RuleVersion.from_dict(d)
            self.rules[f"{r.rule_id}@{r.version}"] = r
        elif t == "sensitive_event_registered":
            e = SensitiveEvent.from_dict(d)
            self.sensitive_events[e.event_id] = e
            self._note_id(e.event_id)
        elif t == "window_registered":
            w = Window.from_dict(d)
            self.windows[w.window_id] = w
            self._note_id(w.window_id)
        elif t == "company_configured":
            self.company["total_shares"] = int(d["total_shares"])
        elif t == "plan_created":
            p = Plan.from_dict(d)
            self.plans[p.plan_id] = p
            self._note_id(p.plan_id)
        elif t == "plan_edited":
            p = self.plans[d["plan_id"]]
            ch = d["changes"]
            if "planned_quantity" in ch:
                p.planned_quantity = int(ch["planned_quantity"])
            if "window_start" in ch:
                p.window_start = parse_ts(ch["window_start"])
            if "window_end" in ch:
                p.window_end = parse_ts(ch["window_end"])
            if "lot_ids" in ch:
                p.lot_ids = list(ch["lot_ids"])
            p.revision = int(d["revision"])
        elif t in ("plan_submitted", "plan_approved", "plan_suspended", "plan_resumed", "plan_closed"):
            self.plans[d["plan_id"]].state = d["state"]
        elif t == "execution_recorded":
            ex = Execution.from_dict(d)
            self.executions[ex.report_id] = ex
        elif t == "execution_revoked":
            ex = self.executions[d["report_id"]]
            ex.revoked = True
            ex.revoked_at = parse_ts(d["as_of"])
            ex.revoke_reason = d.get("reason")
        elif t == "announcement_created":
            a = Announcement.from_dict(d)
            self.announcements[a.announcement_id] = a
            self._note_id(a.announcement_id)
        elif t == "announcement_confirmed":
            a = self.announcements[d["announcement_id"]]
            a.status = "confirmed"
            a.confirmed_at = parse_ts(d["as_of"])
        elif t == "announcement_overdue":
            self.announcements[d["announcement_id"]].status = "overdue"
        elif t == "decision_logged":
            self.decisions.append(d)

    def _log_decision(self, decision: dict) -> dict:
        decision = dict(decision)
        decision.setdefault("decision_id", self._gen_id("dec"))
        self._emit("decision_logged", decision)
        return decision

    # ------------------------------------------------------------------
    # 注册类命令
    # ------------------------------------------------------------------

    def register_account(self, data: dict) -> dict:
        with self._lock:
            account_id = data.get("account_id") or self._gen_id("acct")
            if account_id in self.accounts:
                raise DomainError("duplicate", f"账户 {account_id} 已存在", 409)
            kind = data.get("kind", "other")
            if kind not in ACCOUNT_KINDS:
                raise DomainError("bad_kind", f"未知账户类型 {kind}，可选: {ACCOUNT_KINDS}")
            acct = Account(account_id, data.get("name") or account_id, kind,
                           list(data.get("roles", [])), now_utc())
            self._emit("account_registered", acct.to_dict())
            return acct.to_dict()

    def register_relationship(self, data: dict) -> dict:
        with self._lock:
            for key in ("account_a", "account_b"):
                if data.get(key) not in self.accounts:
                    raise DomainError("unknown_account", f"账户不存在: {data.get(key)}", 404)
            rtype = data.get("type")
            if rtype not in self.enums["relationship_types"]:
                raise DomainError("bad_type", f"未知关系类型 {rtype}，可选: {self.enums['relationship_types']}")
            effective_from = parse_ts(data["effective_from"])
            effective_to = parse_ts(data["effective_to"]) if data.get("effective_to") else None
            if effective_to and effective_to <= effective_from:
                raise DomainError("bad_window", "effective_to 必须晚于 effective_from")
            rid = data.get("relationship_id") or self._gen_id("rel")
            if rid in self.relationships:
                raise DomainError("duplicate", f"关系 {rid} 已存在", 409)
            rel = Relationship(rid, data["account_a"], data["account_b"], rtype,
                               effective_from, effective_to)
            self._emit("relationship_registered", rel.to_dict())
            return rel.to_dict()

    def end_relationship(self, relationship_id: str, data: dict) -> dict:
        with self._lock:
            rel = self.relationships.get(relationship_id)
            if not rel:
                raise DomainError("not_found", f"关系 {relationship_id} 不存在", 404)
            if rel.effective_to is not None:
                raise DomainError("already_ended", "关系已终止", 409)
            effective_to = parse_ts(data["effective_to"])
            if effective_to <= rel.effective_from:
                raise DomainError("bad_window", "effective_to 必须晚于 effective_from")
            self._emit("relationship_ended", {"relationship_id": relationship_id,
                                              "effective_to": iso(effective_to)})
            return rel.to_dict()

    def register_lot(self, data: dict) -> dict:
        with self._lock:
            if data.get("account_id") not in self.accounts:
                raise DomainError("unknown_account", f"账户不存在: {data.get('account_id')}", 404)
            source = data.get("source")
            if source not in self.enums["restriction_sources"]:
                raise DomainError("bad_source", f"未知限售来源 {source}，可选: {self.enums['restriction_sources']}")
            quantity = int(data.get("quantity", 0))
            if quantity <= 0:
                raise DomainError("bad_quantity", "批次数量必须为正整数")
            lot_id = data.get("lot_id") or self._gen_id("lot")
            if lot_id in self.lots:
                raise DomainError("duplicate", f"批次 {lot_id} 已存在", 409)
            lot = Lot(lot_id, data["account_id"], source, quantity,
                      parse_ts(data["unlock_date"]), list(data.get("conditions", [])))
            self._emit("lot_registered", lot.to_dict())
            return lot.to_dict()

    def satisfy_lot_condition(self, lot_id: str, data: dict) -> dict:
        with self._lock:
            lot = self.lots.get(lot_id)
            if not lot:
                raise DomainError("not_found", f"批次 {lot_id} 不存在", 404)
            condition = data.get("condition")
            if condition not in lot.conditions:
                raise DomainError("bad_condition", f"批次未声明解禁条件 {condition}")
            if condition not in lot.conditions_satisfied:
                self._emit("lot_condition_satisfied", {"lot_id": lot_id, "condition": condition})
            return lot.to_dict()

    def register_rule(self, data: dict) -> dict:
        with self._lock:
            for key in ("rule_id", "version", "type", "effective_from"):
                if not data.get(key):
                    raise DomainError("missing_field", f"缺少必填字段: {key}")
            rtype = data["type"]
            if rtype not in RULE_TYPES:
                raise DomainError("bad_type", f"未知规则类型 {rtype}，可选: {RULE_TYPES}")
            params = dict(data.get("params", {}))
            self._validate_rule_params(rtype, params)
            key = f"{data['rule_id']}@{data['version']}"
            if key in self.rules:
                raise DomainError("duplicate", f"规则版本 {key} 已存在", 409)
            rule = RuleVersion(data["rule_id"], data["version"], rtype,
                               parse_ts(data["effective_from"]), params,
                               order=len(self.rules))
            self._emit("rule_registered", rule.to_dict())
            return rule.to_dict()

    @staticmethod
    def _validate_rule_params(rtype: str, params: dict) -> None:
        if rtype == "rolling_window_cap":
            if int(params.get("window_days", 0)) <= 0:
                raise DomainError("bad_params", "rolling_window_cap 需要正的 window_days")
            if "max_quantity" not in params and "max_pct_of_total_shares" not in params:
                raise DomainError("bad_params", "rolling_window_cap 需要 max_quantity 或 max_pct_of_total_shares")
        elif rtype == "sensitive_window":
            if not params.get("event_types") or not params.get("applies_to_roles"):
                raise DomainError("bad_params", "sensitive_window 需要 event_types 与 applies_to_roles")
            if int(params.get("days_before", 0)) < 0 or int(params.get("days_after", 0)) < 0:
                raise DomainError("bad_params", "days_before/days_after 不能为负")
        elif rtype == "disclosure_threshold":
            if "threshold_quantity" not in params and "threshold_pct_of_total_shares" not in params:
                raise DomainError("bad_params", "disclosure_threshold 需要 threshold_quantity 或 threshold_pct_of_total_shares")

    def register_sensitive_event(self, data: dict) -> dict:
        with self._lock:
            if not data.get("event_type") or not data.get("announce_date"):
                raise DomainError("missing_field", "需要 event_type 与 announce_date")
            roles = list(data.get("applies_to_roles", []))
            if not roles:
                raise DomainError("missing_field", "需要 applies_to_roles")
            eid = data.get("event_id") or self._gen_id("sev")
            if eid in self.sensitive_events:
                raise DomainError("duplicate", f"敏感事件 {eid} 已存在", 409)
            ev = SensitiveEvent(eid, data["event_type"], parse_ts(data["announce_date"]), roles)
            self._emit("sensitive_event_registered", ev.to_dict())
            return ev.to_dict()

    def register_window(self, data: dict) -> dict:
        with self._lock:
            if data.get("account_id") not in self.accounts:
                raise DomainError("unknown_account", f"账户不存在: {data.get('account_id')}", 404)
            start, end = parse_ts(data["start"]), parse_ts(data["end"])
            if end <= start:
                raise DomainError("bad_window", "窗口结束必须晚于开始")
            wid = data.get("window_id") or self._gen_id("win")
            if wid in self.windows:
                raise DomainError("duplicate", f"窗口 {wid} 已存在", 409)
            w = Window(wid, data["account_id"], start, end, data.get("reason", ""))
            self._emit("window_registered", w.to_dict())
            return w.to_dict()

    def set_company(self, data: dict) -> dict:
        with self._lock:
            total = int(data.get("total_shares", 0))
            if total <= 0:
                raise DomainError("bad_params", "total_shares 必须为正整数")
            self._emit("company_configured", {"total_shares": total})
            return dict(self.company)

    # ------------------------------------------------------------------
    # 计划生命周期
    # ------------------------------------------------------------------

    def _get_plan(self, plan_id: str) -> Plan:
        plan = self.plans.get(plan_id)
        if not plan:
            raise DomainError("not_found", f"计划 {plan_id} 不存在", 404)
        return plan

    def _validate_plan_payload(self, account_id: str, lot_ids: list[str],
                               planned_quantity: int, window_start: datetime,
                               window_end: datetime) -> None:
        if account_id not in self.accounts:
            raise DomainError("unknown_account", f"账户不存在: {account_id}", 404)
        if not lot_ids:
            raise DomainError("bad_params", "计划必须关联至少一个证券批次")
        total = 0
        for lid in lot_ids:
            lot = self.lots.get(lid)
            if not lot:
                raise DomainError("unknown_lot", f"批次不存在: {lid}", 404)
            if lot.account_id != account_id:
                raise DomainError("bad_params", f"批次 {lid} 不属于账户 {account_id}")
            total += lot.quantity
        if planned_quantity <= 0:
            raise DomainError("bad_params", "计划数量必须为正整数")
        if planned_quantity > total:
            raise DomainError("bad_params", f"计划数量 {planned_quantity} 超过关联批次总量 {total}")
        if window_end <= window_start:
            raise DomainError("bad_params", "计划执行窗口结束必须晚于开始")

    def create_plan(self, data: dict) -> dict:
        with self._lock:
            lot_ids = list(data.get("lot_ids", []))
            planned = int(data.get("planned_quantity", 0))
            ws, we = parse_ts(data["window_start"]), parse_ts(data["window_end"])
            self._validate_plan_payload(data.get("account_id"), lot_ids, planned, ws, we)
            plan_id = data.get("plan_id") or self._gen_id("plan")
            if plan_id in self.plans:
                raise DomainError("duplicate", f"计划 {plan_id} 已存在", 409)
            plan = Plan(plan_id, data["account_id"], lot_ids, planned, ws, we,
                        "draft", 1, now_utc())
            self._emit("plan_created", plan.to_dict())
            return plan.to_dict()

    def submit_plan(self, plan_id: str, as_of: datetime | None = None) -> dict:
        with self._lock:
            as_of = as_of or now_utc()
            plan = self._get_plan(plan_id)
            if plan.state != "draft":
                raise DomainError("bad_state", f"仅 draft 可提交送审，当前为 {plan.state}", 409)
            self._emit("plan_submitted", {"plan_id": plan_id, "state": "review", "as_of": iso(as_of)})
            return self._log_decision(self._lifecycle_decision(plan, "submit", as_of))

    def approve_plan(self, plan_id: str, as_of: datetime | None = None) -> dict:
        with self._lock:
            as_of = as_of or now_utc()
            plan = self._get_plan(plan_id)
            if plan.state != "review":
                raise DomainError("bad_state", f"仅 review 可核准，当前为 {plan.state}", 409)
            self._emit("plan_approved", {"plan_id": plan_id, "state": "active", "as_of": iso(as_of)})
            return self._log_decision(self._lifecycle_decision(plan, "approve", as_of))

    def edit_plan(self, plan_id: str, data: dict) -> dict:
        """普通编辑。送审中（review）的计划拒绝覆盖；其余可编辑状态要求
        expected_revision 与当前版本一致（乐观并发）。"""
        with self._lock:
            as_of = parse_ts(data["as_of"]) if data.get("as_of") else now_utc()
            plan = self._get_plan(plan_id)
            if plan.state == "review":
                raise DomainError("under_review",
                                  "计划送审中，不得被普通编辑覆盖；请先撤回或待审结", 409)
            if plan.state not in ("draft", "active"):
                raise DomainError("bad_state", f"{plan.state} 状态不可编辑", 409)
            expected = data.get("expected_revision")
            if expected is None or int(expected) != plan.revision:
                raise DomainError("revision_conflict",
                                  f"版本冲突：期望 {expected}，当前 {plan.revision}", 409)
            changes: dict = {}
            if "planned_quantity" in data:
                changes["planned_quantity"] = int(data["planned_quantity"])
            if "window_start" in data:
                changes["window_start"] = iso(parse_ts(data["window_start"]))
            if "window_end" in data:
                changes["window_end"] = iso(parse_ts(data["window_end"]))
            if "lot_ids" in data:
                changes["lot_ids"] = list(data["lot_ids"])
            if not changes:
                raise DomainError("bad_params", "未提供任何变更字段")
            new_qty = changes.get("planned_quantity", plan.planned_quantity)
            new_lots = changes.get("lot_ids", plan.lot_ids)
            new_ws = parse_ts(changes.get("window_start", iso(plan.window_start)))
            new_we = parse_ts(changes.get("window_end", iso(plan.window_end)))
            self._validate_plan_payload(plan.account_id, new_lots, new_qty, new_ws, new_we)
            if plan.state == "active" and new_qty < self.plan_executed(plan_id):
                raise DomainError("bad_params", "新计划数量不得低于已成交数量", 409)
            new_revision = plan.revision + 1
            self._emit("plan_edited", {"plan_id": plan_id, "changes": changes,
                                       "revision": new_revision, "as_of": iso(as_of)})
            return self._log_decision(self._lifecycle_decision(plan, "edit", as_of,
                                                               extra={"changes": changes}))

    def suspend_plan(self, plan_id: str, data: dict) -> dict:
        with self._lock:
            as_of = parse_ts(data["as_of"]) if data.get("as_of") else now_utc()
            plan = self._get_plan(plan_id)
            if plan.state != "active":
                raise DomainError("bad_state", f"仅 active 可暂停，当前为 {plan.state}", 409)
            reason = data.get("reason", "人工暂停")
            self._emit("plan_suspended", {"plan_id": plan_id, "state": "suspended",
                                          "reason": reason, "automatic": False,
                                          "as_of": iso(as_of)})
            return self._log_decision(self._lifecycle_decision(
                plan, "suspend", as_of, extra={"reason": reason}))

    def resume_plan(self, plan_id: str, as_of: datetime | None = None) -> dict:
        with self._lock:
            as_of = as_of or now_utc()
            self._sweep(as_of)
            plan = self._get_plan(plan_id)
            if plan.state != "suspended":
                raise DomainError("bad_state", f"仅 suspended 可恢复，当前为 {plan.state}", 409)
            overdue = [a for a in self.announcements.values()
                       if a.plan_id == plan_id and a.status == "overdue"]
            if overdue:
                raise DomainError("overdue_disclosure",
                                  "存在逾期未确认公告，须先确认: "
                                  + ", ".join(a.announcement_id for a in overdue), 409)
            self._emit("plan_resumed", {"plan_id": plan_id, "state": "active", "as_of": iso(as_of)})
            return self._log_decision(self._lifecycle_decision(plan, "resume", as_of))

    def close_plan(self, plan_id: str, as_of: datetime | None = None) -> dict:
        with self._lock:
            as_of = as_of or now_utc()
            plan = self._get_plan(plan_id)
            if plan.state == "closed":
                raise DomainError("bad_state", "计划已终止", 409)
            self._emit("plan_closed", {"plan_id": plan_id, "state": "closed", "as_of": iso(as_of)})
            executed = self.plan_executed(plan_id)
            return self._log_decision(self._lifecycle_decision(
                plan, "close", as_of,
                extra={"final_executed": executed,
                       "summary": f"计划终止，累计成交 {executed} / 计划 {plan.planned_quantity}"}))

    def _lifecycle_decision(self, plan: Plan, action: str, as_of: datetime,
                            extra: dict | None = None) -> dict:
        """提交/变更/核准/暂停/恢复/终止时的可解释结论。"""
        warnings = []
        conflicts = [w for w in self.blackout_windows(plan.account_id, as_of)
                     if w["start"] <= plan.window_end and w["end"] >= plan.window_start]
        if conflicts:
            warnings.append(f"计划执行期间与 {len(conflicts)} 个限制窗口相交，执行时将按日拦截")
        locked = []
        for lid in plan.lot_ids:
            ok, reasons = self.lots[lid].unlock_status(as_of)
            if not ok:
                locked.append({"lot_id": lid, "reasons": reasons})
        if locked:
            warnings.append("部分批次尚未解禁，执行时按批次解禁状态核算")
        decision = {
            "plan_id": plan.plan_id,
            "action": action,
            "as_of": iso(as_of),
            "allowed": True,
            "reasons": [f"{action} 已受理"],
            "warnings": warnings,
            "windows": conflicts,
            "locked_lots": locked,
            "rule_evidence": self._rule_evidence(plan, as_of),
            "detail": extra or {},
        }
        return decision

    # ------------------------------------------------------------------
    # 成交回报
    # ------------------------------------------------------------------

    def record_execution(self, data: dict) -> dict:
        """记录成交回报。report_id 幂等：同一回报重复提交不重复扣减。"""
        with self._lock:
            report_id = data.get("report_id")
            if not report_id:
                raise DomainError("missing_field", "成交回报必须携带 report_id 作为幂等键")
            existing = self.executions.get(report_id)
            if existing:
                mismatch = []
                if data.get("plan_id") and data["plan_id"] != existing.plan_id:
                    mismatch.append("plan_id")
                if data.get("quantity") is not None and int(data["quantity"]) != existing.quantity:
                    mismatch.append("quantity")
                return {"duplicate": True, "mismatch_fields": mismatch,
                        "execution": existing.to_dict(), "decision": None}

            plan = self._get_plan(data.get("plan_id"))
            quantity = int(data.get("quantity", 0))
            trade_time = parse_ts(data["trade_time"])
            as_of = parse_ts(data["as_of"]) if data.get("as_of") else now_utc()
            self._sweep(as_of)

            decision = self._evaluate(plan, quantity, trade_time, as_of, "execute")
            decision["detail"]["report_id"] = report_id
            if not decision["allowed"]:
                logged = self._log_decision(decision)
                raise DomainError("execution_rejected", "成交回报未通过放行核算", 409,
                                  payload={"decision": logged})

            attribution = self.merge_group(plan.account_id, trade_time)
            allocations = self._allocate(plan, quantity, trade_time)
            self._emit("execution_recorded", {
                "report_id": report_id,
                "plan_id": plan.plan_id,
                "account_id": plan.account_id,
                "quantity": quantity,
                "trade_time": iso(trade_time),
                "attribution": attribution,
                "allocations": allocations,
                "seq": self._seq + 1,
            })
            created = self._maybe_create_announcements(plan, trade_time, as_of)
            decision["detail"]["attribution"] = attribution
            decision["detail"]["allocations"] = allocations
            decision["detail"]["announcements_created"] = [a.announcement_id for a in created]
            logged = self._log_decision(decision)
            return {"duplicate": False, "execution": self.executions[report_id].to_dict(),
                    "decision": logged}

    def revoke_execution(self, report_id: str, data: dict) -> dict:
        """撤回成交回报（补偿事件）。重复撤回为幂等空操作。"""
        with self._lock:
            ex = self.executions.get(report_id)
            if not ex:
                raise DomainError("not_found", f"成交回报 {report_id} 不存在", 404)
            if ex.revoked:
                return {"duplicate": True, "execution": ex.to_dict(), "decision": None}
            as_of = parse_ts(data["as_of"]) if data.get("as_of") else now_utc()
            reason = data.get("reason", "")
            self._emit("execution_revoked", {"report_id": report_id,
                                             "reason": reason, "as_of": iso(as_of)})
            decision = self._log_decision({
                "plan_id": ex.plan_id,
                "action": "revoke_execution",
                "as_of": iso(as_of),
                "allowed": True,
                "reasons": [f"回报 {report_id} 已撤回，累计额度重放时不再计入"],
                "warnings": [],
                "windows": [],
                "rule_evidence": self._rule_evidence(self.plans[ex.plan_id], as_of),
                "detail": {"report_id": report_id, "released_quantity": ex.quantity,
                           "reason": reason},
            })
            return {"duplicate": False, "execution": ex.to_dict(), "decision": decision}

    def _allocate(self, plan: Plan, quantity: int, trade_time: datetime) -> list[dict]:
        """按计划内批次解禁时间先后 FIFO 分摊成交，仅使用成交时点已解禁批次。"""
        lots = sorted((self.lots[lid] for lid in plan.lot_ids),
                      key=lambda l: (l.unlock_date, l.lot_id))
        remaining = quantity
        allocations = []
        for lot in lots:
            if remaining <= 0:
                break
            ok, _ = lot.unlock_status(trade_time)
            if not ok:
                continue
            avail = lot.quantity - self.lot_consumed(lot.lot_id)
            take = min(avail, remaining)
            if take > 0:
                allocations.append({"lot_id": lot.lot_id, "quantity": take})
                remaining -= take
        if remaining > 0:  # 核算层已拦截，此处为防御
            raise DomainError("allocation_failed", "批次可用量不足，无法分摊成交", 409)
        return allocations

    # ------------------------------------------------------------------
    # 放行核算（可解释）
    # ------------------------------------------------------------------

    def check_execution(self, plan_id: str, data: dict) -> dict:
        """提交前试算：不记录成交，只返回可解释的放行结论。"""
        with self._lock:
            plan = self._get_plan(plan_id)
            quantity = int(data.get("quantity", 0))
            trade_time = parse_ts(data["trade_time"])
            as_of = parse_ts(data["as_of"]) if data.get("as_of") else now_utc()
            self._sweep(as_of)
            return self._evaluate(plan, quantity, trade_time, as_of, "check")

    def _evaluate(self, plan: Plan, quantity: int, trade_time: datetime,
                  as_of: datetime, action: str) -> dict:
        reasons: list[str] = []
        allowed = True

        if quantity <= 0:
            allowed = False
            reasons.append("申报数量必须为正整数")
        if plan.state != "active":
            allowed = False
            reasons.append(f"计划状态为 {plan.state}，仅 active 可执行")
        overdue = [a for a in self.announcements.values()
                   if a.plan_id == plan.plan_id and a.status == "overdue"]
        if overdue:
            allowed = False
            reasons.append("存在逾期未确认公告: "
                           + ", ".join(a.announcement_id for a in overdue))
        if not (plan.window_start <= trade_time <= plan.window_end):
            allowed = False
            reasons.append(f"成交时间 {iso(trade_time)} 不在计划执行期间 "
                           f"[{iso(plan.window_start)}, {iso(plan.window_end)}] 内")

        hits = [w for w in self.blackout_windows(plan.account_id, trade_time)
                if w["start"] <= trade_time <= w["end"]]
        if hits:
            allowed = False
            reasons.append(f"成交时间落入 {len(hits)} 个限制窗口")

        layers, final = self._layers(plan, trade_time)
        if quantity > final:
            allowed = False
            reasons.append(f"申报数量 {quantity} 超过可减数量 {final}（最严格层限制）")
        if not reasons:
            reasons.append("各层核算与窗口检查均通过")

        return {
            "plan_id": plan.plan_id,
            "action": action,
            "as_of": iso(as_of),
            "trade_time": iso(trade_time),
            "requested_quantity": quantity,
            "allowed": allowed,
            "reasons": reasons,
            "reducible_quantity": final,
            "layers": layers,
            "windows": hits,
            "contributions": self._contributions_for_rules(plan.account_id, trade_time),
            "pending_disclosures": [a.to_dict() for a in self.announcements.values()
                                    if a.plan_id == plan.plan_id and a.status != "confirmed"],
            "rule_evidence": self._rule_evidence(plan, trade_time),
            "detail": {},
        }

    def _layers(self, plan: Plan, at: datetime) -> tuple[list[dict], int]:
        """逐层计算可减数量：计划剩余 → 批次可用 → 各数量规则剩余。取最小（最严格）。"""
        layers: list[dict] = []
        executed = self.plan_executed(plan.plan_id)
        plan_remaining = plan.planned_quantity - executed
        layers.append({
            "layer": "plan_remaining",
            "quantity": plan_remaining,
            "detail": {"planned_quantity": plan.planned_quantity, "executed": executed},
        })

        lot_entries = []
        lot_total = 0
        for lid in plan.lot_ids:
            lot = self.lots[lid]
            consumed = self.lot_consumed(lid)
            remaining = lot.quantity - consumed
            unlocked, lock_reasons = lot.unlock_status(at)
            available = remaining if unlocked else 0
            lot_total += available
            lot_entries.append({
                "lot_id": lid, "source": lot.source, "quantity": lot.quantity,
                "consumed": consumed, "remaining": remaining,
                "unlocked": unlocked, "available": available,
                "lock_reasons": lock_reasons,
            })
        layers.append({"layer": "lot_availability", "quantity": lot_total, "lots": lot_entries})

        for cap in self._rule_caps(plan.account_id, at):
            layers.append({
                "layer": "rule_cap",
                "rule_id": cap["rule_id"],
                "version": cap["version"],
                "quantity": cap["remaining"],
                "detail": {"cap": cap["cap"], "consumed": cap["consumed"],
                           "window": cap["window"]},
            })

        final = max(0, min(l["quantity"] for l in layers))
        for l in layers:
            l["binding"] = (l["quantity"] == final)
        return layers, final

    # ------------------------------------------------------------------
    # 公告流程
    # ------------------------------------------------------------------

    def _maybe_create_announcements(self, plan: Plan, trade_time: datetime,
                                    as_of: datetime) -> list[Announcement]:
        created = []
        cumulative = self.plan_executed(plan.plan_id)
        for rule in self.effective_rules("disclosure_threshold", trade_time):
            threshold, note = self._threshold_quantity(rule)
            if threshold is None:
                continue
            if threshold <= 0:
                continue
            crossings = cumulative // threshold
            existing = {a.crossing_no for a in self.announcements.values()
                        if a.plan_id == plan.plan_id and a.rule_id == rule.rule_id}
            for n in range(1, int(crossings) + 1):
                if n in existing:
                    continue
                deadline_days = int(rule.params.get("deadline_days", 2))
                ann = Announcement(
                    self._gen_id("ann"), plan.plan_id, rule.rule_id, rule.version,
                    n, threshold, as_of, as_of + timedelta(days=deadline_days))
                self._emit("announcement_created", ann.to_dict())
                created.append(ann)
        return created

    def _threshold_quantity(self, rule: RuleVersion) -> tuple[int | None, str | None]:
        p = rule.params
        if "threshold_quantity" in p:
            return int(p["threshold_quantity"]), None
        total = self.company.get("total_shares")
        if not total:
            return None, "total_shares 未配置，无法换算比例阈值"
        return int(total * float(p["threshold_pct_of_total_shares"]) / 100), None

    def confirm_announcement(self, announcement_id: str, data: dict) -> dict:
        with self._lock:
            ann = self.announcements.get(announcement_id)
            if not ann:
                raise DomainError("not_found", f"公告 {announcement_id} 不存在", 404)
            if ann.status == "confirmed":
                return {"duplicate": True, "announcement": ann.to_dict(), "decision": None}
            as_of = parse_ts(data["as_of"]) if data.get("as_of") else now_utc()
            was_overdue = ann.status == "overdue"
            self._emit("announcement_confirmed", {"announcement_id": announcement_id,
                                                  "as_of": iso(as_of)})
            decision = self._log_decision({
                "plan_id": ann.plan_id,
                "action": "confirm_announcement",
                "as_of": iso(as_of),
                "allowed": True,
                "reasons": [f"公告 {announcement_id} 已确认"
                            + ("（逾期后补确认，计划可申请恢复）" if was_overdue else "")],
                "warnings": [],
                "windows": [],
                "rule_evidence": [],
                "detail": {"announcement_id": announcement_id, "was_overdue": was_overdue},
            })
            return {"duplicate": False, "announcement": ann.to_dict(), "decision": decision}

    def list_announcements(self, status: str | None = None,
                           as_of: datetime | None = None) -> list[dict]:
        with self._lock:
            if as_of:
                self._sweep(as_of)
            out = [a.to_dict() for a in self.announcements.values()
                   if status is None or a.status == status]
            return sorted(out, key=lambda a: a["announcement_id"])

    def _sweep(self, as_of: datetime) -> list[str]:
        """逾期扫描：pending 公告超过 deadline 即标记 overdue，并自动暂停其计划。"""
        affected = []
        for ann in sorted(self.announcements.values(), key=lambda a: a.announcement_id):
            if ann.status == "pending" and ann.deadline < as_of:
                self._emit("announcement_overdue", {"announcement_id": ann.announcement_id,
                                                    "as_of": iso(as_of)})
                plan = self.plans.get(ann.plan_id)
                if plan and plan.state == "active":
                    self._emit("plan_suspended", {
                        "plan_id": plan.plan_id, "state": "suspended",
                        "reason": f"公告 {ann.announcement_id} 逾期未确认，自动暂停后续放行",
                        "automatic": True, "as_of": iso(as_of)})
                    self._log_decision({
                        "plan_id": plan.plan_id,
                        "action": "auto_suspend",
                        "as_of": iso(as_of),
                        "allowed": True,
                        "reasons": [f"公告 {ann.announcement_id} 超过时限 {iso(ann.deadline)} "
                                    "未确认，暂停后续放行"],
                        "warnings": [], "windows": [], "rule_evidence": [],
                        "detail": {"announcement_id": ann.announcement_id},
                    })
                affected.append(ann.announcement_id)
        return affected

    def sweep(self, as_of: datetime | None = None) -> dict:
        with self._lock:
            as_of = as_of or now_utc()
            return {"as_of": iso(as_of), "overdue": self._sweep(as_of)}

    # ------------------------------------------------------------------
    # 派生计算：合并组 / 消耗 / 窗口 / 规则
    # ------------------------------------------------------------------

    def merge_group(self, account_id: str, at: datetime) -> list[str]:
        """at 时点账户所在的合并组（关系连通分量，含自身）。"""
        parent = {a: a for a in self.accounts}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for rel in self.relationships.values():
            if rel.active_at(at) and rel.account_a in parent and rel.account_b in parent:
                ra, rb = find(rel.account_a), find(rel.account_b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
        root = find(account_id)
        return sorted(a for a in self.accounts if find(a) == root)

    def _active_executions(self) -> list[Execution]:
        """未撤回成交，按 (成交时间, 记录序号) 排序 —— 重放顺序与到达顺序无关。"""
        return sorted((e for e in self.executions.values() if not e.revoked),
                      key=lambda e: (e.trade_time, e.seq))

    def consumption(self, account_id: str, start: datetime, end: datetime) -> int:
        """账户在 [start, end] 的合并消耗：归属（成交时冻结）含该账户的成交之和。"""
        return sum(e.quantity for e in self._active_executions()
                   if start <= e.trade_time <= end and account_id in e.attribution)

    def plan_executed(self, plan_id: str) -> int:
        return sum(e.quantity for e in self._active_executions() if e.plan_id == plan_id)

    def lot_consumed(self, lot_id: str) -> int:
        return sum(a["quantity"] for e in self._active_executions()
                   for a in e.allocations if a["lot_id"] == lot_id)

    def effective_rules(self, rule_type: str, at: datetime) -> list[RuleVersion]:
        """at 时点已生效的各 rule_id 最新版本。"""
        best: dict[str, RuleVersion] = {}
        for r in self.rules.values():
            if r.type != rule_type or r.effective_from > at:
                continue
            cur = best.get(r.rule_id)
            if cur is None or (r.effective_from, r.order) > (cur.effective_from, cur.order):
                best[r.rule_id] = r
        return sorted(best.values(), key=lambda r: (r.rule_id, r.version))

    def _rule_caps(self, account_id: str, at: datetime) -> list[dict]:
        caps = []
        for rule in self.effective_rules("rolling_window_cap", at):
            p = rule.params
            window_days = int(p["window_days"])
            start = at - timedelta(days=window_days)
            values, notes = {}, []
            if "max_quantity" in p:
                values["max_quantity"] = int(p["max_quantity"])
            if "max_pct_of_total_shares" in p:
                total = self.company.get("total_shares")
                if total:
                    values["max_pct_of_total_shares"] = int(
                        total * float(p["max_pct_of_total_shares"]) / 100)
                else:
                    values["max_pct_of_total_shares"] = 0
                    notes.append("total_shares 未配置，比例上限按 0 处理（从严）")
            cap = min(values.values())
            consumed = self.consumption(account_id, start, at)
            caps.append({
                "rule_id": rule.rule_id, "version": rule.version,
                "effective_from": iso(rule.effective_from), "params": dict(p),
                "window": [iso(start), iso(at)],
                "cap": cap, "consumed": consumed, "remaining": cap - consumed,
                "notes": notes,
            })
        return caps

    def blackout_windows(self, account_id: str, at: datetime) -> list[dict]:
        """账户的限制窗口：人工登记窗口 + 敏感事件按规则版本生成的窗口。"""
        out = []
        for w in self.windows.values():
            if w.account_id == account_id:
                out.append({"start": w.start, "end": w.end, "reason": w.reason,
                            "source": {"kind": "explicit", "window_id": w.window_id}})
        account = self.accounts.get(account_id)
        if not account:
            return out
        for rule in self.effective_rules("sensitive_window", at):
            p = rule.params
            for ev in self.sensitive_events.values():
                if ev.event_type not in p.get("event_types", []):
                    continue
                if not set(account.roles) & set(p.get("applies_to_roles", [])):
                    continue
                start = ev.announce_date - timedelta(days=int(p.get("days_before", 0)))
                end = ev.announce_date + timedelta(days=int(p.get("days_after", 0)))
                out.append({
                    "start": start, "end": end,
                    "reason": f"敏感事件 {ev.event_type}（公告日 {iso(ev.announce_date)}）",
                    "source": {"kind": "sensitive", "rule_id": rule.rule_id,
                               "version": rule.version, "event_id": ev.event_id,
                               "event_type": ev.event_type},
                })
        return out

    def _contributions_for_rules(self, account_id: str, at: datetime) -> list[dict]:
        """各数量规则窗口内，归属含该账户的成交按执行账户分解（关联账户贡献）。"""
        out = []
        for cap in self._rule_caps(account_id, at):
            start, end = parse_ts(cap["window"][0]), parse_ts(cap["window"][1])
            by: dict[str, int] = {}
            for e in self._active_executions():
                if start <= e.trade_time <= end and account_id in e.attribution:
                    by[e.account_id] = by.get(e.account_id, 0) + e.quantity
            out.append({
                "rule_id": cap["rule_id"], "version": cap["version"],
                "window": cap["window"],
                "by_account": [{"account_id": k, "quantity": v}
                               for k, v in sorted(by.items())],
                "total": sum(by.values()),
            })
        return out

    def _rule_evidence(self, plan: Plan, at: datetime) -> list[dict]:
        """本次决定采用的规则证据：版本、参数与核算结果。"""
        evidence: list[dict] = []
        for cap in self._rule_caps(plan.account_id, at):
            evidence.append({
                "rule_id": cap["rule_id"], "version": cap["version"],
                "type": "rolling_window_cap", "effective_from": cap["effective_from"],
                "params": cap["params"],
                "outcome": {"window": cap["window"], "cap": cap["cap"],
                            "consumed": cap["consumed"], "remaining": cap["remaining"],
                            "notes": cap["notes"]},
            })
        for rule in self.effective_rules("sensitive_window", at):
            matched = [
                {"event_id": ev.event_id, "event_type": ev.event_type,
                 "announce_date": iso(ev.announce_date)}
                for ev in self.sensitive_events.values()
                if ev.event_type in rule.params.get("event_types", [])
                and set(self.accounts[plan.account_id].roles) & set(rule.params.get("applies_to_roles", []))
            ]
            evidence.append({
                "rule_id": rule.rule_id, "version": rule.version,
                "type": "sensitive_window", "effective_from": iso(rule.effective_from),
                "params": dict(rule.params), "outcome": {"matched_events": matched},
            })
        cumulative = self.plan_executed(plan.plan_id)
        for rule in self.effective_rules("disclosure_threshold", at):
            threshold, note = self._threshold_quantity(rule)
            evidence.append({
                "rule_id": rule.rule_id, "version": rule.version,
                "type": "disclosure_threshold", "effective_from": iso(rule.effective_from),
                "params": dict(rule.params),
                "outcome": {"plan_cumulative": cumulative, "threshold_quantity": threshold,
                            "note": note},
            })
        return evidence

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def plan_view(self, plan_id: str, as_of: datetime | None = None) -> dict:
        """负责人视图：逐层可减数量、冲突窗口、关联账户贡献、待披露事项、决策证据。"""
        with self._lock:
            as_of = as_of or now_utc()
            self._sweep(as_of)
            plan = self._get_plan(plan_id)
            layers, final = self._layers(plan, as_of)
            conflicts = [self._window_dict(w) for w in self.blackout_windows(plan.account_id, as_of)
                         if w["start"] <= plan.window_end and w["end"] >= plan.window_start]
            overdue = [a for a in self.announcements.values()
                       if a.plan_id == plan_id and a.status == "overdue"]
            blocks = {
                "plan_state": plan.state,
                "in_blackout_window_now": any(
                    w["start"] <= as_of <= w["end"]
                    for w in self.blackout_windows(plan.account_id, as_of)),
                "overdue_announcements": [a.announcement_id for a in overdue],
            }
            return {
                "plan": plan.to_dict(),
                "as_of": iso(as_of),
                "merge_group": {"resolved_at": iso(as_of),
                                "members": self.merge_group(plan.account_id, as_of)},
                "reducible": {"final": final, "layers": layers, "blocks": blocks},
                "conflicting_windows": conflicts,
                "contributions": self._contributions_for_rules(plan.account_id, as_of),
                "pending_disclosures": [a.to_dict() for a in self.announcements.values()
                                        if a.plan_id == plan_id and a.status != "confirmed"],
                "executions": [e.to_dict() for e in self._active_executions()
                               if e.plan_id == plan_id]
                              + [e.to_dict() for e in self.executions.values()
                                 if e.plan_id == plan_id and e.revoked],
                "decisions": [d for d in self.decisions if d.get("plan_id") == plan_id],
            }

    @staticmethod
    def _window_dict(w: dict) -> dict:
        return {"start": iso(w["start"]), "end": iso(w["end"]),
                "reason": w["reason"], "source": w["source"]}

    def plan_decisions(self, plan_id: str) -> list[dict]:
        self._get_plan(plan_id)
        return [d for d in self.decisions if d.get("plan_id") == plan_id]

    def list_plans(self) -> list[dict]:
        return [p.to_dict() for p in sorted(self.plans.values(), key=lambda p: p.plan_id)]

    def list_accounts(self) -> list[dict]:
        return [a.to_dict() for a in sorted(self.accounts.values(), key=lambda a: a.account_id)]

    def list_rules(self) -> list[dict]:
        return [r.to_dict() for r in sorted(self.rules.values(),
                                            key=lambda r: (r.rule_id, r.version))]


def load_enums(reference_dir: str | Path | None = None) -> dict:
    """从 reference/domain.json 读取公开枚举（缺失时回退内置默认值）。"""
    import json

    path = Path(reference_dir) / "domain.json" if reference_dir else None
    if path and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}
