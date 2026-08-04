"""Provider facade support-matrix tests for P3-06."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.app.api.v1.chat_commands import CapabilityError, ChatAction, ChatTarget
from src.app.api.v1.chat_platforms import FeishuChatFacade, NapCatChatFacade


@pytest.mark.asyncio
async def test_feishu_facade_uses_allowlisted_action() -> None:
    client = SimpleNamespace(
        execute_action=AsyncMock(return_value={"status": "ok", "data": {"id": "r1"}})
    )
    facade = FeishuChatFacade(client)
    target = ChatTarget(
        "stream-1",
        "feishu",
        "private",
        provider_message_id="om_1",
    )
    result = await facade.perform(
        ChatAction.REACTION_ADD,
        target=target,
        payload={"reaction": "THUMBSUP"},
    )
    assert result == {"id": "r1"}
    client.execute_action.assert_awaited_once_with(
        "add_reaction",
        {"message_id": "om_1", "emoji_type": "THUMBSUP"},
    )


@pytest.mark.asyncio
async def test_feishu_unsupported_action_is_not_degraded() -> None:
    facade = FeishuChatFacade(SimpleNamespace(execute_action=AsyncMock()))
    with pytest.raises(CapabilityError):
        await facade.perform(
            ChatAction.POKE,
            target=ChatTarget("stream-1", "feishu", "private"),
            payload={"target_id": "user-1"},
        )


@pytest.mark.asyncio
async def test_napcat_poke_and_announcement_use_private_client_facade() -> None:
    client = SimpleNamespace(
        send_poke=AsyncMock(return_value={"status": "ok"}),
        send_group_notice=AsyncMock(return_value={"status": "ok"}),
    )
    facade = NapCatChatFacade(client)
    target = ChatTarget(
        "stream-1",
        "qq",
        "group",
        provider_target={"group_id": "123"},
    )
    await facade.perform(ChatAction.POKE, target=target, payload={"target_id": "456"})
    await facade.perform(
        ChatAction.ANNOUNCEMENT_PUBLISH,
        target=target,
        payload={"content": "notice"},
    )
    client.send_poke.assert_awaited_once_with(456, 123)
    client.send_group_notice.assert_awaited_once_with(123, "notice")


def test_provider_support_matrix_is_explicit() -> None:
    feishu = FeishuChatFacade(SimpleNamespace())
    napcat = NapCatChatFacade(SimpleNamespace())
    assert feishu.capabilities()[ChatAction.PIN] is True
    assert feishu.capabilities()[ChatAction.POKE] is False
    assert napcat.capabilities()[ChatAction.POKE] is True
    assert napcat.capabilities()[ChatAction.EDIT] is False
