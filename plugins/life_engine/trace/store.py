"""Persistent file trace storage for life_engine workspace files."""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


TRACE_DIR_NAME = ".life_trace"
TRACE_VERSION = 1


@dataclass(slots=True)
class LifeTraceRecord:
    """One tracked workspace file change."""

    trace_id: str
    timestamp: str
    path: str
    operation: str
    tool_name: str
    actor: str
    reason: str
    before_exists: bool
    after_exists: bool
    before_hash: str
    after_hash: str
    before_size: int
    after_size: int
    diff_path: str
    source_event_id: str = ""
    stream_id: str = ""
    version: int = TRACE_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LifeTraceRecord":
        return cls(
            trace_id=str(data.get("trace_id", "") or ""),
            timestamp=str(data.get("timestamp", "") or ""),
            path=str(data.get("path", "") or ""),
            operation=str(data.get("operation", "") or ""),
            tool_name=str(data.get("tool_name", "") or ""),
            actor=str(data.get("actor", "") or ""),
            reason=str(data.get("reason", "") or ""),
            before_exists=bool(data.get("before_exists", False)),
            after_exists=bool(data.get("after_exists", False)),
            before_hash=str(data.get("before_hash", "") or ""),
            after_hash=str(data.get("after_hash", "") or ""),
            before_size=int(data.get("before_size", 0) or 0),
            after_size=int(data.get("after_size", 0) or 0),
            diff_path=str(data.get("diff_path", "") or ""),
            source_event_id=str(data.get("source_event_id", "") or ""),
            stream_id=str(data.get("stream_id", "") or ""),
            version=int(data.get("version", TRACE_VERSION) or TRACE_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LifeTraceStore:
    """Append-only trace store under ``workspace/.life_trace``."""

    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()
        self.trace_root = self.workspace / TRACE_DIR_NAME
        self.index_path = self.trace_root / "index.jsonl"
        self.blob_root = self.trace_root / "blobs"
        self.diff_root = self.trace_root / "diffs"

    def record_change(
        self,
        *,
        path: str,
        before_content: str | None,
        after_content: str | None,
        operation: str,
        tool_name: str,
        actor: str = "life_engine",
        reason: str = "",
        source_event_id: str = "",
        stream_id: str = "",
    ) -> LifeTraceRecord | None:
        """Record one text file change.

        Returns ``None`` when before and after content are identical.
        """

        rel_path = self._normalize_rel_path(path)
        before_text = before_content if before_content is not None else ""
        after_text = after_content if after_content is not None else ""
        before_exists = before_content is not None
        after_exists = after_content is not None
        before_hash = _sha256_text(before_text) if before_exists else ""
        after_hash = _sha256_text(after_text) if after_exists else ""

        if before_exists == after_exists and before_hash == after_hash:
            return None

        self._ensure_dirs()
        if before_exists:
            self._write_blob(before_hash, before_text)
        if after_exists:
            self._write_blob(after_hash, after_text)

        trace_id = f"trace_{uuid4().hex[:16]}"
        diff_text = _make_unified_diff(
            rel_path=rel_path,
            before_text=before_text if before_exists else "",
            after_text=after_text if after_exists else "",
            before_exists=before_exists,
            after_exists=after_exists,
        )
        diff_rel_path = f"diffs/{trace_id}.diff"
        (self.trace_root / diff_rel_path).write_text(diff_text, encoding="utf-8")

        record = LifeTraceRecord(
            trace_id=trace_id,
            timestamp=_now_iso(),
            path=rel_path,
            operation=str(operation or "modify"),
            tool_name=str(tool_name or ""),
            actor=str(actor or "life_engine"),
            reason=str(reason or "").strip(),
            before_exists=before_exists,
            after_exists=after_exists,
            before_hash=before_hash,
            after_hash=after_hash,
            before_size=len(before_text.encode("utf-8")) if before_exists else 0,
            after_size=len(after_text.encode("utf-8")) if after_exists else 0,
            diff_path=diff_rel_path,
            source_event_id=str(source_event_id or ""),
            stream_id=str(stream_id or ""),
        )
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        return record

    def recent(self, *, limit: int = 10, path: str = "") -> list[LifeTraceRecord]:
        records = self._load_records()
        rel_path = self._normalize_rel_path(path) if path else ""
        if rel_path:
            records = [record for record in records if record.path == rel_path]
        return records[-max(0, limit):][::-1]

    def history(self, path: str, *, limit: int = 20) -> list[LifeTraceRecord]:
        return self.recent(limit=limit, path=path)

    def get(self, trace_id: str) -> LifeTraceRecord | None:
        ref = str(trace_id or "").strip()
        if not ref:
            return None
        for record in reversed(self._load_records()):
            if record.trace_id == ref or record.trace_id.startswith(ref):
                return record
        return None

    def read_diff(self, trace_id: str) -> tuple[LifeTraceRecord | None, str]:
        record = self.get(trace_id)
        if record is None:
            return None, ""
        path = (self.trace_root / record.diff_path).resolve()
        try:
            path.relative_to(self.trace_root)
        except ValueError:
            return record, ""
        if not path.exists():
            return record, ""
        return record, path.read_text(encoding="utf-8", errors="replace")

    def read_blob(self, content_hash: str) -> str | None:
        digest = str(content_hash or "").strip()
        if not digest:
            return None
        path = self._blob_path(digest)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def _load_records(self) -> list[LifeTraceRecord]:
        if not self.index_path.exists():
            return []
        records: list[LifeTraceRecord] = []
        for line in self.index_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                record = LifeTraceRecord.from_dict(raw)
                if record.trace_id and record.path:
                    records.append(record)
        return records

    def _ensure_dirs(self) -> None:
        self.trace_root.mkdir(parents=True, exist_ok=True)
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.diff_root.mkdir(parents=True, exist_ok=True)

    def _write_blob(self, digest: str, content: str) -> None:
        path = self._blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(content, encoding="utf-8")

    def _blob_path(self, digest: str) -> Path:
        return self.blob_root / digest[:2] / f"{digest}.txt"

    def _normalize_rel_path(self, path: str) -> str:
        text = str(path or "").strip()
        if not text:
            raise ValueError("path 不能为空")
        candidate = Path(text)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.workspace / candidate).resolve()
        resolved.relative_to(self.workspace)
        rel = resolved.relative_to(self.workspace).as_posix()
        if rel == TRACE_DIR_NAME or rel.startswith(f"{TRACE_DIR_NAME}/"):
            raise ValueError(".life_trace 内部文件不进入追溯")
        return rel


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _make_unified_diff(
    *,
    rel_path: str,
    before_text: str,
    after_text: str,
    before_exists: bool,
    after_exists: bool,
) -> str:
    before_name = f"a/{rel_path}" if before_exists else "/dev/null"
    after_name = f"b/{rel_path}" if after_exists else "/dev/null"
    before_lines = before_text.splitlines(keepends=True)
    after_lines = after_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=before_name,
        tofile=after_name,
        lineterm="",
    )
    text = "\n".join(diff)
    if text and not text.endswith("\n"):
        text += "\n"
    return text
