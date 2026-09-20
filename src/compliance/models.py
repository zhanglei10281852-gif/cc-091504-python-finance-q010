"""领域模型：账户、股东关系、证券批次、规则版本、减持计划、成交回报与公告流程。

设计要点：
- 所有业务时间使用带时区的 ISO 8601 时间戳（内部统一 UTC）。
- 成交回报在记录时冻结当时的合并归属（attribution），历史成交不因后续
  关系变化而改属；累计额度通过对成交事件的确定性重放得到。
- 监管规则按 (rule_id, version, effective_from) 版本化，决策时取业务时间
  点已生效的最新版本，并把所用版本写入决策证据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# 与 reference/domain.json 对齐的枚举
PLAN_STATES = ("draft", "review", "active", "suspended", "closed")
ACCOUNT_KINDS = ("controller", "director", "relative", "employee_platform", "other")
RULE_TYPES = ("rolling_window_cap", "sensitive_window", "disclosure_threshold")
ANNOUNCEMENT_STATES = ("pending", "confirmed", "overdue")


class DomainError(Exception):
    """业务校验失败。status 供 HTTP 层映射，payload 可携带决策等结构化信息。"""

    def __init__(self, code: str, message: str, status: int = 400, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.payload = payload or {}

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.payload}}


def parse_ts(value) -> datetime:
    """解析 ISO 8601 时间；纯日期按 UTC 零点处理，无时区按 UTC 处理。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if len(s) == 10:  # YYYY-MM-DD
            s = s + "T00:00:00+00:00"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    else:
        raise DomainError("bad_time", f"无法解析时间: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _require(data: dict, *names: str) -> None:
    missing = [n for n in names if data.get(n) is None]
    if missing:
        raise DomainError("missing_field", f"缺少必填字段: {', '.join(missing)}")


@dataclass
class Account:
    account_id: str
    name: str
    kind: str  # controller / director / relative / employee_platform / other
    roles: list[str]  # 敏感窗口按角色匹配，如 director / supervisor / executive
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "name": self.name,
            "kind": self.kind,
            "roles": list(self.roles),
            "created_at": iso(self.created_at),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        return cls(d["account_id"], d["name"], d["kind"], list(d.get("roles", [])),
                   parse_ts(d["created_at"]))


@dataclass
class Relationship:
    """股东关系（一致行动/合并计算边）。effective_to 为 None 表示持续有效。

    关系只在 [effective_from, effective_to) 内参与合并范围解析；
    变化不影响历史成交已冻结的归属。
    """
    relationship_id: str
    account_a: str
    account_b: str
    type: str  # controller / family / employee_platform
    effective_from: datetime
    effective_to: datetime | None = None

    def active_at(self, at: datetime) -> bool:
        return self.effective_from <= at and (self.effective_to is None or at < self.effective_to)

    def to_dict(self) -> dict:
        return {
            "relationship_id": self.relationship_id,
            "account_a": self.account_a,
            "account_b": self.account_b,
            "type": self.type,
            "effective_from": iso(self.effective_from),
            "effective_to": iso(self.effective_to) if self.effective_to else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Relationship":
        return cls(d["relationship_id"], d["account_a"], d["account_b"], d["type"],
                   parse_ts(d["effective_from"]),
                   parse_ts(d["effective_to"]) if d.get("effective_to") else None)


@dataclass
class Lot:
    """证券批次：某一账户持有的、有特定限售来源与解禁条件的一批股份。"""
    lot_id: str
    account_id: str
    source: str  # IPO_lockup / director_holding / private_placement / incentive_award
    quantity: int
    unlock_date: datetime
    conditions: list[str] = field(default_factory=list)
    conditions_satisfied: list[str] = field(default_factory=list)

    def unlock_status(self, at: datetime) -> tuple[bool, list[str]]:
        reasons = []
        if self.unlock_date > at:
            reasons.append(f"解禁日期 {iso(self.unlock_date)} 未到")
        missing = [c for c in self.conditions if c not in self.conditions_satisfied]
        if missing:
            reasons.append(f"解禁条件未满足: {', '.join(missing)}")
        return (not reasons, reasons)

    def to_dict(self) -> dict:
        return {
            "lot_id": self.lot_id,
            "account_id": self.account_id,
            "source": self.source,
            "quantity": self.quantity,
            "unlock_date": iso(self.unlock_date),
            "conditions": list(self.conditions),
            "conditions_satisfied": list(self.conditions_satisfied),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Lot":
        return cls(d["lot_id"], d["account_id"], d["source"], int(d["quantity"]),
                   parse_ts(d["unlock_date"]), list(d.get("conditions", [])),
                   list(d.get("conditions_satisfied", [])))


@dataclass
class RuleVersion:
    """监管规则的一个版本。同一 rule_id 可有多个版本，按 effective_from 生效。"""
    rule_id: str
    version: str
    type: str  # rolling_window_cap / sensitive_window / disclosure_threshold
    effective_from: datetime
    params: dict
    order: int = 0  # 注册顺序，用于同 effective_from 时的稳定取舍

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "type": self.type,
            "effective_from": iso(self.effective_from),
            "params": dict(self.params),
            "order": self.order,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RuleVersion":
        return cls(d["rule_id"], d["version"], d["type"], parse_ts(d["effective_from"]),
                   dict(d.get("params", {})), int(d.get("order", 0)))


@dataclass
class SensitiveEvent:
    """敏感事件（如业绩预告公告日），与 sensitive_window 规则共同生成限制窗口。"""
    event_id: str
    event_type: str  # earnings_forecast / earnings_report / ...
    announce_date: datetime
    applies_to_roles: list[str]

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "announce_date": iso(self.announce_date),
            "applies_to_roles": list(self.applies_to_roles),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SensitiveEvent":
        return cls(d["event_id"], d["event_type"], parse_ts(d["announce_date"]),
                   list(d.get("applies_to_roles", [])))


@dataclass
class Window:
    """人工登记的限制窗口（如重大事项停牌期间）。"""
    window_id: str
    account_id: str
    start: datetime
    end: datetime
    reason: str

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id,
            "account_id": self.account_id,
            "start": iso(self.start),
            "end": iso(self.end),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Window":
        return cls(d["window_id"], d["account_id"], parse_ts(d["start"]),
                   parse_ts(d["end"]), d["reason"])


@dataclass
class Plan:
    plan_id: str
    account_id: str
    lot_ids: list[str]
    planned_quantity: int
    window_start: datetime
    window_end: datetime
    state: str = "draft"
    revision: int = 1
    created_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "account_id": self.account_id,
            "lot_ids": list(self.lot_ids),
            "planned_quantity": self.planned_quantity,
            "window_start": iso(self.window_start),
            "window_end": iso(self.window_end),
            "state": self.state,
            "revision": self.revision,
            "created_at": iso(self.created_at) if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(d["plan_id"], d["account_id"], list(d["lot_ids"]),
                   int(d["planned_quantity"]), parse_ts(d["window_start"]),
                   parse_ts(d["window_end"]), d["state"], int(d["revision"]),
                   parse_ts(d["created_at"]) if d.get("created_at") else None)


@dataclass
class Execution:
    """成交回报。report_id 为幂等键；attribution 为成交时冻结的合并归属。"""
    report_id: str
    plan_id: str
    account_id: str
    quantity: int
    trade_time: datetime
    attribution: list[str]
    allocations: list[dict]  # [{"lot_id": ..., "quantity": ...}]
    seq: int = 0
    revoked: bool = False
    revoked_at: datetime | None = None
    revoke_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "plan_id": self.plan_id,
            "account_id": self.account_id,
            "quantity": self.quantity,
            "trade_time": iso(self.trade_time),
            "attribution": list(self.attribution),
            "allocations": [dict(a) for a in self.allocations],
            "seq": self.seq,
            "revoked": self.revoked,
            "revoked_at": iso(self.revoked_at) if self.revoked_at else None,
            "revoke_reason": self.revoke_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Execution":
        return cls(d["report_id"], d["plan_id"], d["account_id"], int(d["quantity"]),
                   parse_ts(d["trade_time"]), list(d["attribution"]),
                   [dict(a) for a in d.get("allocations", [])], int(d.get("seq", 0)),
                   bool(d.get("revoked", False)),
                   parse_ts(d["revoked_at"]) if d.get("revoked_at") else None,
                   d.get("revoke_reason"))


@dataclass
class Announcement:
    """触碰披露阈值后生成的有时限公告流程。"""
    announcement_id: str
    plan_id: str
    rule_id: str
    rule_version: str
    crossing_no: int  # 第几次跨越阈值（累计 // 阈值）
    threshold_quantity: int
    created_at: datetime
    deadline: datetime
    status: str = "pending"  # pending / confirmed / overdue
    confirmed_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "announcement_id": self.announcement_id,
            "plan_id": self.plan_id,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "crossing_no": self.crossing_no,
            "threshold_quantity": self.threshold_quantity,
            "created_at": iso(self.created_at),
            "deadline": iso(self.deadline),
            "status": self.status,
            "confirmed_at": iso(self.confirmed_at) if self.confirmed_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Announcement":
        return cls(d["announcement_id"], d["plan_id"], d["rule_id"], d["rule_version"],
                   int(d["crossing_no"]), int(d["threshold_quantity"]),
                   parse_ts(d["created_at"]), parse_ts(d["deadline"]), d["status"],
                   parse_ts(d["confirmed_at"]) if d.get("confirmed_at") else None)
