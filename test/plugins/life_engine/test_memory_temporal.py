"""Tests for deterministic document date extraction."""

from __future__ import annotations

from datetime import date, datetime

from plugins.life_engine.memory.temporal import (
    parse_explicit_date,
    parse_relative_date,
    parse_temporal_date,
)


def test_explicit_dates_from_path_title_and_body() -> None:
    assert parse_explicit_date("notes/2026-07-20.md") == date(2026, 7, 20)
    assert parse_explicit_date("2026/7/3 主题") == date(2026, 7, 3)
    assert parse_explicit_date("记录于2026年7月3日") == date(2026, 7, 3)
    assert parse_explicit_date("snapshot_20260703.md") == date(2026, 7, 3)
    assert parse_temporal_date(
        "notes/no-date.md",
        "标题 2026年7月4日",
        "正文 2026-07-05",
    ) == date(2026, 7, 4)


def test_invalid_or_unknown_dates_return_none() -> None:
    assert parse_explicit_date("notes/2026-02-31.md") is None
    assert parse_temporal_date("notes/no-date.md", "无日期", "没有明确时间") is None
    assert parse_relative_date("昨天") is None


def test_relative_dates_require_injected_now_and_support_timezone() -> None:
    now = datetime(2026, 7, 20, 0, 30)
    assert parse_relative_date("昨天", now=now) == date(2026, 7, 19)
    assert parse_relative_date("3 days ago", now=lambda: date(2026, 7, 20)) == date(2026, 7, 17)
    assert parse_relative_date("下周", now=date(2026, 7, 20)) == date(2026, 7, 27)
    assert parse_temporal_date("daily.md", "", "今天的记录", now=date(2026, 7, 20)) == date(2026, 7, 20)
