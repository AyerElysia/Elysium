"""Read-only migration of Diary Plugin Markdown into legacy witness records."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from ..memory.service import LifeMemoryService

logger = get_logger("life_engine.legacy_diary")
_ENTRY_PATTERN = re.compile(
    r"\*\*\[(?P<time>\d{2}:\d{2})\]\*\*\s*"
    r"(?P<content>.+?)(?=\s*\*\*\[\d{2}:\d{2}\]\*\*|\Z)",
    flags=re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class LegacyDiaryEntry:
    source_path: str
    source_hash: str
    migration_key: str
    content: str
    valid_from: str
    recorded_at: str


def parse_legacy_diary_file(path: Path, *, root: Path) -> list[LegacyDiaryEntry]:
    """Parse old Markdown without rewriting it or inferring missing provenance."""

    raw = path.read_text(encoding="utf-8", errors="replace")
    relative = path.relative_to(root).as_posix()
    source_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    try:
        date_value = datetime.strptime(path.stem, "%Y-%m-%d").date()
    except ValueError:
        logger.warning(f"跳过无法识别日期的旧日记: {relative}")
        return []
    entries = []
    duplicate_ordinals: dict[tuple[str, str], int] = {}
    for match in _ENTRY_PATTERN.finditer(raw):
        content = match.group("content").strip()
        if not content:
            continue
        hour, minute = (int(value) for value in match.group("time").split(":"))
        occurred = datetime.combine(date_value, datetime.min.time()).replace(
            hour=hour,
            minute=minute,
        )
        occurred_text = occurred.isoformat()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        duplicate_key = (occurred_text, content_hash)
        duplicate_ordinal = duplicate_ordinals.get(duplicate_key, 0)
        duplicate_ordinals[duplicate_key] = duplicate_ordinal + 1
        migration_key = hashlib.sha256(
            (
                f"{relative}\0{occurred_text}\0{content_hash}\0"
                f"{duplicate_ordinal}"
            ).encode("utf-8")
        ).hexdigest()
        entries.append(
            LegacyDiaryEntry(
                source_path=relative,
                source_hash=source_hash,
                migration_key=migration_key,
                content=content,
                valid_from=occurred_text,
                recorded_at=occurred_text,
            )
        )
    return entries


async def migrate_legacy_diaries(
    memory: LifeMemoryService,
    source_root: str | Path,
) -> int:
    """Idempotently register all old entries as lower-provenance witnesses."""

    root = Path(source_root).resolve()
    if not root.exists() or not root.is_dir():
        return 0
    files = await asyncio.to_thread(lambda: sorted(root.rglob("*.md")))
    migrated = 0
    for path in files:
        entries = await asyncio.to_thread(parse_legacy_diary_file, path, root=root)
        for entry in entries:
            witness = await memory.migrate_legacy_witness(
                migration_key=entry.migration_key,
                source_path=entry.source_path,
                source_hash=entry.source_hash,
                content=entry.content,
                valid_from=entry.valid_from,
                recorded_at=entry.recorded_at,
            )
            if witness is not None:
                migrated += 1
    return migrated


__all__ = [
    "LegacyDiaryEntry",
    "migrate_legacy_diaries",
    "parse_legacy_diary_file",
]
