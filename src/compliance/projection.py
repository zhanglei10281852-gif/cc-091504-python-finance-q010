"""事件投影：把追加日志重放为内存读取模型。

投影结果 :class:`State` 是只读快照，每次命令处理时即时重建一次——
事件量在证券事务场景下可控，换取完全确定性的可重放语义：

* 成交回报乱序到达：按业务发生日重排后再累计；
* 回报撤回/更正：被标记的回报不参与累计；
* 关系变更：按业务日判断当时是否属于合并范围；
* 规则版本：按业务日选取当时生效的版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from compliance.dates import parse_date
from compliance.models import (
    Announcement,
    PlanAggregate,
    PlanVersion,
    Relationship,
    RuleVersion,
    SecurityBatch,
    Shareholder,
    Trade,
    UnlockCondition,
    Window,
)


@dataclass
class State:
    shareholders: dict[str, Shareholder] = field(default_factory=dict)
    relationships: dict[str, Relationship] = field(default_factory=dict)
    batches: dict[str, SecurityBatch] = field(default_factory=dict)
    unlocks: dict[str, UnlockCondition] = field(default_factory=dict)
    windows: dict[str, Window] = field(default_factory=dict)
    rules: dict[str, RuleVersion] = field(default_factory=dict)  # key: rule_id
    plans: dict[str, PlanAggregate] = field(default_factory=dict)
    # 阶梯减持公告的确认记录，键为 (plan_id, kind, step_no)
    announcement_confirmations: dict[tuple[str, str, int], str] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------ 关系合并范围

    def group_members(self, owner_id: str, day: str) -> list[str]:
        """返回 ``day`` 当日与 ``owner_id`` 合并计算的全部账户（含本人）。"""

        members = {owner_id}
        for rel in self.relationships.values():
            if rel.group_owner_id == owner_id and rel.active_on(day):
                members.add(rel.subject_id)
        return sorted(members)

    def attributed_to_group(self, plan: PlanAggregate, trade: Trade) -> bool:
        """成交是否计入该计划组合并范围——按成交当日的关系归属判断。"""

        if trade.shareholder_id == plan.group_owner_id:
            return True
        return trade.shareholder_id in self.group_members(
            plan.group_owner_id, trade.trade_date
        )

    # ------------------------------------------------------------ 规则版本选择

    def effective_rule(self, day: str) -> RuleVersion | None:
        d = parse_date(day)
        candidates = [
            r
            for r in self.rules.values()
            if parse_date(r.effective_from) <= d
            and (r.effective_to is None or d < parse_date(r.effective_to))
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: parse_date(r.effective_from))

    # ------------------------------------------------------------ 计划成交（重放）

    def group_trades(self, owner_id: str, security: str) -> list[Trade]:
        """该股东组合（含当时关联账户）在该证券上的全部有效成交，跨计划聚合。

        归属按每笔成交当日的关系判断——关系变化后，历史成交保留当时归属。
        """

        result: list[Trade] = []
        for plan in self.plans.values():
            if plan.group_owner_id != owner_id or plan.security_code != security:
                continue
            for t in plan.trades:
                if t.replaced or not self.attributed_to_group(plan, t):
                    continue
                result.append(t)
        result.sort(key=lambda t: (t.trade_date, t.report_id))
        return result

    def effective_trades(self, plan_id: str) -> list[Trade]:
        """当前计划内有效、且按成交当日关系归属本组合的成交，按业务日重排。

        同一 report_id 在日志中只会出现一次（重复回报在命令层被拒绝），
        因此本列表即是“可重放、不重复扣减”的累计依据。
        """

        plan = self.plans[plan_id]
        trades = [
            t
            for t in plan.trades
            if not t.replaced and self.attributed_to_group(plan, t)
        ]
        trades.sort(key=lambda t: (t.trade_date, t.report_id))
        return trades


def _rule_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    fields = set(RuleVersion.__dataclass_fields__)
    return {k: v for k, v in payload.items() if k in fields}


def apply_event(state: State, event: dict[str, Any]) -> None:
    etype = event["event_type"]
    p = event["payload"]

    if etype == "shareholder_registered":
        state.shareholders[p["shareholder_id"]] = Shareholder(**p)

    elif etype == "relationship_declared":
        state.relationships[p["rel_id"]] = Relationship(**p)

    elif etype == "relationship_ended":
        rel = state.relationships.get(p["rel_id"])
        if rel is not None:
            rel.effective_to = p["effective_to"]

    elif etype == "batch_recorded":
        state.batches[p["batch_id"]] = SecurityBatch(**p)

    elif etype == "unlock_recorded":
        state.unlocks[p["unlock_id"]] = UnlockCondition(**p)

    elif etype == "window_declared":
        state.windows[p["window_id"]] = Window(**p)

    elif etype == "rule_published":
        # 同 rule_id 的新版本覆盖；生效区间不重叠由命令层校验
        state.rules[p["rule_id"]] = RuleVersion(**_rule_kwargs(p))

    elif etype == "plan_created":
        v = PlanVersion(**p["version"])
        plan = PlanAggregate(
            plan_id=p["plan_id"],
            group_owner_id=p["group_owner_id"],
            security_code=p["security_code"],
            share_capital=p["share_capital"],
            current=v,
            versions=[v],
        )
        state.plans[p["plan_id"]] = plan

    elif etype in {
        "plan_submitted",
        "plan_edited",
        "plan_approved",
        "plan_rejected",
        "plan_withdrawn",
        "plan_suspended",
        "plan_resumed",
        "plan_terminated",
    }:
        plan = state.plans[p["plan_id"]]
        v = PlanVersion(**p["version"])
        plan.versions.append(v)
        plan.current = v
        if etype == "plan_submitted":
            plan.submitted_version = v.version_no
            if p.get("review_return_state") is not None:
                plan.review_return_state = p["review_return_state"]
        elif etype in {
            "plan_withdrawn", "plan_rejected", "plan_approved",
        }:
            plan.submitted_version = None
            plan.review_return_state = None

    elif etype == "plan_change_submitted":
        # 变更送审：挂起待核准版本，现行 active 版本继续作为 current 生效
        plan = state.plans[p["plan_id"]]
        v = PlanVersion(**p["version"])
        plan.versions.append(v)
        plan.submitted_version = v.version_no
        plan.review_return_state = p.get("review_return_state") or "active"

    elif etype == "plan_change_approved":
        # 核准：把挂起版本提升为 active 并设为 current
        plan = state.plans[p["plan_id"]]
        promoted = PlanVersion(**p["version"])
        for i, v in enumerate(plan.versions):
            if v.version_no == promoted.version_no:
                plan.versions[i] = promoted
                break
        plan.current = promoted
        plan.submitted_version = None
        plan.review_return_state = None

    elif etype == "plan_change_rejected":
        # 变更被驳回：现行版本不变，挂起版本标记为 rejected，解除送审锁
        plan = state.plans[p["plan_id"]]
        for v in plan.versions:
            if v.version_no == int(p["rejected_version"]):
                v.state = "rejected"
        plan.submitted_version = None
        plan.review_return_state = None

    elif etype == "trade_reported":
        t = Trade(**p)
        plan = state.plans[t.plan_id]
        plan.trades.append(t)

    elif etype in {"trade_withdrawn", "trade_replaced"}:
        plan = state.plans[p["plan_id"]]
        for t in plan.trades:
            if t.report_id == p["report_id"]:
                t.replaced = True
        if etype == "trade_replaced":
            plan.trades.append(Trade(**p["replacement"]))

    elif etype == "plan_announcement_created":
        plan = state.plans[p["plan_id"]]
        plan.announcements.append(Announcement(**p["announcement"]))

    elif etype == "plan_announcement_status_changed":
        plan = state.plans[p["plan_id"]]
        for ann in plan.announcements:
            if ann.announcement_id == p["announcement_id"]:
                ann.status = p["status"]
                if p.get("confirmed_at") is not None:
                    ann.confirmed_at = p["confirmed_at"]

    elif etype == "announcement_confirmed":
        state.announcement_confirmations[
            (p["plan_id"], p["kind"], int(p["step_no"]))
        ] = event["business_time"] or ""

    elif etype == "decision_recorded":
        state.decisions.append(p)


def replay(events: list[dict[str, Any]]) -> State:
    state = State()
    for event in events:  # 日志按追加顺序（接收顺序）重放
        apply_event(state, event)
    return state
