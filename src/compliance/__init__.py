"""限售股减持合规领域包。

对外只暴露 :class:`~compliance.service.ComplianceService` 与持久化入口，
HTTP 层与测试统一通过该门面访问领域能力。
"""

from __future__ import annotations

from compliance.service import ComplianceService
from compliance.store import EventStore

__all__ = ["ComplianceService", "EventStore"]
