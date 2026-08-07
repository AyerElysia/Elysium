"""Append-only SQLite ledger and recoverable snapshots for P3-10 rooms."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    DeathCause,
    GameEvent,
    GameState,
    NightState,
    Phase,
    Player,
    Role,
    WinRule,
)


class ActionConflict(RuntimeError):
    """One action id was reused with different protocol content."""


class RevisionConflict(RuntimeError):
    """The caller acted on a stale room revision."""


class RoomNotFound(KeyError):
    """The durable room does not exist."""


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    event_id: str
    room_id: str
    sequence: int
    event_type: str
    actor_id: str
    visibility: str
    visible_to: tuple[str, ...]
    payload: dict[str, Any]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class StoredAction:
    action_id: str
    room_id: str
    request_hash: str
    result: dict[str, Any]
    revision: int
    event_sequence: int


class WerewolfLedger:
    """Own the authoritative event stream and revisioned recovery snapshot."""

    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path) if database_path != ":memory:" else None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS werewolf_rooms (
                    room_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS werewolf_events (
                    room_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    visibility TEXT NOT NULL,
                    visible_to_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY (room_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_werewolf_events_room
                ON werewolf_events(room_id, sequence);
                CREATE TABLE IF NOT EXISTS werewolf_actions (
                    action_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def request_hash(
        *, room_id: str, actor_id: str, action_type: str, payload: dict[str, Any]
    ) -> str:
        raw = json.dumps(
            {
                "room_id": room_id,
                "actor_id": actor_id,
                "action_type": action_type,
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def create_room(
        self,
        *,
        room_id: str,
        game: GameState,
        actor_id: str,
        action_id: str,
        request_hash: str,
        result: dict[str, Any],
    ) -> StoredAction:
        now = _now()
        with self._lock, self._connection:
            existing = self._action_row(action_id)
            if existing is not None:
                return self._replay_or_conflict(existing, request_hash)
            if self._room_row(room_id) is not None:
                raise ActionConflict("room id already exists")
            self._connection.execute(
                """
                INSERT INTO werewolf_rooms (
                    room_id, schema_version, revision, state_json, created_at, updated_at
                ) VALUES (?, 1, 1, ?, ?, ?)
                """,
                (room_id, _state_json(game), now, now),
            )
            sequence = self._append_event(
                room_id=room_id,
                sequence=1,
                event_type="tabletop.werewolf.room_created",
                actor_id=actor_id,
                visibility="public",
                visible_to=(),
                payload={"room_id": room_id, "board_name": game.board_name},
                occurred_at=now,
            )
            self._append_event(
                room_id=room_id,
                sequence=2,
                event_type="tabletop.werewolf.snapshot_committed",
                actor_id=actor_id,
                visibility="moderator",
                visible_to=(),
                payload={"revision": 1, "state": _state_payload(game)},
                occurred_at=now,
            )
            stored_result = {**result, "revision": 1}
            self._insert_action(
                action_id=action_id,
                room_id=room_id,
                request_hash=request_hash,
                result=stored_result,
                revision=1,
                event_sequence=sequence,
                now=now,
            )
            return StoredAction(
                action_id, room_id, request_hash, stored_result, 1, sequence
            )

    def commit_action(
        self,
        *,
        room_id: str,
        game: GameState,
        actor_id: str,
        action_id: str,
        request_hash: str,
        action_type: str,
        result: dict[str, Any],
        expected_revision: int | None,
        events: list[dict[str, Any]],
        action_visibility: str = "public",
        action_visible_to: tuple[str, ...] = (),
    ) -> StoredAction:
        now = _now()
        with self._lock, self._connection:
            existing = self._action_row(action_id)
            if existing is not None:
                return self._replay_or_conflict(existing, request_hash)
            row = self._room_row(room_id)
            if row is None:
                raise RoomNotFound(room_id)
            current = int(row["revision"])
            if expected_revision is not None and expected_revision != current:
                raise RevisionConflict(
                    f"expected room revision {expected_revision}, current is {current}"
                )
            revision = current + 1
            next_sequence = self._next_sequence(room_id)
            action_sequence = self._append_event(
                room_id=room_id,
                sequence=next_sequence,
                event_type=f"tabletop.werewolf.{action_type}",
                actor_id=actor_id,
                visibility=action_visibility,
                visible_to=action_visible_to,
                payload={"action_id": action_id, "result": result},
                occurred_at=now,
            )
            sequence = action_sequence
            for event in events:
                sequence += 1
                self._append_event(
                    room_id=room_id,
                    sequence=sequence,
                    event_type=f"tabletop.werewolf.engine.{event['event_type']}",
                    actor_id=str(event.get("actor_id") or ""),
                    visibility=str(event["visibility"]),
                    visible_to=tuple(event.get("visible_to") or ()),
                    payload=dict(event["payload"]),
                    occurred_at=now,
                )
            sequence += 1
            self._append_event(
                room_id=room_id,
                sequence=sequence,
                event_type="tabletop.werewolf.snapshot_committed",
                actor_id=actor_id,
                visibility="moderator",
                visible_to=(),
                payload={"revision": revision, "state": _state_payload(game)},
                occurred_at=now,
            )
            self._connection.execute(
                """
                UPDATE werewolf_rooms
                SET revision = ?, state_json = ?, updated_at = ?
                WHERE room_id = ? AND revision = ?
                """,
                (revision, _state_json(game), now, room_id, current),
            )
            stored_result = {**result, "revision": revision}
            self._insert_action(
                action_id=action_id,
                room_id=room_id,
                request_hash=request_hash,
                result=stored_result,
                revision=revision,
                event_sequence=action_sequence,
                now=now,
            )
            return StoredAction(
                action_id,
                room_id,
                request_hash,
                stored_result,
                revision,
                action_sequence,
            )

    def load_room(self, room_id: str) -> tuple[GameState, int]:
        with self._lock:
            row = self._room_row(room_id)
            if row is None:
                raise RoomNotFound(room_id)
            return _state_from_json(str(row["state_json"])), int(row["revision"])

    def list_rooms(self) -> list[tuple[str, GameState, int]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT room_id, revision, state_json FROM werewolf_rooms ORDER BY updated_at DESC"
            ).fetchall()
            return [
                (str(row["room_id"]), _state_from_json(str(row["state_json"])), int(row["revision"]))
                for row in rows
            ]

    def events(
        self,
        room_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> list[LedgerEvent]:
        with self._lock:
            if self._room_row(room_id) is None:
                raise RoomNotFound(room_id)
            rows = self._connection.execute(
                """
                SELECT * FROM werewolf_events
                WHERE room_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (room_id, after_sequence, limit),
            ).fetchall()
            return [_ledger_event(row) for row in rows]

    def recover_room(self, room_id: str) -> tuple[GameState, int]:
        """Rebuild the mutable room projection from the latest ledger snapshot."""

        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT payload_json FROM werewolf_events
                WHERE room_id = ? AND event_type = 'tabletop.werewolf.snapshot_committed'
                ORDER BY sequence DESC LIMIT 1
                """,
                (room_id,),
            ).fetchone()
            if row is None:
                raise RoomNotFound(room_id)
            payload = json.loads(str(row["payload_json"]))
            revision = int(payload["revision"])
            game = _state_from_payload(dict(payload["state"]))
            now = _now()
            updated = self._connection.execute(
                """
                UPDATE werewolf_rooms
                SET revision = ?, state_json = ?, updated_at = ?
                WHERE room_id = ?
                """,
                (revision, _state_json(game), now, room_id),
            )
            if updated.rowcount != 1:
                raise RoomNotFound(room_id)
            return game, revision

    def integrity(self, room_id: str) -> dict[str, Any]:
        game, revision = self.load_room(room_id)
        events = self.events(room_id, limit=100000)
        sequences = [event.sequence for event in events]
        contiguous = sequences == list(range(1, len(sequences) + 1))
        return {
            "room_id": room_id,
            "revision": revision,
            "phase": game.phase.value,
            "event_count": len(events),
            "last_sequence": sequences[-1] if sequences else 0,
            "contiguous": contiguous,
            "snapshot_valid": True,
        }

    def _room_row(self, room_id: str):
        return self._connection.execute(
            "SELECT * FROM werewolf_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()

    def _action_row(self, action_id: str):
        return self._connection.execute(
            "SELECT * FROM werewolf_actions WHERE action_id = ?", (action_id,)
        ).fetchone()

    def _next_sequence(self, room_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS value FROM werewolf_events WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        return int(row["value"]) + 1

    def _append_event(
        self,
        *,
        room_id: str,
        sequence: int,
        event_type: str,
        actor_id: str,
        visibility: str,
        visible_to: tuple[str, ...],
        payload: dict[str, Any],
        occurred_at: str,
    ) -> int:
        event_id = f"ww_{room_id}_{sequence}"
        self._connection.execute(
            """
            INSERT INTO werewolf_events (
                room_id, sequence, event_id, event_type, actor_id, visibility,
                visible_to_json, payload_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                room_id,
                sequence,
                event_id,
                event_type,
                actor_id,
                visibility,
                json.dumps(visible_to, ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                occurred_at,
            ),
        )
        return sequence

    def _insert_action(
        self,
        *,
        action_id: str,
        room_id: str,
        request_hash: str,
        result: dict[str, Any],
        revision: int,
        event_sequence: int,
        now: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO werewolf_actions (
                action_id, room_id, request_hash, result_json, revision,
                event_sequence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                room_id,
                request_hash,
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                revision,
                event_sequence,
                now,
            ),
        )

    @staticmethod
    def _replay_or_conflict(row, request_hash: str) -> StoredAction:
        if str(row["request_hash"]) != request_hash:
            raise ActionConflict("action id was already used with different content")
        return StoredAction(
            action_id=str(row["action_id"]),
            room_id=str(row["room_id"]),
            request_hash=str(row["request_hash"]),
            result=json.loads(str(row["result_json"])),
            revision=int(row["revision"]),
            event_sequence=int(row["event_sequence"]),
        )


