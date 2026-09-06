"""Append-only, hash-chained traces for Minecraft embodiment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .embodiment_contracts import JsonObject, utc_now


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """One immutable record in an embodiment trace."""

    sequence: int
    recorded_at: str
    kind: str
    payload: Mapping[str, Any]
    previous_hash: str
    record_hash: str

    def to_wire(self) -> JsonObject:
        """Serialize a record for JSONL storage."""

        return {
            "sequence": self.sequence,
            "recorded_at": self.recorded_at,
            "kind": self.kind,
            "payload": dict(self.payload),
            "previous_hash": self.previous_hash,
            "record_hash": self.record_hash,
        }


class TraceIntegrityError(RuntimeError):
    """Raised when a persisted trace does not match its hash chain."""


class EmbodimentTrace:
    """Durable JSONL trace with ordering and tamper-evident hash links."""

    def __init__(self, path: Path) -> None:
        """Create an unopened trace bound to ``path``."""

        self._path = path
        self._lock = asyncio.Lock()
        self._opened = False
        self._sequence = 0
        self._previous_hash = ""

    @property
    def path(self) -> Path:
        """Return the trace path."""

        return self._path

    async def open(self) -> None:
        """Create storage and recover the validated tail state."""

        async with self._lock:
            if self._opened:
                return
            records = await asyncio.to_thread(self._read_and_validate)
            if records:
                tail = records[-1]
                self._sequence = tail.sequence
                self._previous_hash = tail.record_hash
            self._opened = True

    async def append(self, kind: str, payload: Mapping[str, Any]) -> TraceRecord:
        """Append and fsync one complete record."""

        if not kind.strip():
            raise ValueError("kind must not be empty")
        async with self._lock:
            if not self._opened:
                raise RuntimeError("trace is not open")
            sequence = self._sequence + 1
            recorded_at = utc_now()
            owned_payload = dict(payload)
            digest_input = {
                "sequence": sequence,
                "recorded_at": recorded_at,
                "kind": kind,
                "payload": owned_payload,
                "previous_hash": self._previous_hash,
            }
            record_hash = self._digest(digest_input)
            record = TraceRecord(
                sequence=sequence,
                recorded_at=recorded_at,
                kind=kind,
                payload=owned_payload,
                previous_hash=self._previous_hash,
                record_hash=record_hash,
            )
            await asyncio.to_thread(self._append_sync, record)
            self._sequence = sequence
            self._previous_hash = record_hash
            return record

    async def verify(self) -> tuple[TraceRecord, ...]:
        """Read and validate the complete persisted chain."""

        async with self._lock:
            return tuple(await asyncio.to_thread(self._read_and_validate))

    @staticmethod
    def _digest(payload: Mapping[str, Any]) -> str:
        """Hash a canonical JSON representation."""

        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _append_sync(self, record: TraceRecord) -> None:
        """Perform one durable append outside the event loop."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_wire(), ensure_ascii=False, sort_keys=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _read_and_validate(self) -> list[TraceRecord]:
        """Load all records and validate sequence, link, and content hashes."""

        if not self._path.exists():
            return []
        records: list[TraceRecord] = []
        previous_hash = ""
        with self._path.open("r", encoding="utf-8") as handle:
            for expected_sequence, line in enumerate(handle, start=1):
                raw = json.loads(line)
                digest_input = {
                    "sequence": int(raw["sequence"]),
                    "recorded_at": str(raw["recorded_at"]),
                    "kind": str(raw["kind"]),
                    "payload": dict(raw["payload"]),
                    "previous_hash": str(raw["previous_hash"]),
                }
                if digest_input["sequence"] != expected_sequence:
                    raise TraceIntegrityError(
                        f"trace sequence mismatch at {expected_sequence}"
                    )
                if digest_input["previous_hash"] != previous_hash:
                    raise TraceIntegrityError(
                        f"trace link mismatch at {expected_sequence}"
                    )
                calculated = self._digest(digest_input)
                if calculated != str(raw["record_hash"]):
                    raise TraceIntegrityError(
                        f"trace hash mismatch at {expected_sequence}"
                    )
                record = TraceRecord(
                    **digest_input,
                    record_hash=calculated,
                )
                records.append(record)
                previous_hash = calculated
        return records
