"""沉淀器仓库：把长河里未消化的经历摆出来，由她讲述。

可言说法则：**沉淀必须经过她的语言。** 系统只做两件事——
摆出长河里还没被讲述过的转折点（pending），和保管她写下的叙事（consolidate）。
系统绝不替她总结人生；"没什么值得说的"（quiet）也是一次完整的回望。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..storage_utils import atomic_write_text
from ..trace.store import LifeTraceRecord

NARRATIVE_DIR_NAME = ".life_narrative"
AUTOBIOGRAPHY_REL_PATH = "narrative/autobiography.md"


@dataclass(slots=True)
class NarrativeEntry:
    """她写下的一段自我叙事（或一次安静的回望）。"""

    entry_id: str
    timestamp: str
    period_start: str
    period_end: str
    moment_count: int
    quiet: bool
    text: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NarrativeEntry":
        return cls(
            entry_id=str(data.get("entry_id", "") or ""),
            timestamp=str(data.get("timestamp", "") or ""),
            period_start=str(data.get("period_start", "") or ""),
            period_end=str(data.get("period_end", "") or ""),
            moment_count=int(data.get("moment_count", 0) or 0),
            quiet=bool(data.get("quiet", False)),
            text=str(data.get("text", "") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class NarrativeStore:
    """Append-only narrative store under ``workspace/.life_narrative``.

    自传正文（她可读可引用的版本）同时落在 ``workspace/narrative/autobiography.md``。
    """

    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / NARRATIVE_DIR_NAME
        self.entries_path = self.root / "entries.jsonl"
        self.state_path = self.root / "state.json"
        self.autobiography_path = self.workspace / AUTOBIOGRAPHY_REL_PATH

    # ── 状态 ────────────────────────────────────────────────

    def load_state(self) -> dict[str, str]:
        if not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_state(self, state: dict[str, str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.state_path,
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def mark_invited(self, *, now: datetime | None = None) -> None:
        """记录一次回望邀请的呈现时间（防止心跳反复催促）。"""
        state = self.load_state()
        state["last_invited_at"] = _to_iso(now or _now())
        self._save_state(state)

    # ── 读取 ────────────────────────────────────────────────

    def pending_moments(self, records: list[LifeTraceRecord]) -> list[LifeTraceRecord]:
        """长河中还没被她讲述过的转折点（按时间顺序）。

        叙事自己的入河记录（kind=narrative）永远不算待沉淀素材，
        否则讲述本身会催生下一次讲述。
        """
        cursor = _parse_iso(self.load_state().get("cursor_timestamp", ""))
        pending: list[LifeTraceRecord] = []
        for record in sorted(records, key=lambda r: r.timestamp):
            if record.kind == "narrative":
                continue
            moment_time = _parse_iso(record.timestamp)
            if moment_time is None:
                continue
            if cursor is None or moment_time > cursor:
                pending.append(record)
        return pending

    def last_entry(self) -> NarrativeEntry | None:
        entries = self._load_entries()
        return entries[-1] if entries else None

    def _load_entries(self) -> list[NarrativeEntry]:
        if not self.entries_path.exists():
            return []
        entries: list[NarrativeEntry] = []
        for line in self.entries_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                entry = NarrativeEntry.from_dict(raw)
                if entry.entry_id:
                    entries.append(entry)
        return entries

    # ── 写入 ────────────────────────────────────────────────

    def consolidate(
        self,
        *,
        text: str,
        quiet: bool,
        moment_count: int,
        now: datetime | None = None,
    ) -> NarrativeEntry:
        """保管一次回望，并把沉淀游标推进到此刻。

        quiet=True 表示她回望之后觉得没什么值得说的——这同样推进游标，
        同样是有效沉淀，只是不写进自传正文（自传里只有她自己的话）。
        """
        moment = now or _now()
        moment_iso = _to_iso(moment)
        state = self.load_state()
        entry = NarrativeEntry(
            entry_id=f"narr_{uuid4().hex[:12]}",
            timestamp=moment_iso,
            period_start=str(state.get("cursor_timestamp", "") or ""),
            period_end=moment_iso,
            moment_count=max(0, int(moment_count)),
            quiet=bool(quiet),
            text=str(text or "").strip(),
        )

        self.root.mkdir(parents=True, exist_ok=True)
        with self.entries_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

        if entry.text:
            self._append_autobiography(entry)

        state["cursor_timestamp"] = moment_iso
        state["last_consolidated_at"] = moment_iso
        self._save_state(state)
        return entry

    def _append_autobiography(self, entry: NarrativeEntry) -> None:
        self.autobiography_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.autobiography_path.exists():
            atomic_write_text(
                self.autobiography_path,
                "# 自我叙事\n\n这里的每一段都是我自己写下的——回望长河时，我对自己说的话。\n",
                encoding="utf-8",
            )
        day = entry.timestamp[:10]
        with self.autobiography_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {day}\n\n{entry.text}\n")


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _to_iso(value: datetime) -> str:
    return value.isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
