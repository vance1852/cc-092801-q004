"""可注入的 UTC 时钟与业务日期工具。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


class SystemClock:
    def today(self) -> date:
        return datetime.now(timezone.utc).date()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FrozenClock:
    current: date

    def today(self) -> date:
        return self.current

    def now(self) -> datetime:
        return datetime(self.current.year, self.current.month, self.current.day, tzinfo=timezone.utc)

    def advance(self, days: int = 0) -> None:
        self.current += timedelta(days=days)


def date_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 日期")
    text = value.strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
