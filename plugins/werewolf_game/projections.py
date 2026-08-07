"""Stable, role-aware projections for durable Werewolf rooms.

Raw ``GameState`` is deliberately never serialized across the application API.
All hidden information leaves the engine only through one of the projections in
this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .models import GameEvent, GameState, Phase, Role


@dataclass(frozen=True, slots=True)
class ProjectionEvent:
    day: int
    phase: str
    event_type: str
    actor_id: str
    target_id: str
    detail: str
    timestamp: float


_PRIVATE_EVENT_TYPES = frozenset(
    {"wolf_kill", "seer_check", "witch_heal", "witch_poison", "guard_protect"}
)


def public_view(game: GameState, *, room_id: str, revision: int) -> dict[str, Any]:
    """Return the public room projection with no role or night-state leakage."""

    return {
        "projection": "public",
        "room_id": room_id,
        "game_type": "werewolf",
        "rules_version": "werewolf.v2",
        "revision": revision,
        "platform": game.platform,
        "group_id": game.group_id,
        "group_name": game.group_name,
        "owner_actor_id": game.owner_id,
        "board_name": game.board_name,
        "phase": game.phase.value,
        "day_number": game.day_number,
        "players": [_public_player(player) for player in _players(game)],
        "sheriff_actor_id": game.sheriff_id,
        "speaking_order": list(game.speaking_order),
        "speaking_index": game.speaking_index,
        "current_speaker_actor_id": _current_speaker(game),
        "votes": dict(game.votes),
        "sheriff_candidates": list(game.sheriff_candidates),
        "sheriff_votes": dict(game.sheriff_votes),
        "pk_candidates": list(game.pk_candidates),
        "pending_last_words": list(game.pending_last_words),
        "public_log": list(game.public_log),
        "ended_reason": game.ended_reason,
        "created_at": game.created_at,
    }


def player_view(
    game: GameState,
    *,
    room_id: str,
    revision: int,
    actor_id: str,
) -> dict[str, Any]:
    """Return exactly the private knowledge available to ``actor_id``."""

    player = game.players.get(actor_id)
    if player is None:
        raise KeyError(actor_id)
    view = public_view(game, room_id=room_id, revision=revision)
    view["projection"] = "player_private"
    view["private"] = {
        "actor_id": actor_id,
        "role": player.role.value if player.role is not None else None,
        "faction": player.faction.value if player.role is not None else None,
        "alive": player.alive,
        "wolf_teammate_actor_ids": [
            other.user_id
            for other in _players(game)
            if player.role is Role.WEREWOLF
            and other.role is Role.WEREWOLF
            and other.user_id != actor_id
        ],
        "seer_results": [
            _event(event)
            for event in game.event_log
            if event.event_type == "seer_check" and event.actor_id == actor_id
        ],
        "witch_heal_available": (
            game.witch_heal_available if player.role is Role.WITCH else None
        ),
        "witch_poison_available": (
            game.witch_poison_available if player.role is Role.WITCH else None
        ),
        "wolf_target_actor_id": (
            game.night.wolf_target
            if player.role is Role.WITCH and game.phase is Phase.NIGHT
            else None
        ),
        "last_guard_target_actor_id": (
            game.last_guard_target if player.role is Role.GUARD else None
        ),
        "available_actions": available_actions(game, actor_id),
    }
    return view


def moderator_view(game: GameState, *, room_id: str, revision: int) -> dict[str, Any]:
    """Return the explicit referee projection; callers must audit access."""

    view = public_view(game, room_id=room_id, revision=revision)
    view["projection"] = "moderator"
    view["moderator"] = {
        "players": [
            {
                **_public_player(player),
                "role": player.role.value if player.role is not None else None,
                "faction": player.faction.value if player.role is not None else None,
                "death_cause": (
                    player.death_cause.value if player.death_cause is not None else None
                ),
                "is_bot": player.is_bot,
            }
            for player in _players(game)
        ],
        "night": {
            "wolf_target": game.night.wolf_target,
            "wolf_done": game.night.wolf_done,
            "seer_done": sorted(game.night.seer_done),
            "witch_done": sorted(game.night.witch_done),
            "healed_target": game.night.healed_target,
            "poisoned_target": game.night.poisoned_target,
            "guard_done": sorted(game.night.guard_done),
            "guard_target": game.night.guard_target,
        },
        "event_log": [_event(event) for event in game.event_log],
        "last_words_given": dict(game.last_words_given),
        "pending_hunter_shot": game.pending_hunter_shot,
    }
    return view


def replay_view(game: GameState, *, room_id: str, revision: int) -> dict[str, Any]:
    """Return the post-game disclosure projection only for ended rooms."""

    if game.phase is not Phase.ENDED:
        raise ValueError("replay is unavailable before the room ends")
    view = moderator_view(game, room_id=room_id, revision=revision)
    view["projection"] = "replay"
    view["replay"] = view.pop("moderator")
    return view


def available_actions(game: GameState, actor_id: str) -> list[str]:
    """Project protocol actions permitted by current rule state.

    This is a UI affordance only. The engine remains the authority and validates
    every submitted action again.
    """

    player = game.players.get(actor_id)
    actions: list[str] = []
    if game.phase is Phase.WAITING:
        if player is None:
            return ["join"]
        actions.append("leave")
        if actor_id == game.owner_id:
            actions.extend(("start", "end"))
        return actions
    if player is None:
        return actions
    if actor_id == game.owner_id:
        actions.append("end")
    if game.phase is Phase.NIGHT and player.alive:
        if player.role is Role.WEREWOLF:
            actions.extend(("kill", "pass"))
        elif player.role is Role.SEER:
            actions.extend(("check", "pass"))
        elif player.role is Role.WITCH:
            actions.extend(("heal", "poison", "pass"))
        elif player.role is Role.GUARD:
            actions.extend(("guard", "pass"))
    elif game.phase is Phase.SHERIFF_ELECTION and player.alive:
        actions.extend(("campaign", "withdraw", "sheriff_vote"))
    elif game.phase is Phase.SPEAKING and player.alive:
        actions.extend(("speech", "self_destruct"))
        if actor_id == game.owner_id:
            actions.append("next_speaker")
    elif game.phase in {Phase.VOTE, Phase.PK_VOTE} and player.can_vote:
        actions.extend(("vote", "self_destruct"))
    elif game.phase is Phase.LAST_WORDS and actor_id in game.pending_last_words:
        actions.extend(("last_words", "pass"))
    elif game.phase is Phase.HUNTER_SHOT and actor_id == game.pending_hunter_shot:
        actions.append("hunter_shot")
    if actor_id == game.sheriff_id:
        actions.extend(("sheriff_transfer", "sheriff_destroy"))
    return sorted(set(actions))


def event_visibility(event: GameEvent, game: GameState) -> tuple[str, tuple[str, ...]]:
    """Return ledger visibility and allowed actor ids for one engine event."""

    if event.event_type not in _PRIVATE_EVENT_TYPES:
        return "public", ()
    if event.event_type == "wolf_kill":
        actors = tuple(
            player.user_id for player in _players(game) if player.role is Role.WEREWOLF
        )
        return "players", actors
    return "players", (event.actor_id,) if event.actor_id else ()


def _players(game: GameState):
    return sorted(game.players.values(), key=lambda player: player.seat)


def _public_player(player) -> dict[str, Any]:
    return {
        "actor_id": player.user_id,
        "display_name": player.display_name,
        "seat": player.seat,
        "alive": player.alive,
        "is_sheriff": player.is_sheriff,
        "idiot_revealed": player.idiot_revealed,
    }


def _current_speaker(game: GameState) -> str | None:
    if game.phase is not Phase.SPEAKING:
        return None
    if game.speaking_index >= len(game.speaking_order):
        return None
    return game.speaking_order[game.speaking_index]


def _event(event: GameEvent) -> dict[str, Any]:
    return asdict(ProjectionEvent(**asdict(event)))


__all__ = [
    "available_actions",
    "event_visibility",
    "moderator_view",
    "player_view",
    "public_view",
    "replay_view",
]
