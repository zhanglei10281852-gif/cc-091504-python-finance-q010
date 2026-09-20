"""合规服务门面：所有命令与查询的唯一入口。

设计约定：

* **状态机锁**：``draft -> review -> active -> suspended/closed``；送审中的计划
  （``submitted_version`` 非空）拒绝普通编辑，变更必须走变更送审流程；
* **生效后原则**：计划新版本与关系变更只影响生效日之后的计算，历史成交按
  成交当日的归属与规则保留；
* **成交重放**：每次查询/命令都从事件日志重放，成交按业务日排序，重复
  ``report_id`` 拒绝、撤回/替换回报不参与累计；
* **最严格结果**：多个数量层同时命中时取最小余量；
* **披露闸门**：预披露与阶梯减持公告有时限，逾期未确认暂停执行放行。
"""

from __future__ import annotations

import threading
import uuid
from datetime import date
from typing import Any

from compliance.dates import add_days, iso, parse_date
from compliance.errors import (
    NotFoundError,
    PlanLockedError,
    StateConflictError,
    ValidationError,
)
from compliance.projection import State, replay
from compliance.rules import (
    compute_layers,
    conflict_windows,
    evaluate_execution,
    gating_blockers,
    pending_disclosures,
    relationship_evidence,
)
from compliance.store import EventStore

PLAN_CHANNELS = {"secondary", "block"}
TERMINAL_DAYS = "9999-12-31"


