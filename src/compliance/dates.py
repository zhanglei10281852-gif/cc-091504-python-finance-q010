"""日期工具：领域内统一使用 ISO 字符串 ``YYYY-MM-DD``，比较时转为 ``date``。"""

from __future__ import annotations

from datetime import date, timedelta

from compliance.errors import ValidationError

DATE_FMT = "%Y-%m-%d"


def parse_date(value: str | date, field: str = "date") -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 字符串")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 日期格式非法: {value!r}") from exc


def iso(value: date) -> str:
    return value.strftime(DATE_FMT)


def add_days(value: str | date, days: int) -> str:
    return iso(parse_date(value) + timedelta(days=days))


def ordinal_key(value: str | date) -> int:
    """交易日历序，用于成交回报按业务时间排序与重放。"""

    return parse_date(value).toordinal()
