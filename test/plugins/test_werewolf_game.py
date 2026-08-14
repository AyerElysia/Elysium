from __future__ import annotations

import random
from pathlib import Path

import pytest

from plugins.werewolf_game.config import WerewolfConfig
from plugins.werewolf_game.engine import WerewolfEngine
from plugins.werewolf_game.event_handler import WerewolfCommandEventHandler
from plugins.werewolf_game.models import Phase, Role
from plugins.werewolf_game.plugin import WerewolfGamePlugin
from plugins.werewolf_game.service import WerewolfGameService
from src.core.models.message import Message
from src.kernel.event import EventDecision


async def test_new_werewolf_install_is_disabled_without_creating_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = WerewolfConfig()
    plugin = WerewolfGamePlugin(config)

    assert config.plugin.enabled is False
    assert plugin.get_components() == []
    assert not (tmp_path / "runtime" / "api" / "tabletop.sqlite3").exists()


async def test_explicitly_enabled_werewolf_keeps_components_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = WerewolfConfig(plugin={"enabled": True})
    plugin = WerewolfGamePlugin(config)

    assert plugin.get_components() == [
        WerewolfGameService,
        WerewolfCommandEventHandler,
    ]
    assert (tmp_path / "runtime" / "api" / "tabletop.sqlite3").is_file()

    await plugin.on_plugin_unloaded()


def _game_with_players(count: int = 6):
    engine = WerewolfEngine()
    game = engine.create_game(
        platform="qq",
        group_id="100",
        group_name="group",
        group_stream_id="stream",
        owner_id="u1",
        board_name="6人新手局",
    )
    for index in range(1, count + 1):
        engine.add_player(game, user_id=f"u{index}", display_name=f"P{index}")
    return engine, game


def test_player_view_does_not_expose_full_role_table() -> None:
    engine, game = _game_with_players()
    result = engine.start_game(game, rng=random.Random(4))
    assert result.ok is True

    viewer = next(player for player in game.players.values() if player.role != Role.WEREWOLF)
    view = engine.player_view(game, viewer.user_id)

    assert f"你的身份：{viewer.role_label}" in view
    public_players = view.split("玩家列表：", 1)[1]
    for role_label in ("狼人", "预言家", "女巫", "猎人", "守卫"):
        assert role_label not in public_players


def test_wolf_view_only_exposes_wolf_teammates() -> None:
    engine, game = _game_with_players()
    result = engine.start_game(game, rng=random.Random(1))
    assert result.ok is True

    wolf = next(player for player in game.players.values() if player.role == Role.WEREWOLF)
    view = engine.player_view(game, wolf.user_id)

    assert "你的身份：狼人" in view
    assert "狼队友：" in view
    assert "预言家" not in view
    assert "女巫" not in view


def test_three_player_test_game_can_start_but_normal_game_still_requires_six() -> None:
    engine, game = _game_with_players(count=3)

    normal = engine.start_game(game, rng=random.Random(1))
    assert normal.ok is False
    assert "需要 6 名玩家" in normal.message

    test_result = engine.start_test_game(game, rng=random.Random(1))
    assert test_result.ok is True
    assert game.phase == Phase.NIGHT

    roles = {player.role for player in game.players.values()}
    assert roles == {Role.WEREWOLF, Role.SEER, Role.VILLAGER}


def test_public_status_includes_next_step_guidance() -> None:
    engine, game = _game_with_players(count=3)

    waiting_status = engine.public_status(game)
    assert "下一步：" in waiting_status
    assert "/狼人杀 测试开始" in waiting_status

    result = engine.start_test_game(game, rng=random.Random(1))
    assert result.public_messages
    assert "下一步：" in result.public_messages[0]
    assert "私聊里行动或跳过" in result.public_messages[0]


async def test_private_werewolf_command_is_intercepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyPlugin:
        pass

    plugin = DummyPlugin()
    service = WerewolfGameService(plugin=plugin)
    game = service.engine.create_game(
        platform="qq",
        group_id="100",
        group_name="group",
        group_stream_id="stream",
        owner_id="u1",
    )
    service.games[game.key] = game
    service.engine.add_player(game, user_id="u1", display_name="P1")

    sent: list[tuple[str, str, str]] = []

    async def fake_private(self, platform: str, user_id: str, text: str) -> bool:
        del self
        sent.append((platform, user_id, text))
        return True

    monkeypatch.setattr(WerewolfGameService, "_send_private", fake_private)

    message = Message(
        content="/狼人杀 身份",
        processed_plain_text="/狼人杀 身份",
        sender_id="u1",
        sender_name="P1",
        platform="qq",
        chat_type="private",
        stream_id="private-stream",
    )
    handler = WerewolfCommandEventHandler(plugin=plugin)  # type: ignore[arg-type]

    decision, _ = await handler.execute("on_message_received", {"message": message})

    assert decision == EventDecision.STOP
    assert sent == [("qq", "u1", service.engine.player_view(game, "u1"))]


def test_night_resolution_keeps_public_message_role_free() -> None:
    engine, game = _game_with_players()
    result = engine.start_game(game, rng=random.Random(2))
    assert result.ok is True
    game.phase = Phase.NIGHT

    wolf = next(player for player in game.players.values() if player.role == Role.WEREWOLF)
    seer = next(player for player in game.players.values() if player.role == Role.SEER)
    witch = next(player for player in game.players.values() if player.role == Role.WITCH)
    target = next(
        player
        for player in game.players.values()
        if player.role not in {Role.WEREWOLF, Role.SEER, Role.WITCH}
    )

    engine.night_action(
        game,
        actor_id=wolf.user_id,
        action="kill",
        target_id=target.user_id,
    )
    engine.night_action(
        game,
        actor_id=seer.user_id,
        action="check",
        target_id=wolf.user_id,
    )
    resolution = engine.night_action(game, actor_id=witch.user_id, action="pass")

    assert resolution.public_messages
    joined = "\n".join(resolution.public_messages)
    assert target.display_name in joined
    assert "身份：狼人" not in joined
    assert "身份：预言家" not in joined
    assert "身份：女巫" not in joined