class ComplianceService:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._lock = threading.RLock()

    # ==================================================================
    # 内部工具
    # ==================================================================

    def _state(self) -> State:
        return replay(self.store.all_events())

    @staticmethod
    def _require(payload: dict[str, Any], fields: list[str]) -> None:
        for f in fields:
            if f not in payload or payload[f] in (None, ""):
                raise ValidationError(f"缺少必填字段: {f}")

    @staticmethod
    def _qty(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"{field} 必须是非负整数")
        return value

    def _plan(self, state: State, plan_id: str):
        plan = state.plans.get(plan_id)
        if plan is None:
            raise NotFoundError(f"减持计划不存在: {plan_id}")
        return plan

    def _new_version(
        self, plan, state: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        prev = plan.current
        return {
            "version_no": (prev.version_no + 1 if prev else 1),
            "state": state,
            "effective_from": fields.get("effective_from", prev.effective_from),
            "effective_to": fields.get("effective_to", prev.effective_to),
            "proposed_qty": fields.get("proposed_qty", prev.proposed_qty),
            "channels": fields.get("channels", list(prev.channels)),
            "security_code": plan.security_code,
            "reason": fields.get("reason", prev.reason),
            "created_by": fields.get("created_by", prev.created_by),
        }

    def _record_decision(
        self,
        state: State,
        plan,
        action: str,
        evaluation: dict[str, Any],
        business_date: str,
    ) -> None:
        rule = evaluation.get("layers", {}).get("rule")
        self.store.append(
            plan.plan_id,
            "decision_recorded",
            {
                "decision_id": uuid.uuid4().hex,
                "plan_id": plan.plan_id,
                "action": action,
                "business_date": business_date,
                "result": evaluation["result"],
                "reasons": evaluation.get("reasons", []),
                "allowed_qty": evaluation.get("allowed_qty"),
                "binding_layer": evaluation.get("binding_layer"),
                "rule": rule,
                "layers": evaluation.get("layers"),
                "conflict_windows": evaluation.get("conflict_windows"),
            },
            business_time=business_date,
        )

    # ==================================================================
    # 基础数据：股东 / 关系 / 批次 / 解禁 / 窗口 / 规则
    # ==================================================================

    def register_shareholder(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(payload, ["name", "kind"])
            sid = payload.get("shareholder_id") or uuid.uuid4().hex
            data = {
                "shareholder_id": sid,
                "name": payload["name"],
                "kind": payload["kind"],
                "is_controlling_person": bool(payload.get("is_controlling_person", False)),
                "is_director": bool(payload.get("is_director", False)),
            }
            self.store.append(
                f"shareholder:{sid}", "shareholder_registered", data,
                expected_version=0,
            )
            return data

    def declare_relationship(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            self._require(
                payload,
                ["subject_id", "group_owner_id", "rel_type", "effective_from"],
            )
            if payload["subject_id"] == payload["group_owner_id"]:
                raise ValidationError("关系主体不能与组合所有人相同")
            if payload["rel_type"] not in {
                "controller", "family", "employee_platform",
            }:
                raise ValidationError(f"未知关系类型: {payload['rel_type']}")
            for sid in (payload["subject_id"], payload["group_owner_id"]):
                if sid not in state.shareholders:
                    raise ValidationError(f"股东未登记: {sid}")
            parse_date(payload["effective_from"], "effective_from")
            if payload.get("effective_to"):
                parse_date(payload["effective_to"], "effective_to")
                if payload["effective_to"] <= payload["effective_from"]:
                    raise ValidationError("effective_to 必须晚于 effective_from")
            rel_id = payload.get("rel_id") or uuid.uuid4().hex
            data = {
                "rel_id": rel_id,
                "subject_id": payload["subject_id"],
                "group_owner_id": payload["group_owner_id"],
                "rel_type": payload["rel_type"],
                "effective_from": payload["effective_from"],
                "effective_to": payload.get("effective_to"),
                "note": payload.get("note", ""),
            }
            self.store.append(
                f"relationship:{rel_id}", "relationship_declared", data,
                expected_version=0,
            )
            return data

    def end_relationship(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(payload, ["rel_id", "effective_to"])
            state = self._state()
            rel = state.relationships.get(payload["rel_id"])
            if rel is None:
                raise NotFoundError(f"关系不存在: {payload['rel_id']}")
            if rel.effective_to is not None:
                raise StateConflictError(f"关系 {rel.rel_id} 已终止")
            day = payload["effective_to"]
            parse_date(day)
            if day <= rel.effective_from:
                raise ValidationError("终止日必须晚于关系生效日")
            self.store.append(
                f"relationship:{rel.rel_id}",
                "relationship_ended",
                {"rel_id": rel.rel_id, "effective_to": day},
                expected_version=self.store.stream_version(
                    f"relationship:{rel.rel_id}"
                ),
                business_time=day,
            )
            return {"rel_id": rel.rel_id, "effective_to": day}

    def record_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            self._require(
                payload,
                ["shareholder_id", "security_code", "source", "total_qty"],
            )
            if payload["shareholder_id"] not in state.shareholders:
                raise ValidationError(f"股东未登记: {payload['shareholder_id']}")
            if payload["source"] not in {
                "IPO_lockup", "director_holding", "private_placement",
                "incentive_award",
            }:
                raise ValidationError(f"未知限售来源: {payload['source']}")
            total = self._qty(payload["total_qty"], "total_qty")
            if payload.get("issuer_lockup_until"):
                parse_date(payload["issuer_lockup_until"], "issuer_lockup_until")
            bid = payload.get("batch_id") or uuid.uuid4().hex
            data = {
                "batch_id": bid,
                "shareholder_id": payload["shareholder_id"],
                "security_code": payload["security_code"],
                "source": payload["source"],
                "total_qty": total,
                "issuer_lockup_until": payload.get("issuer_lockup_until"),
                "note": payload.get("note", ""),
            }
            self.store.append(
                f"batch:{bid}", "batch_recorded", data, expected_version=0,
            )
            return data

    def record_unlock(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            self._require(
                payload,
                ["batch_id", "condition_type", "unlock_date", "tranche_qty"],
            )
            batch = state.batches.get(payload["batch_id"])
            if batch is None:
                raise ValidationError(f"证券批次不存在: {payload['batch_id']}")
            if payload["condition_type"] not in {"date", "performance"}:
                raise ValidationError("condition_type 只能是 date / performance")
            parse_date(payload["unlock_date"], "unlock_date")
            qty = self._qty(payload["tranche_qty"], "tranche_qty")
            total_unlocked = sum(
                u.tranche_qty
                for u in state.unlocks.values()
                if u.batch_id == batch.batch_id
            )
            if total_unlocked + qty > batch.total_qty:
                raise ValidationError(
                    f"解禁数量合计 {total_unlocked + qty} 超过批次总量 "
                    f"{batch.total_qty}"
                )
            uid = payload.get("unlock_id") or uuid.uuid4().hex
            data = {
                "unlock_id": uid,
                "batch_id": batch.batch_id,
                "condition_type": payload["condition_type"],
                "unlock_date": payload["unlock_date"],
                "tranche_qty": qty,
                "satisfied": bool(payload.get("satisfied", True)),
                "note": payload.get("note", ""),
            }
            self.store.append(
                f"unlock:{uid}", "unlock_recorded", data, expected_version=0,
            )
            return data

    def declare_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(payload, ["security_code", "title", "start_date", "end_date"])
            start = parse_date(payload["start_date"], "start_date")
            end = parse_date(payload["end_date"], "end_date")
            if end < start:
                raise ValidationError("窗口结束日早于开始日")
            wid = payload.get("window_id") or uuid.uuid4().hex
            data = {
                "window_id": wid,
                "security_code": payload["security_code"],
                "title": payload["title"],
                "start_date": iso(start),
                "end_date": iso(end),
                "blocking": bool(payload.get("blocking", True)),
                "note": payload.get("note", ""),
            }
            self.store.append(
                f"window:{wid}", "window_declared", data, expected_version=0,
                business_time=iso(start),
            )
            return data

    def publish_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            self._require(payload, ["version", "effective_from", "name"])
            rid = payload.get("rule_id", "CSRC_REDUCTION")
            effective_from = payload["effective_from"]
            parse_date(effective_from, "effective_from")
            if payload.get("effective_to"):
                parse_date(payload["effective_to"], "effective_to")
            current = state.rules.get(rid)
            if (
                current is not None
                and current.effective_to is None
                and effective_from <= current.effective_from
            ):
                raise ValidationError(
                    f"规则 {rid} 现行版本自 {current.effective_from} 生效，"
                    "新版本生效日必须更晚；如需替换请先关闭旧版本"
                )
            data = {
                "rule_id": rid,
                "version": payload["version"],
                "effective_from": effective_from,
                "effective_to": payload.get("effective_to"),
                "name": payload["name"],
                "controller_calendar_days": int(
                    payload.get("controller_calendar_days", 90)
                ),
                "controller_secondary_pct": float(
                    payload.get("controller_secondary_pct", 1.0)
                ),
                "controller_block_pct": float(
                    payload.get("controller_block_pct", 2.0)
                ),
                "director_annual_pct": float(payload.get("director_annual_pct", 25.0)),
                "pre_disclosure_threshold_pct": float(
                    payload.get("pre_disclosure_threshold_pct", 1.0)
                ),
                "holding_disclosure_step_pct": float(
                    payload.get("holding_disclosure_step_pct", 1.0)
                ),
                "announcement_confirm_days": int(
                    payload.get("announcement_confirm_days", 2)
                ),
                "note": payload.get("note", ""),
            }
            self.store.append(
                f"rule:{rid}", "rule_published", data,
                business_time=effective_from,
            )
            return data

    # ==================================================================
    # 计划生命周期
    # ==================================================================

    def create_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            self._require(
                payload,
                ["group_owner_id", "security_code", "share_capital",
                 "effective_from", "proposed_qty", "channels"],
            )
            owner = payload["group_owner_id"]
            if owner not in state.shareholders:
                raise ValidationError(f"组合所有人（股东）未登记: {owner}")
            qty = self._qty(payload["proposed_qty"], "proposed_qty")
            if qty <= 0:
                raise ValidationError("proposed_qty 必须为正整数")
            channels = payload["channels"]
            if not isinstance(channels, list) or not channels or any(
                c not in PLAN_CHANNELS for c in channels
            ):
                raise ValidationError("channels 必须是 secondary/block 的非空列表")
            parse_date(payload["effective_from"], "effective_from")
            if payload.get("effective_to"):
                parse_date(payload["effective_to"], "effective_to")
                if payload["effective_to"] < payload["effective_from"]:
                    raise ValidationError("effective_to 早于 effective_from")
            capital = self._qty(payload["share_capital"], "share_capital")
            if capital <= 0:
                raise ValidationError("share_capital 必须为正整数")
            plan_id = payload.get("plan_id") or uuid.uuid4().hex
            version = {
                "version_no": 1,
                "state": "draft",
                "effective_from": payload["effective_from"],
                "effective_to": payload.get("effective_to"),
                "proposed_qty": qty,
                "channels": channels,
                "security_code": payload["security_code"],
                "reason": payload.get("reason", ""),
                "created_by": payload.get("created_by", ""),
            }
            self.store.append(
                plan_id,
                "plan_created",
                {
                    "plan_id": plan_id,
                    "group_owner_id": owner,
                    "security_code": payload["security_code"],
                    "share_capital": capital,
                    "version": version,
                },
                expected_version=0,
                business_time=payload["effective_from"],
            )
            return {"plan_id": plan_id, "version": version}

    @staticmethod
    def _assert_valid_period(fields: dict[str, Any]) -> None:
        if fields.get("effective_to") and fields.get("effective_from"):
            if fields["effective_to"] < fields["effective_from"]:
                raise ValidationError("effective_to 早于 effective_from")

    def _assert_editable(self, plan) -> None:
        if plan.submitted_version is not None:
            raise PlanLockedError(
                f"计划 {plan.plan_id} 版本 {plan.submitted_version} 正在送审，"
                "普通编辑已锁定；请撤回送审或使用变更送审流程"
            )
        if plan.state != "draft":
            raise PlanLockedError(
                f"计划状态为 {plan.state}，仅 draft 状态可普通编辑"
            )

    def edit_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """草稿普通编辑。送审中调用一律拒绝（不得覆盖送审版本）。"""

        with self._lock:
            self._require(payload, ["plan_id"])
            state = self._state()
            plan = self._plan(state, payload["plan_id"])
            self._assert_editable(plan)

            fields: dict[str, Any] = {}
            if "proposed_qty" in payload:
                qty = self._qty(payload["proposed_qty"], "proposed_qty")
                if qty <= 0:
                    raise ValidationError("proposed_qty 必须为正整数")
                fields["proposed_qty"] = qty
            for key in ("effective_from", "effective_to", "reason", "created_by"):
                if key in payload:
                    fields[key] = payload[key]
            if "channels" in payload:
                channels = payload["channels"]
                if not isinstance(channels, list) or any(
                    c not in PLAN_CHANNELS for c in channels
                ):
                    raise ValidationError("channels 非法")
                fields["channels"] = channels
            if "effective_from" in fields:
                parse_date(fields["effective_from"], "effective_from")
            if fields.get("effective_to"):
                parse_date(fields["effective_to"], "effective_to")
            self._assert_valid_period(fields)

            version = self._new_version(plan, "draft", fields)
            self.store.append(
                plan.plan_id, "plan_edited",
                {"plan_id": plan.plan_id, "version": version},
                expected_version=self.store.stream_version(plan.plan_id),
                business_time=version["effective_from"],
            )
            return {"plan_id": plan.plan_id, "version": version}

    def submit_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """草稿提交送审：生成送审版本并记录提交时的合规预检结论。"""

        with self._lock:
            self._require(payload, ["plan_id"])
            state = self._state()
            plan = self._plan(state, payload["plan_id"])
            if plan.submitted_version is not None:
                raise PlanLockedError("计划已在送审中，不得重复提交")
            if plan.state != "draft":
                raise StateConflictError(
                    f"仅 draft 计划可提交送审，当前状态 {plan.state}"
                )
            version = self._new_version(plan, "review", {})
            self.store.append(
                plan.plan_id, "plan_submitted",
                {
                    "plan_id": plan.plan_id,
                    "version": version,
                    "review_return_state": "draft",
                },
                expected_version=self.store.stream_version(plan.plan_id),
                business_time=version["effective_from"],
            )            # 提交时预检：记录执行起始日的逐层额度与窗口快照（不阻断送审）
            eval_state = self._state()
            eval_plan = self._plan(eval_state, plan.plan_id)
            snapshot = compute_layers(
                eval_state, eval_plan, version["effective_from"],
            )
            precheck = {
                "result": "submitted",
                "day": version["effective_from"],
                "channel": version["channels"][0],
                "requested_qty": 0,
                "allowed_qty": snapshot["allowed_qty"],
                "binding_layer": snapshot["binding_layer"],
                "reasons": [],
                "layers": snapshot,
                "conflict_windows": conflict_windows(
                    eval_state, eval_plan, version["effective_from"]
                ),
            }
            self._record_decision(
                eval_state, eval_plan, "submit", precheck,
                version["effective_from"],
            )
            return {"plan_id": plan.plan_id, "version": version}

    def approve_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(payload, ["plan_id", "approved_date"])
            state = self._state()
            plan = self._plan(state, payload["plan_id"])
            day = payload["approved_date"]
            parse_date(day)
            if plan.submitted_version is None or plan.state != "review":
                raise StateConflictError("计划不在送审中，无法核准")
            version = self._new_version(plan, "active", {})
            self.store.append(
                plan.plan_id, "plan_approved",
                {"plan_id": plan.plan_id, "version": version},
                expected_version=self.store.stream_version(plan.plan_id),
                business_time=day,
            )

            state = self._state()
            plan = self._plan(state, plan.plan_id)
            rule = state.effective_rule(day)
            threshold_pct = (
                rule.pre_disclosure_threshold_pct if rule else 1.0
            )
            # 触碰预披露阈值 → 建立有时限的预披露公告流程
            if (
                version["proposed_qty"]
                >= plan.share_capital * threshold_pct / 100
            ):
                self._create_announcement(
                    state, plan, "pre_disclosure", day,
                    note=(
                        f"拟减 {version['proposed_qty']} 股达到总股本 "
                        f"{threshold_pct}% 预披露阈值"
                    ),
                )
            post_state = self._state()
            post_plan = self._plan(post_state, plan.plan_id)
            evaluation = evaluate_execution(
                post_state, post_plan, day, version["channels"][0], 0,
            )
            self._record_decision(post_state, post_plan, "approve", evaluation, day)
            return {"plan_id": plan.plan_id, "version": version}

    def reject_or_withdraw_plan(
        self, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        event_type = "plan_rejected" if action == "reject" else "plan_withdrawn"
        with self._lock:
            self._require(payload, ["plan_id"])
            state = self._state()
            plan = self._plan(state, payload["plan_id"])
            if plan.submitted_version is None or plan.state != "review":
                raise StateConflictError("计划不在送审中")
            day = payload.get("date") or iso(date.today())
            if "date" in payload:
                parse_date(day)
            return_state = plan.review_return_state or "draft"
            version = self._new_version(plan, return_state, {})
            self.store.append(
                plan.plan_id, event_type,
                {"plan_id": plan.plan_id, "version": version},
                expected_version=self.store.stream_version(plan.plan_id),
                business_time=day,
            )
            return {"plan_id": plan.plan_id, "version": version}

    def reject_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.reject_or_withdraw_plan("reject", payload)

    def withdraw_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.reject_or_withdraw_plan("withdraw", payload)

    def suspend_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(payload, ["plan_id"])
            state = self._state()
            plan = self._plan(state, payload["plan_id"])
            if plan.state != "active":
                raise StateConflictError("仅 active 计划可暂停")
            version = self._new_version(plan, "suspended", {})
            self.store.append(
                plan.plan_id, "plan_suspended",
                {"plan_id": plan.plan_id, "version": version},
                expected_version=self.store.stream_version(plan.plan_id),
                business_time=payload.get("date") or iso(date.today()),
            )
            return {"plan_id": plan.plan_id, "version": version}

    def resume_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(payload, ["plan_id"])
            state = self._state()
            plan = self._plan(state, payload["plan_id"])
            if plan.state != "suspended":
                raise StateConflictError("仅 suspended 计划可恢复")
            version = self._new_version(plan, "active", {})
            self.store.append(
                plan.plan_id, "plan_resumed",
                {"plan_id": plan.plan_id, "version": version},
                expected_version=self.store.stream_version(plan.plan_id),
                business_time=payload.get("date") or iso(date.today()),
            )
            return {"plan_id": plan.plan_id, "version": version}

    # ----- 变更送审：生效前不影响现行版本 ---------------------------------

    def submit_plan_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        """active 计划提出变更：进入送审，现行版本继续有效，普通编辑仍锁定。"""

        with self._lock:
            self._require(payload, ["plan_id"])
            state = self._state()
            plan = self._plan(state, payload["plan_id"])
            if plan.state != "active":
                raise StateConflictError(
                    f"仅 active 计划可提出变更，当前 {plan.state}"
                )
            if plan.submitted_version is not None:
                raise PlanLockedError("计划已有变更在送审中")
            fields: dict[str, Any] = {}
            if "proposed_qty" in payload:
                qty = self._qty(payload["proposed_qty"], "proposed_qty")
                if qty <= 0:
                    raise ValidationError("proposed_qty 必须为正整数")
                fields["proposed_qty"] = qty
            for key in ("effective_from", "effective_to", "reason", "created_by"):
                if key in payload:
                    fields[key] = payload[key]
            if "channels" in payload:
                channels = payload["channels"]
                if not isinstance(channels, list) or not channels or any(
                    c not in PLAN_CHANNELS for c in channels
                ):
                    raise ValidationError("channels 必须是 secondary/block 的非空列表")
                fields["channels"] = channels
            for key in ("effective_from", "effective_to"):
                if key in fields and fields[key] is not None:
                    parse_date(fields[key], key)
            if not fields:
                raise ValidationError("变更内容为空")
            pending = self._new_version(plan, "review", fields)
            if (
                pending["effective_to"]
                and pending["effective_to"] < pending["effective_from"]
            ):
                raise ValidationError("effective_to 早于 effective_from")
            self.store.append(
                plan.plan_id, "plan_change_submitted",
                {
                    "plan_id": plan.plan_id,
                    "version": pending,
                    "review_return_state": "active",
                },
                expected_version=self.store.stream_version(plan.plan_id),
                business_time=pending["effective_from"],
            )
            return {"plan_id": plan.plan_id, "pending_version": pending}

    def approve_plan_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(payload, ["plan_id", "approved_date"])
            state = self._state()
            plan = self._plan(state, payload["plan_id"])
            day = payload["approved_date"]
            parse_date(day)
            if plan.state != "active" or plan.submitted_version is None:
                raise StateConflictError("没有待核准的计划变更")
            pending = next(
                (v for v in reversed(plan.versions) if v.version_no == plan.submitted_version),
                None,
            )
            promoted = {
                "version_no": pending.version_no,
                "state": "active",
                "effective_from": pending.effective_from,
                "effective_to": pending.effective_to,
                "proposed_qty": pending.proposed_qty,
                "channels": list(pending.channels),
                "security_code": plan.security_code,
                "reason": pending.reason,
                "created_by": pending.created_by,
            }
            self.store.append(
                plan.plan_id, "plan_change_approved",
                {"plan_id": plan.plan_id, "version": promoted},
                expected_version=self.store.stream_version(plan.plan_id),
                business_time=day,
            )
            self._create_announcement(
                self._state(),
                self._plan(self._state(), plan.plan_id),
                "plan_change", day,
                note="减持计划变更已核准生效",
            )
            return {"plan_id": plan.plan_id, "version": promoted}

    def reject_plan_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(payload, ["plan_id"])
            state = self._state()
            plan = self._plan(state, payload["plan_id"])
            if plan.state != "active" or plan.submitted_version is None:
                raise StateConflictError("没有待核准的计划变更")
            rejected_version = plan.submitted_version
            self.store.append(
                plan.plan_id, "plan_change_rejected",
                {"plan_id": plan.plan_id, "rejected_version": rejected_version},
                expected_version=self.store.stream_version(plan.plan_id),
                business_time=payload.get("date") or iso(date.today()),
            )
            return {"plan_id": plan.plan_id, "rejected_version": rejected_version}

    def terminate_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(payload, ["plan_id", "terminated_date"])
            state = self._state()
            plan = self._plan(state, payload["plan_id"])
            day = payload["terminated_date"]
            parse_date(day)
            if plan.state not in {"active", "suspended"}:
                raise StateConflictError(
                    f"仅 active/suspended 计划可终止，当前 {plan.state}"
                )
            version = self._new_version(plan, "closed", {})
            self.store.append(
                plan.plan_id, "plan_terminated",
                {"plan_id": plan.plan_id, "version": version},
                expected_version=self.store.stream_version(plan.plan_id),
                business_time=day,
            )
            self._create_announcement(
                self._state(),
                self._plan(self._state(), plan.plan_id),
                "plan_termination", day,
                note="减持计划终止",
            )
            return {"plan_id": plan.plan_id, "version": version}

    # ==================================================================
    # 成交回报：乱序 / 撤回 / 替换 / 幂等
    # ==================================================================

    def _find_report(self, state: State, report_id: str):
        for plan in state.plans.values():
            for t in plan.trades:
                if t.report_id == report_id:
                    return plan, t
        return None, None

    def report_trade(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(
                payload,
                ["report_id", "plan_id", "shareholder_id", "batch_id",
                 "channel", "trade_date", "qty"],
            )
            report_id = payload["report_id"]
            state = self._state()
            existing_plan, existing = self._find_report(state, report_id)
            if existing is not None:
                # 同一回报不能重复扣减
                raise StateConflictError(
                    f"成交回报 {report_id} 已接收（计划 {existing_plan.plan_id}），"
                    "不得重复提交；如需更正请使用替换回报"
                )
            plan = self._plan(state, payload["plan_id"])
            day = payload["trade_date"]
            parse_date(day, "trade_date")
            qty = self._qty(payload["qty"], "qty")
            if qty <= 0:
                raise ValidationError("成交数量必须为正整数")
            channel = payload["channel"]
            if channel not in PLAN_CHANNELS:
                raise ValidationError(f"未知交易渠道: {channel}")
            member = payload["shareholder_id"]
            if member not in state.group_members(plan.group_owner_id, day):
                raise ValidationError(
                    f"账户 {member} 在 {day} 不属于计划组合并范围，"
                    "不能计入本计划（历史归属之外的成交请另立计划）"
                )
            batch = state.batches.get(payload["batch_id"])
            if batch is None or batch.shareholder_id != member:
                raise ValidationError("证券批次不存在或不属于该账户")
            if batch.security_code != plan.security_code:
                raise ValidationError("批次证券与计划证券不一致")
            if plan.state != "active":
                raise StateConflictError(
                    f"计划状态为 {plan.state}，非 active 不得接收成交回报"
                )

            evaluation = evaluate_execution(
                state, plan, day, channel, qty, account=member,
            )
            if evaluation["result"] == "denied":
                self._record_decision(
                    state, plan, "trade_denied", evaluation, day,
                )
                raise ValidationError(
                    "放行拒绝：" + "；".join(evaluation["reasons"])
                )

            trade = {
                "report_id": report_id,
                "plan_id": plan.plan_id,
                "shareholder_id": member,
                "batch_id": batch.batch_id,
                "security_code": plan.security_code,
                "channel": channel,
                "trade_date": day,
                "qty": qty,
                "price": payload.get("price"),
                "replaced": False,
                "replaces": None,
                "applied": True,
            }
            self.store.append(
                plan.plan_id, "trade_reported", trade, business_time=day,
            )
            self._record_decision(
                self._state(),
                self._plan(self._state(), plan.plan_id),
                "trade_reported", evaluation, day,
            )
            self._detect_holding_steps(plan.plan_id, report_id)
            return {"trade": trade, "evaluation": evaluation}

    def withdraw_trade(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(payload, ["report_id"])
            state = self._state()
            plan, trade = self._find_report(state, payload["report_id"])
            if trade is None:
                raise NotFoundError(f"成交回报不存在: {payload['report_id']}")
            if trade.replaced:
                raise StateConflictError(
                    f"回报 {trade.report_id} 已撤回/被替换，不能重复撤回"
                )
            self.store.append(
                plan.plan_id, "trade_withdrawn",
                {"plan_id": plan.plan_id, "report_id": trade.report_id},
                business_time=payload.get("date") or iso(date.today()),
            )
            return {"report_id": trade.report_id, "status": "withdrawn"}

    def replace_trade(self, payload: dict[str, Any]) -> dict[str, Any]:
        """更正回报：原回报标记失效（回补额度），替换回报按业务日参与重放。"""

        with self._lock:
            self._require(
                payload,
                ["report_id", "replacement"],
            )
            state = self._state()
            plan, original = self._find_report(state, payload["report_id"])
            if original is None:
                raise NotFoundError(f"原成交回报不存在: {payload['report_id']}")
            if original.replaced:
                raise StateConflictError(
                    f"原回报 {original.report_id} 已撤回/被替换"
                )
            repl = payload["replacement"]
            self._require(
                repl,
                ["report_id", "channel", "trade_date", "qty"],
            )
            if state and self._find_report(state, repl["report_id"])[1] is not None:
                raise StateConflictError(
                    f"替换回报 {repl['report_id']} 已存在"
                )
            day = repl["trade_date"]
            parse_date(day, "trade_date")
            qty = self._qty(repl["qty"], "qty")
            if qty <= 0:
                raise ValidationError("成交数量必须为正整数")
            if repl["channel"] not in PLAN_CHANNELS:
                raise ValidationError("替换回报渠道非法")
            member = original.shareholder_id
            if member not in state.group_members(plan.group_owner_id, day):
                raise ValidationError(
                    f"账户 {member} 在 {day} 不属于计划组合并范围"
                )
            # 更正后的成交按业务日重新走放行闸门；评估时视原回报已失效，
            # 避免新旧两笔同时占用额度（投影为一次性快照，可安全就地标记）
            original.replaced = True
            evaluation = evaluate_execution(
                state, plan, day, repl["channel"], qty, account=member,
            )
            if evaluation["result"] == "denied":
                raise ValidationError(
                    "替换回报放行拒绝：" + "；".join(evaluation["reasons"])
                )
            replacement = {
                "report_id": repl["report_id"],
                "plan_id": plan.plan_id,
                "shareholder_id": member,
                "batch_id": original.batch_id,
                "security_code": plan.security_code,
                "channel": repl["channel"],
                "trade_date": day,
                "qty": qty,
                "price": repl.get("price"),
                "replaced": False,
                "replaces": original.report_id,
                "applied": True,
            }
            self.store.append(
                plan.plan_id,
                "trade_replaced",
                {
                    "plan_id": plan.plan_id,
                    "report_id": original.report_id,
                    "replacement": replacement,
                },
                business_time=day,
            )
            self._detect_holding_steps(plan.plan_id, repl["report_id"])
            return {"report_id": original.report_id, "replacement": replacement}

    def _detect_holding_steps(self, plan_id: str, trigger_report: str) -> None:
        """重放后以新回报为触发点，检测组合累计减持是否触达新的披露阶梯。

        阶梯按组合全口径累计（跨计划、按当时归属），每达到一个
        ``holding_disclosure_step_pct``（默认总股本 1%）建立一个有时限的公告。
        """

        state = self._state()
        plan = self._plan(state, plan_id)
        trades = state.group_trades(plan.group_owner_id, plan.security_code)
        trigger = next((t for t in trades if t.report_id == trigger_report), None)
        if trigger is None:
            return
        existing = {(a.kind, a.step_no) for a in plan.announcements}
        rule = state.effective_rule(trigger.trade_date)
        step_pct = rule.holding_disclosure_step_pct if rule else 1.0
        confirm_days = rule.announcement_confirm_days if rule else 2
        step_size = plan.share_capital * step_pct / 100
        cumulative = sum(x.qty for x in trades if x.trade_date <= trigger.trade_date)
        top_step = int(cumulative // step_size) if step_size > 0 else 0
        for s in range(1, top_step + 1):
            if ("holding_step", s) in existing:
                continue
            self._create_announcement(
                state, plan, "holding_step", trigger.trade_date,
                step_no=s, confirm_days=confirm_days,
                note=(
                    f"截至 {trigger.trade_date} 组合累计减持 {cumulative} 股，"
                    f"触及第 {s} 个 {step_pct}% 阶梯（触发回报 {trigger.report_id}）"
                ),
            )
            existing.add(("holding_step", s))

    # ==================================================================
    # 公告流程
    # ==================================================================

    def _create_announcement(
        self, state: State, plan, kind: str, triggered_date: str,
        *, step_no: int | None = None, confirm_days: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        rule = state.effective_rule(triggered_date)
        days = confirm_days or (rule.announcement_confirm_days if rule else 2)
        ann_id = uuid.uuid4().hex
        ann = {
            "announcement_id": ann_id,
            "plan_id": plan.plan_id,
            "kind": kind,
            "triggered_date": triggered_date,
            "due_date": add_days(triggered_date, days),
            "status": "pending",
            "confirmed_at": None,
            "step_no": step_no,
            "note": note,
        }
        self.store.append(
            plan.plan_id, "plan_announcement_created",
            {"plan_id": plan.plan_id, "announcement": ann},
            business_time=triggered_date,
        )
        return ann

    def confirm_announcement(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(payload, ["plan_id", "announcement_id", "confirmed_date"])
            state = self._state()
            plan = self._plan(state, payload["plan_id"])
            ann = next(
                (a for a in plan.announcements
                 if a.announcement_id == payload["announcement_id"]),
                None,
            )
            if ann is None:
                raise NotFoundError(
                    f"公告不存在: {payload['announcement_id']}"
                )
            if ann.status in {"confirmed", "void"}:
                raise StateConflictError("公告已确认/作废")
            day = payload["confirmed_date"]
            parse_date(day)
            self.store.append(
                plan.plan_id, "plan_announcement_status_changed",
                {
                    "plan_id": plan.plan_id,
                    "announcement_id": ann.announcement_id,
                    "status": "confirmed",
                    "confirmed_at": day,
                },
                expected_version=self.store.stream_version(plan.plan_id),
                business_time=day,
            )
            return {
                "announcement_id": ann.announcement_id,
                "status": "confirmed",
                "confirmed_at": day,
                "overdue_at_confirmation": day > ann.due_date,
            }

    # ==================================================================
    # 试算与查询（只读，全部可解释）
    # ==================================================================

    def evaluate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """不落库的放行试算。可传 shareholder_id 绑定申报账户。"""

        self._require(payload, ["plan_id", "date", "channel", "qty"])
        state = self._state()
        plan = self._plan(state, payload["plan_id"])
        parse_date(payload["date"], "date")
        qty = self._qty(payload["qty"], "qty")
        account = payload.get("shareholder_id")
        return evaluate_execution(
            state, plan, payload["date"], payload["channel"], qty,
            account=account,
        )

    def get_plan(self, plan_id: str, as_of: str | None = None) -> dict[str, Any]:
        state = self._state()
        plan = self._plan(state, plan_id)
        day = as_of or iso(date.today())
        parse_date(day)
        layers = compute_layers(state, plan, day)
        windows = conflict_windows(state, plan, day)
        disclosures = pending_disclosures(state, plan, day)
        blockers = gating_blockers(state, plan, day)

        members = state.group_members(plan.group_owner_id, day)
        group_trades = state.group_trades(plan.group_owner_id, plan.security_code)
        plan_trades = state.effective_trades(plan_id)
        # 关联账户贡献：当前成员 + 在计划内留有按当时归属成交的历史成员
        historical_accounts = {t.shareholder_id for t in plan_trades}
        contribution_ids = sorted(set(members) | historical_accounts)
        contributions = []
        for m in contribution_ids:
            shareholder = state.shareholders.get(m)
            contributions.append(
                {
                    "shareholder_id": m,
                    "name": shareholder.name if shareholder else None,
                    "qty_in_plan": sum(
                        t.qty for t in plan_trades if t.shareholder_id == m
                    ),
                    "qty_group_since_plan_start": sum(
                        t.qty
                        for t in group_trades
                        if t.shareholder_id == m
                        and t.trade_date >= plan.current.effective_from
                    ),
                    "included": m in members,
                }
            )

        # 成交重放序列：包含被排除的回报及其原因，证明"可重放/不重复扣减"
        replay_rows = []
        for t in plan.trades:
            attributed = state.attributed_to_group(plan, t)
            excluded = []
            if t.replaced:
                excluded.append("回报已撤回或被替换回报取代")
            if not attributed:
                excluded.append("成交当日该账户不在合并范围（保留历史归属）")
            replay_rows.append(
                {
                    "report_id": t.report_id,
                    "replaces": t.replaces,
                    "shareholder_id": t.shareholder_id,
                    "batch_id": t.batch_id,
                    "channel": t.channel,
                    "trade_date": t.trade_date,
                    "qty": t.qty,
                    "counted": not excluded,
                    "excluded_reasons": excluded,
                }
            )
        replay_rows.sort(key=lambda r: (r["trade_date"], r["report_id"]))

        decisions = [
            d for d in state.decisions if d["plan_id"] == plan_id
        ]
        return {
            "plan_id": plan_id,
            "as_of_date": day,
            "group_owner_id": plan.group_owner_id,
            "security_code": plan.security_code,
            "share_capital": plan.share_capital,
            "state": plan.state,
            "submitted_version": plan.submitted_version,
            "current_version": self._version_dict(plan, plan.current),
            "pending_change": (
                self._version_dict(
                    plan,
                    next(
                        (v for v in reversed(plan.versions)
                         if v.version_no == plan.submitted_version),
                        None,
                    ),
                )
                if plan.submitted_version is not None
                and plan.state == "active"
                else None
            ),
            "versions": [self._version_dict(plan, v) for v in plan.versions],
            "members": members,
            "related_accounts": [
                row for row in relationship_evidence(state, plan, day)
                if row["role"] == "related"
            ],
            "contributions": contributions,
            "available_qty": {
                "as_of_date": day,
                "allowed_qty": layers["allowed_qty"],
                "binding_layer": layers["binding_layer"],
                "layers": layers["layers"],
            },
            "conflict_windows": windows,
            "pending_disclosures": disclosures,
            "gating_blockers": blockers,
            "trade_replay": {
                "rows": replay_rows,
                "counted_qty": sum(
                    r["qty"] for r in replay_rows if r["counted"]
                ),
            },
            "decisions": decisions,
        }

    @staticmethod
    def _version_dict(plan, v) -> dict[str, Any] | None:
        if v is None:
            return None
        return {
            "version_no": v.version_no,
            "state": v.state,
            "effective_from": v.effective_from,
            "effective_to": v.effective_to,
            "proposed_qty": v.proposed_qty,
            "channels": list(v.channels),
            "reason": v.reason,
            "created_by": v.created_by,
        }

    def list_plans(self) -> list[dict[str, Any]]:
        state = self._state()
        result = []
        for plan in state.plans.values():
            result.append(
                {
                    "plan_id": plan.plan_id,
                    "state": plan.state,
                    "group_owner_id": plan.group_owner_id,
                    "security_code": plan.security_code,
                    "current_version_no": plan.version,
                    "submitted_version": plan.submitted_version,
                }
            )
        return result
