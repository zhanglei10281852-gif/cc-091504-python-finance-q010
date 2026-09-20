"""规则引擎：把放行结论拆成可解释的逐层计算。

每一个数量限制都是一个独立"层"，层内给出 ``limit / used / remaining`` 与
**证据**（规则版本、命中条款、参与计算的批次/成交/关系）。多层同时限制时
取 ``remaining`` 最小值（最严格结果）。

层级总览：
    plan_remaining        计划额度：计划拟减数量 − 计划内已成交
    unlocked_holdings     解禁持仓：成员账户已解禁且未卖出的股份
    controller_secondary  实际控制人集中竞价：任意连续 90 日 ≤ 总股本 1%
    controller_block      实际控制人大宗交易：任意连续 90 日 ≤ 总股本 2%
    director_annual       董监高任职年度：自然年内转让 ≤ 所持 25%（按账户）
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from compliance.dates import iso, parse_date
from compliance.models import PlanAggregate

CHANNEL_CLASS = {
    "secondary": "secondary",  # 集中竞价
    "block": "block",  # 大宗交易
}

GATING_ANNOUNCEMENT_KINDS = {"pre_disclosure", "holding_step"}


# --------------------------------------------------------------------- 工具函数


def _clause_evidence(rule: Any | None) -> dict[str, Any]:
    if rule is None:
        return {
            "rule_id": None,
            "version": None,
            "name": "内置缺省规则",
            "note": "当日无已发布规则版本，采用系统内置阈值",
        }
    return {
        "rule_id": rule.rule_id,
        "version": rule.version,
        "name": rule.name,
        "effective_from": rule.effective_from,
        "effective_to": rule.effective_to,
    }


def _default_thresholds(rule: Any | None) -> dict[str, Any]:
    if rule is None:
        # 与《上市公司股东减持股份管理暂行办法》常规口径一致的缺省值
        return {
            "controller_calendar_days": 90,
            "controller_secondary_pct": 1.0,
            "controller_block_pct": 2.0,
            "director_annual_pct": 25.0,
            "holding_disclosure_step_pct": 1.0,
            "announcement_confirm_days": 2,
        }
    return {
        "controller_calendar_days": rule.controller_calendar_days,
        "controller_secondary_pct": rule.controller_secondary_pct,
        "controller_block_pct": rule.controller_block_pct,
        "director_annual_pct": rule.director_annual_pct,
        "holding_disclosure_step_pct": rule.holding_disclosure_step_pct,
        "announcement_confirm_days": rule.announcement_confirm_days,
    }


# --------------------------------------------------------------------- 持仓/成交


def member_unlocked(state: Any, member: str, security: str, day: str) -> dict[str, Any]:
    """单个成员账户在 ``day`` 的已解禁数量及批次证据。"""

    d = parse_date(day)
    items: list[dict[str, Any]] = []
    total = 0
    for batch in state.batches.values():
        if batch.shareholder_id != member or batch.security_code != security:
            continue
        locked_by_issuer = (
            batch.issuer_lockup_until is not None
            and parse_date(batch.issuer_lockup_until) > d
        )
        batch_unlocked = 0
        unlock_items = []
        for unlock in state.unlocks.values():
            if unlock.batch_id != batch.batch_id:
                continue
            effective = parse_date(unlock.unlock_date) <= d
            counted = unlock.satisfied and effective and not locked_by_issuer
            qty = unlock.tranche_qty if counted else 0
            batch_unlocked += qty
            unlock_items.append(
                {
                    "unlock_id": unlock.unlock_id,
                    "type": unlock.condition_type,
                    "unlock_date": unlock.unlock_date,
                    "tranche_qty": unlock.tranche_qty,
                    "satisfied": unlock.satisfied,
                    "counted": counted,
                    "reason_not_counted": (
                        None
                        if counted
                        else (
                            "发行人锁定期未满"
                            if locked_by_issuer
                            else (
                                "解禁日未到"
                                if not effective
                                else "业绩考核条件未满足"
                            )
                        )
                    ),
                }
            )
        total += batch_unlocked
        items.append(
            {
                "batch_id": batch.batch_id,
                "source": batch.source,
                "total_qty": batch.total_qty,
                "issuer_lockup_until": batch.issuer_lockup_until,
                "unlocked_qty": batch_unlocked,
                "unlocks": unlock_items,
            }
        )
    return {"shareholder_id": member, "unlocked_qty": total, "batches": items}


def account_trades_in_year(
    state: Any, account: str, security: str, day: str
) -> list[Any]:
    """该账户在 ``day`` 所在自然年、针对该证券的全部有效成交（跨计划）。"""

    year = parse_date(day).year
    result = []
    for plan in state.plans.values():
        if plan.security_code != security:
            continue
        for t in plan.trades:
            if (
                t.replaced
                or t.shareholder_id != account
                or parse_date(t.trade_date).year != year
            ):
                continue
            result.append(t)
    return result


# --------------------------------------------------------------------- 逐层计算


def relationship_evidence(state: Any, plan: PlanAggregate, day: str) -> list[dict[str, Any]]:
    evidence = [
        {
            "shareholder_id": plan.group_owner_id,
            "role": "self",
            "rel_type": None,
            "active_on": day,
        }
    ]
    for rel in state.relationships.values():
        if rel.group_owner_id != plan.group_owner_id:
            continue
        evidence.append(
            {
                "shareholder_id": rel.subject_id,
                "role": "related",
                "rel_type": rel.rel_type,
                "effective_from": rel.effective_from,
                "effective_to": rel.effective_to,
                "active_on": day,
                "included": rel.active_on(day),
            }
        )
    return evidence


def compute_layers(
    state: Any, plan: PlanAggregate, day: str, channel: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """计算 ``day`` 的逐层额度。

    * ``channel`` 给定时只选取该渠道的数量上限；
    * ``account`` 给定时（执行场景）绑定该申报账户的持仓与董事年度限额；
      不给出时（计划查询）只绑定组合层级限额，账户级限额仅作展示证据。

    多层同时限制时取最小余量，即最严格结果。
    """

    rule = state.effective_rule(day)
    thr = _default_thresholds(rule)
    evidence = _clause_evidence(rule)
    trades = state.effective_trades(plan.plan_id)
    # 监管限额（90 日滚动 / 年度 25% / 持仓余量）按组合跨计划聚合，
    # 归属已在投影中按成交当日关系判定。
    group_trades = state.group_trades(plan.group_owner_id, plan.security_code)

    layers: list[dict[str, Any]] = []

    # 1) 计划额度 ----------------------------------------------------------
    used_plan = sum(t.qty for t in trades)
    plan_lim = plan.current.proposed_qty if plan.current else 0
    layers.append(
        {
            "key": "plan_remaining",
            "title": "计划剩余可减数量",
            "limit": plan_lim,
            "used": used_plan,
            "remaining": max(0, plan_lim - used_plan),
            "evidence": {
                "plan_version": plan.current.version_no if plan.current else None,
                "proposed_qty": plan_lim,
                "trades": [t.report_id for t in trades],
            },
        }
    )

    # 2) 已解禁持仓（执行时绑定申报账户；查询时按合并范围逐账户汇总） -------
    members = state.group_members(plan.group_owner_id, day)
    scoped_members = [account] if account in members else members
    holdings_items = []
    holdings_total = 0
    for m in scoped_members:
        info = member_unlocked(state, m, plan.security_code, day)
        sold = sum(t.qty for t in group_trades if t.shareholder_id == m)
        available = max(0, info["unlocked_qty"] - sold)
        holdings_total += available
        holdings_items.append(
            {
                "shareholder_id": m,
                "unlocked_qty": info["unlocked_qty"],
                "sold_total": sold,
                "available_qty": available,
                "batches": info["batches"],
            }
        )
    layers.append(
        {
            "key": "unlocked_holdings",
            "title": (
                f"申报账户 {account} 已解禁且未减持持仓"
                if account
                else "合并范围已解禁且未减持持仓"
            ),
            "limit": holdings_total,
            "used": 0,
            "remaining": holdings_total,
            "evidence": {
                "members": scoped_members,
                "scope": "account" if account else "group",
                "per_account": holdings_items,
                "relationships": relationship_evidence(state, plan, day),
            },
        }
    )

    # 3) 实际控制人 90 日滚动渠道上限 --------------------------------------
    owner = state.shareholders.get(plan.group_owner_id)
    is_controller = bool(owner and owner.is_controlling_person)
    window_days = thr["controller_calendar_days"]
    window_start = iso(parse_date(day) - timedelta(days=window_days - 1))
    channel_caps = [
        ("controller_secondary", "实际控制人连续%s日集中竞价上限" % window_days,
         "secondary", thr["controller_secondary_pct"]),
        ("controller_block", "实际控制人连续%s日大宗交易上限" % window_days,
         "block", thr["controller_block_pct"]),
    ]
    for key, title, ch, pct in channel_caps:
        applicable = is_controller and (channel is None or channel == ch)
        window_trades = (
            [
                t
                for t in group_trades
                if t.channel == ch and window_start <= t.trade_date <= day
            ]
            if applicable
            else []
        )
        used_ch = sum(t.qty for t in window_trades)
        limit = int(plan.share_capital * pct / 100) if applicable else None
        layers.append(
            {
                "key": key,
                "title": title,
                "applicable": applicable,
                "limit": limit,
                "used": used_ch,
                "remaining": (max(0, limit - used_ch) if limit is not None else None),
                "evidence": {
                    "rule": evidence,
                    "clause": f"{ch}: 任意连续{window_days}日不超过总股本{pct}%",
                    "window_start": window_start,
                    "window_end": day,
                    "share_capital": plan.share_capital,
                    "trades": [
                        {"report_id": t.report_id, "date": t.trade_date, "qty": t.qty,
                         "shareholder_id": t.shareholder_id}
                        for t in window_trades
                    ],
                    "not_applicable_reason": (
                        None if is_controller else "组合所有人非实际控制人/大股东"
                    ),
                },
            }
        )

    # 4) 董监高自然年 25%（账户级限额） ------------------------------------
    # 执行时只约束申报账户；计划查询时逐董事展示，但不参与组合级 binding。
    director_accounts = [account] if account else [
        m for m in members
        if state.shareholders.get(m) and state.shareholders[m].is_director
    ]
    director_layers = []
    for m in director_accounts:
        sh = state.shareholders.get(m)
        is_director = bool(sh and sh.is_director)
        base = sum(
            b.total_qty
            for b in state.batches.values()
            if b.shareholder_id == m
            and b.security_code == plan.security_code
            and b.source == "director_holding"
        )
        year_trades = account_trades_in_year(state, m, plan.security_code, day)
        used_ytd = sum(t.qty for t in year_trades)
        allowance = int(base * thr["director_annual_pct"] / 100)
        if not is_director:
            director_layers.append(
                {
                    "key": "director_annual",
                    "title": f"账户 {m} 非董监高，年度25%限额不适用",
                    "applicable": False,
                    "limit": None,
                    "used": used_ytd,
                    "remaining": None,
                    "evidence": {"not_applicable_reason": "申报账户非董监高"},
                }
            )
            continue
        director_layers.append(
            {
                "key": "director_annual",
                "title": f"董事 {m} 自然年度转让上限（持股25%）",
                "applicable": True,
                # 仅在绑定该申报账户执行时约束组合可减数量；组级查询仅展示
                "binds_group": account is not None,
                "limit": allowance,
                "used": used_ytd,
                "remaining": max(0, allowance - used_ytd),
                "evidence": {
                    "rule": evidence,
                    "clause": f"董监高每个自然年转让不超过所持股份{thr['director_annual_pct']}%",
                    "year": parse_date(day).year,
                    "base_qty": base,
                    "shareholder_id": m,
                    "trades": [
                        {"report_id": t.report_id, "plan_id": t.plan_id,
                         "date": t.trade_date, "qty": t.qty}
                        for t in year_trades
                    ],
                },
            }
        )
    layers.extend(director_layers)
    if not director_layers:
        layers.append(
            {
                "key": "director_annual",
                "title": "董事自然年度转让上限（持股25%）",
                "applicable": False,
                "limit": None,
                "used": 0,
                "remaining": None,
                "evidence": {"not_applicable_reason": "合并范围内无董事账户"},
            }
        )

    applicable = [
        l for l in layers
        if l.get("applicable", True) and l["remaining"] is not None
        and l.get("binds_group", True)
    ]
    if channel == "secondary":
        applicable = [l for l in applicable if l["key"] != "controller_block"]
    elif channel == "block":
        applicable = [l for l in applicable if l["key"] != "controller_secondary"]
    binding = min(applicable, key=lambda l: l["remaining"], default=None)
    allowed = binding["remaining"] if binding else 0
    return {
        "as_of_date": day,
        "channel": channel,
        "account": account,
        "rule": evidence,
        "members": members,
        "layers": layers,
        "binding_layer": binding["key"] if binding else None,
        "allowed_qty": allowed,
    }


# --------------------------------------------------------------------- 窗口/公告


def conflict_windows(state: Any, plan: PlanAggregate, day: str) -> list[dict[str, Any]]:
    """与计划执行区间重叠或覆盖判定日的窗口；``covers_today`` 为即期冲突。"""

    ver = plan.current
    start = ver.effective_from if ver else day
    end = ver.effective_to or "9999-12-31"
    result = []
    for w in state.windows.values():
        if w.security_code not in (plan.security_code, "*"):
            continue
        overlap = not (w.end_date < start or w.start_date > end)
        covers = w.covers(day)
        if overlap or covers:
            result.append(
                {
                    "window_id": w.window_id,
                    "title": w.title,
                    "start_date": w.start_date,
                    "end_date": w.end_date,
                    "blocking": w.blocking,
                    "overlaps_plan_period": overlap,
                    "covers_as_of_date": covers,
                    "note": w.note,
                }
            )
    return result


def announcement_effective_status(ann: Any, day: str) -> str:
    if ann.status in ("confirmed", "void"):
        return ann.status
    if day > ann.due_date:
        return "overdue"
    return "pending"


def pending_disclosures(state: Any, plan: PlanAggregate, day: str) -> list[dict[str, Any]]:
    result = []
    for ann in plan.announcements:
        eff = announcement_effective_status(ann, day)
        result.append(
            {
                "announcement_id": ann.announcement_id,
                "kind": ann.kind,
                "step_no": ann.step_no,
                "triggered_date": ann.triggered_date,
                "due_date": ann.due_date,
                "status": ann.status,
                "effective_status": eff,
                "confirmed_at": ann.confirmed_at,
                "gating": ann.kind in GATING_ANNOUNCEMENT_KINDS,
                "note": ann.note,
            }
        )
    return result


def gating_blockers(state: Any, plan: PlanAggregate, day: str) -> list[str]:
    """逾期未确认的披露流程 → 暂停后续放行。"""

    blockers = []
    for ann in pending_disclosures(state, plan, day):
        if ann["gating"] and ann["effective_status"] == "overdue":
            blockers.append(
                f"{ann['kind']} 公告 {ann['announcement_id']} 已逾期"
                f"（到期日 {ann['due_date']}）未确认，后续放行暂停"
            )
    return blockers


# --------------------------------------------------------------------- 放行结论


def evaluate_execution(
    state: Any, plan: PlanAggregate, day: str, channel: str, qty: int,
    account: str | None = None,
) -> dict[str, Any]:
    """执行前放行：硬性闸门 + 逐层数量，给出批准/拒绝与理由。

    ``account`` 为申报账户，账户级持仓与董事年度限额按该账户绑定。
    """

    reasons: list[str] = []
    ver = plan.current
    if plan.state != "active":
        reasons.append(f"计划状态为 {plan.state}，非 active 不得执行")
    if ver is not None and not (ver.effective_from <= day <= (ver.effective_to or "9999-12-31")):
        reasons.append(
            f"{day} 不在计划当前版本执行区间 "
            f"[{ver.effective_from}, {ver.effective_to or '∞'}]"
        )
    if channel not in CHANNEL_CLASS:
        reasons.append(f"未知交易渠道 {channel!r}")
    if qty <= 0:
        reasons.append("申报数量必须为正整数")
    members = state.group_members(plan.group_owner_id, day)
    if account is not None and account not in members:
        reasons.append(
            f"申报账户 {account} 在 {day} 不属于计划合并范围"
        )

    for w in conflict_windows(state, plan, day):
        if w["blocking"] and w["covers_as_of_date"]:
            reasons.append(f"当日处于禁止交易窗口：{w['title']}（{w['start_date']}~{w['end_date']}）")
    reasons.extend(gating_blockers(state, plan, day))

    layers = compute_layers(
        state, plan, day,
        channel if channel in CHANNEL_CLASS else None,
        account=account,
    )
    if channel in CHANNEL_CLASS and qty > 0 and qty > layers["allowed_qty"]:
        reasons.append(
            f"申报 {qty} 股超过最严层 {layers['binding_layer']} 允许的 "
            f"{layers['allowed_qty']} 股"
        )

    return {
        "result": "approved" if not reasons else "denied",
        "day": day,
        "channel": channel,
        "account": account,
        "requested_qty": qty,
        "allowed_qty": layers["allowed_qty"],
        "binding_layer": layers["binding_layer"],
        "reasons": reasons,
        "layers": layers,
        "conflict_windows": conflict_windows(state, plan, day),
    }
