"""时间范围解析：把「今天 / 本月 / 上个月 / 2026-09」算成 unix 秒区间。

为什么要这个：agent 只用 prompt 里的 ``[时间] unix=... utcOffset=...``，
让它自己推算「本月 1 号 0 点」很容易算错（月初、跨年、时区各错一次），
而统计的区间一旦错了，整份报表就是错的。这里用 ``datetime`` 算，避免手算。
"""

from __future__ import annotations

import calendar
import time
from datetime import datetime, timedelta, timezone

#: 支持的范围表达式
RANGES = ("today", "yesterday", "this-week", "last-week", "this-month", "last-month", "this-year")


class RangeError(ValueError):
    """范围表达式不合法（由 CLI 翻译成给 agent 的提示）。"""


def _tz(offset_minutes: int | None):
    if offset_minutes is None:
        return datetime.now().astimezone().tzinfo or timezone.utc
    return timezone(timedelta(minutes=int(offset_minutes)))


def _month_bounds(year: int, month: int, tz) -> tuple[datetime, datetime]:
    first = datetime(year, month, 1, tzinfo=tz)
    last_day = calendar.monthrange(year, month)[1]
    # 用「下月 1 号 0 点」作为开区间右端，再减 1 秒得到闭区间末尾
    if month == 12:
        nxt = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        nxt = datetime(year, month + 1, 1, tzinfo=tz)
    return first, nxt - timedelta(seconds=1)


def resolve_range(expr: str, *, tz_offset: int | None = None, now: float | None = None) -> tuple[int, int, str]:
    """把范围表达式解析成 ``(start_unix, end_unix, label)``。

    支持：``today`` / ``yesterday`` / ``this-week`` / ``last-week`` /
    ``this-month`` / ``last-month`` / ``this-year`` / ``YYYY-MM``（如 ``2026-09``）。
    """
    tz = _tz(tz_offset)
    moment = datetime.fromtimestamp(now if now is not None else time.time(), tz=tz)
    key = (expr or "").strip().lower()

    if key == "today":
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        label = start.strftime("%Y-%m-%d")
    elif key == "yesterday":
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        label = start.strftime("%Y-%m-%d")
    elif key in ("this-week", "last-week"):
        # 周一为一周起点（国内习惯）
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=moment.weekday())
        if key == "last-week":
            start -= timedelta(days=7)
        end = start + timedelta(days=7) - timedelta(seconds=1)
        label = f"{start.strftime('%Y-%m-%d')} 起一周"
    elif key == "this-month":
        start, end = _month_bounds(moment.year, moment.month, tz)
        label = moment.strftime("%Y-%m")
    elif key == "last-month":
        year, month = (moment.year - 1, 12) if moment.month == 1 else (moment.year, moment.month - 1)
        start, end = _month_bounds(year, month, tz)
        label = f"{year:04d}-{month:02d}"
    elif key == "this-year":
        start = datetime(moment.year, 1, 1, tzinfo=tz)
        end = datetime(moment.year, 12, 31, 23, 59, 59, tzinfo=tz)
        label = str(moment.year)
    else:
        # YYYY-MM
        try:
            year_text, month_text = key.split("-")
            year, month = int(year_text), int(month_text)
            if not 1 <= month <= 12:
                raise ValueError
        except ValueError:
            raise RangeError(
                f"不认识的时间范围 '{expr}'；可用：{', '.join(RANGES)} 或 2026-09 这种"
            ) from None
        start, end = _month_bounds(year, month, tz)
        label = f"{year:04d}-{month:02d}"

    return int(start.timestamp()), int(end.timestamp()), label
