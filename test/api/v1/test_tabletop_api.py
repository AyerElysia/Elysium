"""P3-10 durable Werewolf domain and API contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plugins.werewolf_game.domain import ActionConflict, WerewolfDomainService
from plugins.werewolf_game.ledger import WerewolfLedger
from plugins.werewolf_game.models import Phase, Role
from plugins.werewolf_game.projections import moderator_view, player_view, public_view
from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.policy import USER_FRONTEND_AUDIENCE
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.tokens import SignedValueCodec
from src.kernel.commands import CommandDispatcher, CommandStore

SECRET = "t" * 48
ORIGIN = "http://localhost:5173"


@pytest.mark.asyncio
async def test_projections_do_not_leak_hidden_state_by_role(tmp_path: Path) -> None:
    ledger = WerewolfLedger(tmp_path / "tabletop.sqlite3")
    service = WerewolfDomainService(ledger)
    try:
        created = await service.create_room(
            actor_id="wolf",
            display_name="Wolf",
            platform="app",
            group_id="g1",
            group_name="Room",
            group_stream_id="s1",
            board_name="6人新手局",
            action_id="create-projection-room",
            room_id="room_projection",
        )
        room_id = created["view"]["room_id"]
        for index, actor in enumerate(("seer", "witch", "v1", "v2", "wolf2"), 1):
            await service.apply_action(
                room_id=room_id,
                actor_id=actor,
                action_id=f"join-player-{index}",
                action_type="join",
                payload={"display_name": actor},
            )
        game, revision = ledger.load_room(room_id)
        assigned = [Role.WEREWOLF, Role.SEER, Role.WITCH, Role.VILLAGER, Role.VILLAGER, Role.WEREWOLF]
        for player, role in zip(game.players.values(), assigned, strict=True):
            player.role = role
        game.phase = Phase.NIGHT
        game.day_number = 1

        public = public_view(game, room_id=room_id, revision=revision)
        encoded_public = str(public)
        assert "role" not in public["players"][0]
        assert "wolf_target" not in encoded_public
        assert "seer_done" not in encoded_public
        assert "event_log" not in encoded_public

        wolf = player_view(game, room_id=room_id, revision=revision, actor_id="wolf")
        assert wolf["private"]["role"] == "werewolf"
        assert wolf["private"]["wolf_teammate_actor_ids"] == ["wolf2"]
        assert wolf["private"]["witch_heal_available"] is None

        seer = player_view(game, room_id=room_id, revision=revision, actor_id="seer")
        assert seer["private"]["wolf_teammate_actor_ids"] == []
        assert seer["private"]["wolf_target_actor_id"] is None

        moderator = moderator_view(game, room_id=room_id, revision=revision)
        assert moderator["moderator"]["players"][0]["role"] == "werewolf"
        assert "night" in moderator["moderator"]
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_action_id_is_idempotent_conflicting_content_is_rejected_and_restart_recovers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tabletop.sqlite3"
    first = WerewolfLedger(path)
    service = WerewolfDomainService(first)
    created = await service.create_room(
        actor_id="owner",
        display_name="Owner",
        platform="app",
        group_id="g2",
        group_name="Room",
        group_stream_id="s2",
        board_name="6人新手局",
        action_id="create-recovery-room",
        room_id="room_recovery",
    )
    revision = created["view"]["revision"]
    joined = await service.apply_action(
        room_id="room_recovery",
        actor_id="player2",
        action_id="same-join-action",
        action_type="join",
        payload={"display_name": "P2"},
        expected_revision=revision,
    )
    replay = await service.apply_action(
        room_id="room_recovery",
        actor_id="player2",
        action_id="same-join-action",
        action_type="join",
        payload={"display_name": "P2"},
        expected_revision=revision,
    )
    assert replay["result"] == joined["result"]
    assert len(replay["view"]["players"]) == 2
    with pytest.raises(ActionConflict):
        await service.apply_action(
            room_id="room_recovery",
            actor_id="player2",
            action_id="same-join-action",
            action_type="join",
            payload={"display_name": "different"},
        )
    first.close()

    recovered = WerewolfLedger(path)
    try:
        game, recovered_revision = recovered.load_room("room_recovery")
        assert recovered_revision == joined["view"]["revision"]
        assert list(game.players) == ["owner", "player2"]
        integrity = recovered.integrity("room_recovery")
        assert integrity["contiguous"] is True
        assert integrity["phase"] == "waiting"

        with recovered._lock, recovered._connection:
            recovered._connection.execute(
                "UPDATE werewolf_rooms SET state_json = ? WHERE room_id = ?",
                ("{}", "room_recovery"),
            )
        with pytest.raises((KeyError, TypeError)):
            recovered.load_room("room_recovery")
        rebuilt, rebuilt_revision = recovered.recover_room("room_recovery")
        assert rebuilt_revision == joined["view"]["revision"]
        assert list(rebuilt.players) == ["owner", "player2"]
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_private_action_is_filtered_from_other_players(tmp_path: Path) -> None:
    ledger = WerewolfLedger(tmp_path / "tabletop.sqlite3")
    service = WerewolfDomainService(ledger)
    try:
        await service.create_room(
            actor_id="wolf",
            display_name="Wolf",
            platform="app",
            group_id="g3",
            group_name="Room",
            group_stream_id="s3",
            board_name="测试局",
            action_id="create-private-events",
            room_id="room_private",
        )
        for actor in ("seer", "villager"):
            await service.apply_action(
                room_id="room_private",
                actor_id=actor,
                action_id=f"join-private-{actor}",
                action_type="join",
                payload={"display_name": actor},
            )
        game, revision = ledger.load_room("room_private")
        game.players["wolf"].role = Role.WEREWOLF
        game.players["seer"].role = Role.SEER
        game.players["villager"].role = Role.VILLAGER
        game.phase = Phase.NIGHT
        game.day_number = 1
        ledger.commit_action(
            room_id="room_private",
            game=game,
            actor_id="wolf",
            action_id="prepare-private-room",
            request_hash=ledger.request_hash(room_id="room_private", actor_id="wolf", action_type="prepare", payload={}),
            action_type="prepare",
            result={"ok": True},
            expected_revision=revision,
            events=[],
        )
        await service.apply_action(
            room_id="room_private",
            actor_id="wolf",
            action_id="wolf-kill-once",
            action_type="kill",
            payload={"target_actor_id": "villager"},
        )
        wolf_events = await service.events("room_private", actor_id="wolf")
        seer_events = await service.events("room_private", actor_id="seer")
        assert any(event.event_type.endswith(".kill") for event in wolf_events)
        assert not any(event.event_type.endswith(".kill") for event in seer_events)
        assert not any(event.event_type.endswith(".engine.wolf_kill") for event in seer_events)
    finally:
        ledger.close()


def test_http_room_flow_scope_and_idempotency(tmp_path: Path) -> None:
    auth = AuthStore(tmp_path / "api.sqlite3", installation_id="test")
    codec = SignedValueCodec(SECRET)
    command_store = CommandStore(tmp_path / "api.sqlite3")
    ledger = WerewolfLedger(tmp_path / "tabletop.sqlite3")
    context = APIContext(
        store=auth,
        codec=codec,
        installation_id="test",
        allowed_origins=(ORIGIN,),
        command_store=command_store,
        command_dispatcher=CommandDispatcher(command_store),
        tabletop=WerewolfDomainService(ledger),
    )
    client = TestClient(create_api_app(context))
    challenge = auth.create_bootstrap_challenge(
        codec=codec,
        audience=USER_FRONTEND_AUDIENCE,
        origin=ORIGIN,
        scopes=("auth:session", "tabletop:read", "tabletop:play"),
    )
    token = client.post(
        "/auth/sessions",
        headers={"Origin": ORIGIN},
        json={
            "grant_type": "bootstrap_challenge",
            "audience": USER_FRONTEND_AUDIENCE,
            "bootstrap_challenge": challenge,
            "origin": ORIGIN,
        },
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}

    missing = client.post(
        "/tabletop/rooms",
        headers=headers,
        json={"display_name": "Owner", "board_name": "6人新手局"},
    )
    assert missing.status_code == 422
    created = client.post(
        "/tabletop/rooms",
        headers={**headers, "Idempotency-Key": "create-http-room"},
        json={"display_name": "Owner", "board_name": "6人新手局"},
    )
    assert created.status_code == 201
    room_id = created.json()["room"]["room_id"]
    assert "private" in created.json()["room"]

    repeated = client.post(
        "/tabletop/rooms",
        headers={**headers, "Idempotency-Key": "create-http-room"},
        json={"display_name": "Owner", "board_name": "6人新手局"},
    )
    assert repeated.status_code == 201
    assert repeated.json()["room"]["room_id"] == room_id

    room = client.get(f"/tabletop/rooms/{room_id}", headers=headers)
    assert room.status_code == 200
    assert room.json()["room"]["private"]["actor_id"] == "local_user"

    command_store.close()
    ledger.close()
    auth.close()
