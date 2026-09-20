"""领域模型（由事件重放重建的读取侧数据结构）。

所有数量单位为“股”，金额比例为百分数字典 ``{"percent": 1.0}``。
模型本身不做业务校验，校验集中在 :mod:`compliance.service`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RelationshipType = Literal["controller", "family", "employee_platform"]
PlanState = Literal[
    "draft", "review", "active", "suspended", "closed", "rejected"
]


@dataclass
class Shareholder:
    shareholder_id: str
    name: str
    kind: str  # natural_person / company / employee_platform / family_account
    is_controlling_person: bool = False
    is_director: bool = False


@dataclass
class Relationship:
    """有生效区间的股东归属关系。

    ``rel_type`` 描述 ``subject_id`` 相对 ``group_owner_id`` 的身份
    （实际控制人 / 亲属 / 员工持股平台）。只在 ``[effective_from, effective_to)``
    内参与合并：关系变化只影响生效后的合并范围，历史成交保留当时归属。
    """

    rel_id: str
    subject_id: str
    group_owner_id: str
    rel_type: RelationshipType
    effective_from: str
    effective_to: str | None = None
    note: str = ""

    def active_on(self, day: str) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day < self.effective_to


@dataclass
class SecurityBatch:
    batch_id: str
    shareholder_id: str
    security_code: str
    source: str  # IPO_lockup / director_holding / private_placement / incentive_award
    total_qty: int
    issuer_lockup_until: str | None = None  # 发行人承诺/法定锁定到期日
    note: str = ""


@dataclass
class UnlockCondition:
    """限售来源的解禁条件：到期日解禁，或满足考核条件后解禁。"""

    unlock_id: str
    batch_id: str
    condition_type: Literal["date", "performance"]
    unlock_date: str  # date 类型：到期日；performance 类型：满足条件的生效日
    tranche_qty: int
    satisfied: bool = True
    note: str = ""


@dataclass
class Window:
    """窗口期。``blocking=True`` 为禁止交易的敏感期（如业绩预告）。"""

    window_id: str
    security_code: str
    title: str
    start_date: str
    end_date: str
    blocking: bool = True
    note: str = ""

    def covers(self, day: str) -> bool:
        return self.start_date <= day <= self.end_date


@dataclass
class RuleVersion:
    """监管/内部规则版本，按生效日选取；数量规则取所有命中规则的最严格结果。"""

    rule_id: str
    version: str
    effective_from: str
    effective_to: str | None
    name: str
    # 控制器/大股东：任意连续 90 日
    controller_calendar_days: int = 90
    controller_secondary_pct: float = 1.0  # 集中竞价 1%
    controller_block_pct: float = 2.0  # 大宗交易 2%
    # 董监高：每个自然年可转让 25%
    director_annual_pct: float = 25.0
    # 预披露：计划减持数量占公司总股本比例阈值
    pre_disclosure_threshold_pct: float = 1.0
    # 减持达总股本 1% 需在事实发生次日通知并公告
    holding_disclosure_step_pct: float = 1.0
    # 公告创建后确认时限（自然日），逾期未确认暂停后续放行
    announcement_confirm_days: int = 2
    note: str = ""


@dataclass
class PlanVersion:
    """减持计划的一个不可变版本（提交与每次变更都会生成新版本）。"""

    version_no: int
    state: PlanState
    effective_from: str
    effective_to: str | None
    proposed_qty: int
    channels: list[str]  # secondary / block
    security_code: str
    reason: str = ""
    created_by: str = ""


@dataclass
class Trade:
    report_id: str
    plan_id: str
    shareholder_id: str
    batch_id: str
    security_code: str
    channel: str
    trade_date: str
    qty: int
    price: float | None
    replaced: bool = False  # True 表示该回报已被撤回
    replaces: str | None = None  # 若为替换回报，指向原 report_id
    applied: bool = False  # 是否已经进入累计扣减（去重水位标记）


@dataclass
class Announcement:
    announcement_id: str
    plan_id: str
    kind: str  # pre_disclosure / holding_step / plan_change / plan_termination
    triggered_date: str
    due_date: str
    status: Literal["pending", "confirmed", "overdue", "void"] = "pending"
    confirmed_at: str | None = None
    step_no: int | None = None  # holding_step：累计减持触及的第几个披露阶梯
    note: str = ""


@dataclass
class PlanAggregate:
    plan_id: str
    group_owner_id: str
    security_code: str
    share_capital: int  # 公司总股本，用于百分比阈值
    current: PlanVersion | None = None
    versions: list[PlanVersion] = field(default_factory=list)
    submitted_version: int | None = None  # 送审版本号；送审中普通编辑不得覆盖
    review_return_state: PlanState | None = None  # 撤回/驳回时回到的状态
    trades: list[Trade] = field(default_factory=list)
    announcements: list[Announcement] = field(default_factory=list)

    @property
    def version(self) -> int:
        return self.current.version_no if self.current else 0

    @property
    def state(self) -> PlanState | None:
        return self.current.state if self.current else None


def to_dict(obj: Any) -> Any:
    """轻量序列化，供查询接口返回。"""

    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, list):
        return [to_dict(v) for v in obj]
    return obj
