"""Single durable owner for Werewolf rule execution across chat and HTTP."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import asdict
from typing import Any

from .engine import ActionResult, WerewolfEngine
from .ledger import (
    ActionConflict,
    LedgerEvent,
    RevisionConflict,
    RoomNotFound,
    StoredAction,
    WerewolfLedger,
)
from .models import GameState, Phase
from .projections import (
    event_visibility,
    moderator_view,
    player_view,
    public_view,
    replay_view,
)


class DomainActionRejected(RuntimeError):
    """The deterministic engine rejected a game action."""


class DomainAuthorizationError(PermissionError):
    """The actor is not allowed to perform or view the operation."""


class WerewolfDomainService:
    """Serialize durable room actions and project role-aware views."""

    def __init__(self, ledger: WerewolfLedger, engine: WerewolfEngine | None = None) -> None:
        self.ledger = ledger
        self.engine = engine or WerewolfEngine()
        self._lock = asyncio.Lock()

    async def create_room(
        self,
        *,
        actor_id: str,
        display_name: str,
        platform: str,
        group_id: str,
        group_name: str,
        group_stream_id: str,
        board_name: str,
        action_id: str,
        room_id: str | None = None,
    ) -> dict[str, Any]:
        room_id = room_id or f"room_{secrets.token_urlsafe(12)}"
        payload = {
            "display_name": display_name,
            "platform": platform,
            "group_id": group_id,
            "group_name": group_name,
            "group_stream_id": group_stream_id,
            "board_name": board_name,
        }
        request_hash = self.ledger.request_hash(
            room_id=room_id,
            actor_id=actor_id,
            action_type="room_create",
            payload=payload,
        )
        async with self._lock:
            game = self.engine.create_game(
                platform=platform,
                group_id=group_id,
                group_name=group_name,
                group_stream_id=group_stream_id,
                owner_id=actor_id,
                board_name=board_name,
            )
            joined = self.engine.add_player(
                game, user_id=actor_id, display_name=display_name
            )
            result = {
                "ok": True,
                "message": joined.message,
                "room_id": room_id,
                "action_id": action_id,
            }
            stored = await asyncio.to_thread(
                self.ledger.create_room,
                room_id=room_id,
                game=game,
                actor_id=actor_id,
                action_id=action_id,
                request_hash=request_hash,
                result=result,
            )
            loaded, _ = await asyncio.to_thread(self.ledger.load_room, room_id)
            return {
                "result": stored.result,
                "view": self.authorized_view(
                    loaded,
                    room_id=room_id,
                    revision=stored.revision,
                    actor_id=actor_id,
                ),
            }

    async def apply_action(
        self,
        *,
        room_id: str,
        actor_id: str,
        action_id: str,
        action_type: str,
        payload: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        payload = dict(payload or {})
        request_hash = self.ledger.request_hash(
            room_id=room_id,
            actor_id=actor_id,
            action_type=action_type,
            payload=payload,
        )
        async with self._lock:
            replay = await asyncio.to_thread(
                self._existing_action, action_id, request_hash
            )
            if replay is not None:
                game, _ = await asyncio.to_thread(self.ledger.load_room, room_id)
                return {
                    "result": replay.result,
                    "view": self.authorized_view(
                        game, room_id=room_id, revision=replay.revision, actor_id=actor_id
                    ),
                }
            game, _ = await asyncio.to_thread(self.ledger.load_room, room_id)
            before = len(game.event_log)
            engine_result = self._execute(game, actor_id, action_type, payload)
            if not engine_result.ok:
                raise DomainActionRejected(engine_result.message)
            events = []
            for event in game.event_log[before:]:
                visibility, visible_to = event_visibility(event, game)
                events.append(
                    {
                        "event_type": event.event_type,
                        "actor_id": event.actor_id,
                        "visibility": visibility,
                        "visible_to": visible_to,
                        "payload": asdict(event),
                    }
                )
            result = {
                "ok": True,
                "message": engine_result.message,
                "public_messages": list(engine_result.public_messages or ()),
                "game_ended": engine_result.game_ended,
                "room_id": room_id,
                "action_id": action_id,
                "action_type": action_type,
            }
            action_visibility = (
                "players" if action_type in _PRIVATE_ACTIONS else "public"
            )
            stored = await asyncio.to_thread(
                self.ledger.commit_action,
                room_id=room_id,
                game=game,
                actor_id=actor_id,
                action_id=action_id,
                request_hash=request_hash,
                action_type=action_type,
                result=result,
                expected_revision=expected_revision,
                events=events,
                action_visibility=action_visibility,
                action_visible_to=((actor_id,) if action_visibility == "players" else ()),
            )
            return {
                "result": stored.result,
                "view": self.authorized_view(
                    game, room_id=room_id, revision=stored.revision, actor_id=actor_id
                ),
            }

    async def room_view(
        self, room_id: str, *, actor_id: str, grants: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        game, revision = await asyncio.to_thread(self.ledger.load_room, room_id)
        if not self.can_read(game, room_id, actor_id, grants):
            raise DomainAuthorizationError("room is not granted to this actor")
        return self.authorized_view(
            game, room_id=room_id, revision=revision, actor_id=actor_id
        )

    async def private_view(self, room_id: str, *, actor_id: str) -> dict[str, Any]:
        game, revision = await asyncio.to_thread(self.ledger.load_room, room_id)
        if actor_id not in game.players:
            raise DomainAuthorizationError("actor is not a room player")
        return player_view(game, room_id=room_id, revision=revision, actor_id=actor_id)

    async def replay(self, room_id: str, *, actor_id: str, grants: tuple[str, ...] = ()) -> dict[str, Any]:
        game, revision = await asyncio.to_thread(self.ledger.load_room, room_id)
        if not self.can_read(game, room_id, actor_id, grants):
            raise DomainAuthorizationError("room is not granted to this actor")
        return replay_view(game, room_id=room_id, revision=revision)

    async def moderator_view(self, room_id: str) -> dict[str, Any]:
        game, revision = await asyncio.to_thread(self.ledger.load_room, room_id)
        return moderator_view(game, room_id=room_id, revision=revision)

    async def recover_room(self, room_id: str) -> dict[str, Any]:
        game, revision = await asyncio.to_thread(self.ledger.recover_room, room_id)
        return moderator_view(game, room_id=room_id, revision=revision)

    async def events(
        self,
        room_id: str,
        *,
        actor_id: str,
        grants: tuple[str, ...] = (),
        after_sequence: int = 0,
        limit: int = 200,
        moderator: bool = False,
    ) -> list[LedgerEvent]:
        game, _ = await asyncio.to_thread(self.ledger.load_room, room_id)
        if not moderator and not self.can_read(game, room_id, actor_id, grants):
            raise DomainAuthorizationError("room is not granted to this actor")
        events = await asyncio.to_thread(
            self.ledger.events,
            room_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        if moderator:
            return events
        return [event for event in events if self._event_visible(event, actor_id)]

    async def find_room(self, platform: str, group_id: str) -> str | None:
        rooms = await asyncio.to_thread(self.ledger.list_rooms)
        for room_id, game, _ in rooms:
            if game.platform == platform and game.group_id == group_id and game.phase is not Phase.ENDED:
                return room_id
        return None

    async def find_room_for_player(self, actor_id: str) -> str | None:
        rooms = await asyncio.to_thread(self.ledger.list_rooms)
        for room_id, game, _ in rooms:
            if actor_id in game.players and game.phase is not Phase.ENDED:
                return room_id
        return None

    @staticmethod
    def can_read(
        game: GameState, room_id: str, actor_id: str, grants: tuple[str, ...]
    ) -> bool:
        values = set(grants)
        return (
            actor_id in game.players
            or actor_id == game.owner_id
            or "*" in values
            or "tabletop:*" in values
            or f"tabletop:{room_id}" in values
        )

    @staticmethod
    def authorized_view(
        game: GameState, *, room_id: str, revision: int, actor_id: str
    ) -> dict[str, Any]:
        if actor_id in game.players:
            return player_view(
                game, room_id=room_id, revision=revision, actor_id=actor_id
            )
        return public_view(game, room_id=room_id, revision=revision)

    def _execute(
        self,
        game: GameState,
        actor_id: str,
        action_type: str,
        payload: dict[str, Any],
    ) -> ActionResult:
        target = payload.get("target_actor_id")
        if action_type == "join":
            return self.engine.add_player(
                game,
                user_id=actor_id,
                display_name=str(payload.get("display_name") or actor_id),
            )
        if action_type == "leave":
            return self.engine.remove_player(game, actor_id)
        if action_type == "start":
            self._require_owner(game, actor_id)
            return self.engine.start_game(game)
        if action_type == "end":
            self._require_owner(game, actor_id)
            if game.phase is Phase.ENDED:
                return ActionResult(False, "本局已经结束。")
            game.phase = Phase.ENDED
            game.ended_reason = str(payload.get("reason") or "房主结束了本局。")
            game.log_event("game_end", actor_id=actor_id, detail=game.ended_reason)
            return ActionResult(True, "本局狼人杀已结束。", [game.ended_reason], True)
        if action_type in _PRIVATE_ACTIONS:
            return self.engine.night_action(
                game, actor_id=actor_id, action=action_type, target_id=target
            )
        if action_type == "vote":
            return self.engine.vote(game, voter_id=actor_id, target_id=target)
        if action_type == "speech":
            return self.engine.record_speech(
                game, user_id=actor_id, text=str(payload.get("text") or "")
            )
        if action_type == "next_speaker":
            return self.engine.advance_speaker(game)
        if action_type == "self_destruct":
            return self.engine.wolf_self_destruct(
                game,
                user_id=actor_id,
                last_words=str(payload.get("text") or ""),
            )
        if action_type == "campaign":
            return self.engine.sheriff_register(game, user_id=actor_id)
        if action_type == "withdraw":
            return self.engine.sheriff_withdraw(game, user_id=actor_id)
        if action_type == "sheriff_vote":
            return self.engine.sheriff_vote(
                game, voter_id=actor_id, target_id=target
            )
        if action_type == "sheriff_transfer":
            return self.engine.sheriff_transfer(
                game, sheriff_id=actor_id, target_id=str(target or "")
            )
        if action_type == "sheriff_destroy":
            return self.engine.sheriff_destroy(game, sheriff_id=actor_id)
        if action_type == "last_words":
            return self.engine.submit_last_words(
                game, user_id=actor_id, text=str(payload.get("text") or "")
            )
        if action_type == "pass":
            if game.phase is Phase.LAST_WORDS:
                return self.engine.skip_last_words(game, user_id=actor_id)
            return self.engine.night_action(
                game, actor_id=actor_id, action="pass", target_id=None
            )
        if action_type == "hunter_shot":
            return self.engine.hunter_shot(
                game, hunter_id=actor_id, target_id=target
            )
        raise DomainActionRejected("未知狼人杀动作。")

    @staticmethod
    def _require_owner(game: GameState, actor_id: str) -> None:
        if actor_id != game.owner_id:
            raise DomainAuthorizationError("only the room owner may perform this action")

    def _existing_action(
        self, action_id: str, request_hash: str
    ) -> StoredAction | None:
        row = self.ledger._action_row(action_id)
        if row is None:
            return None
        return self.ledger._replay_or_conflict(row, request_hash)

    @staticmethod
    def _event_visible(event: LedgerEvent, actor_id: str) -> bool:
        return event.visibility == "public" or (
            event.visibility == "players" and actor_id in event.visible_to
        )


_PRIVATE_ACTIONS = frozenset({"kill", "check", "heal", "poison", "guard", "pass"})


__all__ = [
    "ActionConflict",
    "DomainActionRejected",
    "DomainAuthorizationError",
    "RevisionConflict",
    "RoomNotFound",
    "WerewolfDomainService",
]
