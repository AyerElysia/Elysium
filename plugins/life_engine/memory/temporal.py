"""Deterministic temporal hints for life-engine memory documents.

The parsers in this module deliberately never read the system clock.  Relative
phrases are only resolved when the caller supplies ``now`` (or a callable
returning a date/datetime).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Callable
from zoneinfo import ZoneInfo


_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(?P<year>\d{4})/(?P<month>\d{1,2})/(?P<day>\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(?P<year>\d{4})[_.](?P<month>\d{1,2})[_.](?P<day>\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日?(?!\d)"),
    re.compile(r"(?<!\d)(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})(?!\d)"),
)

_RELATIVE_PATTERNS = (
    (re.compile(r"\b(?:the\s+)?day\s+before\s+yesterday\b", re.IGNORECASE), -2),
    (re.compile(r"\b(?:the\s+)?day\s+after\s+tomorrow\b", re.IGNORECASE), 2),
    (re.compile(r"\b(today)\b", re.IGNORECASE), 0),
    (re.compile(r"\b(yesterday)\b", re.IGNORECASE), -1),
    (re.compile(r"\b(tomorrow)\b", re.IGNORECASE), 1),
    (re.compile(r"\b(last|previous)\s+week\b", re.IGNORECASE), -7),
    (re.compile(r"\b(next|following)\s+week\b", re.IGNORECASE), 7),
    (re.compile(r"今日|今天"), 0),
    (re.compile(r"昨日|昨天"), -1),
    (re.compile(r"明日|明天"), 1),
    (re.compile(r"上周|上星期"), -7),
    (re.compile(r"下周|下星期"), 7),
    (re.compile(r"大前天"), -3),
    (re.compile(r"前天"), -2),
    (re.compile(r"后天"), 2),
)

_RELATIVE_NUMBER_PATTERNS = (
    (re.compile(r"(?<!\d)(?P<days>\d+)\s*(?:days?|d)\s*ago\b", re.IGNORECASE), -1),
    (re.compile(r"(?<!\d)(?P<days>\d+)\s*天前"), -1),
    (re.compile(r"(?<!\d)(?P<days>\d+)\s*(?:days?|d)\s*(?:later|after|from\s+now)\b", re.IGNORECASE), 1),
    (re.compile(r"(?<!\d)(?P<days>\d+)\s*天后"), 1),
)


def _coerce_timezone(value: tzinfo | str | None) -> tzinfo | None:
    if value is None or isinstance(value, tzinfo):
        return value
    try:
        return ZoneInfo(str(value))
    except Exception:
        return None


def _coerce_now(now: Any, tz: tzinfo | str | None) -> date | None:
    """Convert an injected clock value to a local date without using time.time."""
    if callable(now):
        now = now()
    if isinstance(now, datetime):
        target_tz = _coerce_timezone(tz)
        if target_tz is not None:
            if now.tzinfo is None:
                now = now.replace(tzinfo=target_tz)
            else:
                now = now.astimezone(target_tz)
        return now.date()
    if isinstance(now, date):
        return now
    return None


def _valid_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_explicit_date(value: str | None) -> date | None:
    """Return the first valid explicit calendar date in ``value``.

    Supported forms include ISO dates, slash-separated dates, Chinese dates,
    and compact ``YYYYMMDD`` values commonly used in filenames.
    """
    text = str(value or "")
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            parsed = _valid_date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
            if parsed is not None:
                return parsed
    return None


def parse_relative_date(
    value: str | None,
    *,
    now: date | datetime | Callable[[], date | datetime] | None = None,
    tz: tzinfo | str | None = None,
) -> date | None:
    """Resolve a relative date only against an explicitly injected ``now``."""
    base = _coerce_now(now, tz)
    if base is None:
        return None
    text = str(value or "")
    for pattern, offset in _RELATIVE_PATTERNS:
        if pattern.search(text):
            return base + timedelta(days=offset)
    for pattern, direction in _RELATIVE_NUMBER_PATTERNS:
        match = pattern.search(text)
        if match:
            return base + timedelta(days=direction * int(match.group("days")))
    return None


def parse_temporal_date(
    path: str | None = "",
    title: str | None = "",
    content: str | None = "",
    *,
    now: date | datetime | Callable[[], date | datetime] | None = None,
    tz: tzinfo | str | None = None,
) -> date | None:
    """Find a deterministic date hint in path, title, or document content.

    Explicit dates have priority over relative phrases.  Fields are scanned in
    path, title, content order.  When no explicit date is present, relative
    phrases are resolved against ``now``; if no clock is supplied, ``None`` is
    returned instead of guessing the current date.
    """
    fields = (path, title, content)
    for value in fields:
        parsed = parse_explicit_date(value)
        if parsed is not None:
            return parsed
    for value in fields:
        parsed = parse_relative_date(value, now=now, tz=tz)
        if parsed is not None:
            return parsed
    return None


def extract_document_date(
    path: str | None = "",
    title: str | None = "",
    content: str | None = "",
    *,
    now: date | datetime | Callable[[], date | datetime] | None = None,
    tz: tzinfo | str | None = None,
) -> date | None:
    """Alias for :func:`parse_temporal_date` used by indexing callers."""
    return parse_temporal_date(path, title, content, now=now, tz=tz)


# Short aliases keep the small pure API convenient for callers and tests.
parse_date = parse_explicit_date
parse_date_from_text = parse_explicit_date


__all__ = [
    "extract_document_date",
    "parse_date",
    "parse_date_from_text",
    "parse_explicit_date",
    "parse_relative_date",
    "parse_temporal_date",
]