def _state_json(game: GameState) -> str:
    return json.dumps(
        _state_payload(game),
        ensure_ascii=False,
        sort_keys=False,
    )


def _state_payload(game: GameState) -> dict[str, Any]:
    return json.loads(
        json.dumps(asdict(game), ensure_ascii=False, default=_json_default)
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"unsupported snapshot value: {type(value).__name__}")


def _state_from_json(raw: str) -> GameState:
    return _state_from_payload(json.loads(raw))


def _state_from_payload(data: dict[str, Any]) -> GameState:
    data = dict(data)
    data["phase"] = Phase(data["phase"])
    data["win_rule"] = WinRule(data["win_rule"])
    data["players"] = {
        actor_id: Player(
            **{
                **payload,
                "role": Role(payload["role"]) if payload.get("role") else None,
                "death_cause": (
                    DeathCause(payload["death_cause"])
                    if payload.get("death_cause")
                    else None
                ),
            }
        )
        for actor_id, payload in data["players"].items()
    }
    data["night"] = NightState(
        wolf_target=data["night"].get("wolf_target"),
        wolf_done=bool(data["night"].get("wolf_done")),
        seer_done=set(data["night"].get("seer_done") or ()),
        witch_done=set(data["night"].get("witch_done") or ()),
        healed_target=data["night"].get("healed_target"),
        poisoned_target=data["night"].get("poisoned_target"),
        guard_done=set(data["night"].get("guard_done") or ()),
        guard_target=data["night"].get("guard_target"),
    )
    data["event_log"] = [GameEvent(**event) for event in data.get("event_log", ())]
    allowed = {item.name for item in fields(GameState)}
    return GameState(**{key: value for key, value in data.items() if key in allowed})


def _ledger_event(row) -> LedgerEvent:
    occurred_at = datetime.fromisoformat(str(row["occurred_at"]))
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    return LedgerEvent(
        event_id=str(row["event_id"]),
        room_id=str(row["room_id"]),
        sequence=int(row["sequence"]),
        event_type=str(row["event_type"]),
        actor_id=str(row["actor_id"]),
        visibility=str(row["visibility"]),
        visible_to=tuple(json.loads(str(row["visible_to_json"]))),
        payload=json.loads(str(row["payload_json"])),
        occurred_at=occurred_at,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ActionConflict",
    "LedgerEvent",
    "RevisionConflict",
    "RoomNotFound",
    "StoredAction",
    "WerewolfLedger",
]
