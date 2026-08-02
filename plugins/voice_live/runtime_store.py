"""Durable, append-only storage for an independent voice episode."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_component(value: str) -> str:
    clean = "".join(ch for ch in value if ch.isalnum() or ch in "-_")
    if not clean:
        raise ValueError("episode and instance identifiers must contain safe characters")
    return clean


@dataclass(slots=True, frozen=True)
class EpisodeRecord:
    sequence: int
    timestamp: str
    event: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event": self.event,
            "payload": self.payload,
        }


class VoiceEpisodeStore:
    """One instance/episode store with atomic checkpoints and crash recovery."""

    def __init__(self, root: str | Path, instance_id: str, episode_id: str) -> None:
        self.instance_id = _safe_component(instance_id)
        self.episode_id = _safe_component(episode_id)
        self.directory = Path(root) / self.instance_id / "episodes" / self.episode_id
        self.events_path = self.directory / "events.jsonl"
        self.checkpoint_path = self.directory / "checkpoint.json"
        self._lock = threading.Lock()
        self._repair_torn_tail()
        self._sequence = self._recover_sequence()

    def _repair_torn_tail(self) -> None:
        """Remove only an incomplete final JSONL record left by a hard crash."""
        try:
            content = self.events_path.read_bytes()
        except FileNotFoundError:
            return
        if not content or content.endswith(b"\n"):
            return
        boundary = content.rfind(b"\n") + 1
        final_line = content[boundary:]
        try:
            decoded = json.loads(final_line)
            valid = isinstance(decoded, dict)
        except (UnicodeDecodeError, json.JSONDecodeError):
            valid = False
        repaired = content + b"\n" if valid else content[:boundary]
        with self.events_path.open("wb") as handle:
            handle.write(repaired)
            handle.flush()
            os.fsync(handle.fileno())

    def _recover_sequence(self) -> int:
        checkpoint = self.load_checkpoint()
        sequence = int(checkpoint.get("last_sequence") or 0)
        if not self.events_path.exists():
            return sequence
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                    sequence = max(sequence, int(raw.get("sequence") or 0))
                except (json.JSONDecodeError, TypeError, ValueError):
                    # A process crash can leave only the final line incomplete.
                    continue
        return sequence

    def append(self, event: str, payload: dict[str, Any] | None = None) -> EpisodeRecord:
        if not event:
            raise ValueError("event name is required")
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._sequence += 1
            record = EpisodeRecord(self._sequence, _utc_now(), event, dict(payload or {}))
            encoded = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":"))
            with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return record

    async def append_async(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
    ) -> EpisodeRecord:
        """Append durably without blocking the realtime event loop."""
        return await asyncio.to_thread(self.append, event, payload)

    def checkpoint(self, state: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            data = {
                "schema_version": SCHEMA_VERSION,
                "instance_id": self.instance_id,
                "episode_id": self.episode_id,
                "state": state,
                "last_sequence": self._sequence,
                "updated_at": _utc_now(),
                **fields,
            }
            tmp = self.checkpoint_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.checkpoint_path)
            return data

    async def checkpoint_async(self, state: str, **fields: Any) -> dict[str, Any]:
        """Write an atomic checkpoint outside the realtime event loop."""
        return await asyncio.to_thread(self.checkpoint, state, **fields)

    def load_checkpoint(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def read_all(self) -> list[EpisodeRecord]:
        records: list[EpisodeRecord] = []
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return records
        for line in lines:
            try:
                raw = json.loads(line)
                records.append(
                    EpisodeRecord(
                        sequence=int(raw["sequence"]),
                        timestamp=str(raw["timestamp"]),
                        event=str(raw["event"]),
                        payload=dict(raw.get("payload") or {}),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return records

    def transcript(self) -> list[dict[str, Any]]:
        return [record.payload for record in self.read_all() if record.event == "transcript.final"]
