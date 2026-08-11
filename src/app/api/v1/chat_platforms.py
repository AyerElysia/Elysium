"""Provider-specific chat facades kept behind the P3-06 domain contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .chat_commands import (
    CapabilityError,
    ChatAction,
    ChatTarget,
    DeliveryUnknownError,
)


class FeishuActionClient(Protocol):
    async def execute_action(
        self,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]: ...


class NapCatPrivateClient(Protocol):
    async def delete_msg(self, message_id: int) -> dict[str, Any]: ...

    async def set_msg_emoji_like(
        self,
        message_id: int,
        emoji_id: str,
        set_like: bool = True,
    ) -> dict[str, Any]: ...

    async def mark_msg_as_read(self, message_id: int) -> dict[str, Any]: ...

    async def forward_friend_single_msg(
        self,
        message_id: int,
        user_id: int,
    ) -> dict[str, Any]: ...

    async def forward_group_single_msg(
        self,
        message_id: int,
        group_id: int,
    ) -> dict[str, Any]: ...

    async def send_poke(
        self,
        user_id: int,
        group_id: int | None = None,
    ) -> dict[str, Any]: ...

    async def send_group_notice(
        self,
        group_id: int,
        content: str,
        image: str = "",
    ) -> dict[str, Any]: ...

    async def delete_group_notice(
        self,
        group_id: int,
        notice_id: str,
    ) -> dict[str, Any]: ...

    async def set_essence_msg(self, message_id: int) -> dict[str, Any]: ...

    async def delete_essence_msg(self, message_id: int) -> dict[str, Any]: ...


_FEISHU_ACTIONS: Mapping[ChatAction, str] = {
    ChatAction.EDIT: "edit_message",
    ChatAction.RECALL: "delete_message",
    ChatAction.REACTION_ADD: "add_reaction",
    ChatAction.PIN: "pin_message",
    ChatAction.UNPIN: "unpin_message",
}


@dataclass(slots=True)
class FeishuChatFacade:
    """Allowlisted wrapper around ``FeishuAdapter.execute_action``."""

    client: FeishuActionClient
    platform: str = "feishu"

    def capabilities(self) -> Mapping[ChatAction, bool]:
        return {action: action in _FEISHU_ACTIONS for action in ChatAction}

    async def perform(
        self,
        action: ChatAction,
        *,
        target: ChatTarget,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        provider_action = _FEISHU_ACTIONS.get(action)
        if provider_action is None:
            raise CapabilityError(f"Feishu does not support {action.value!r}")
        message_id = _message_id(target)
        params: dict[str, Any] = {"message_id": message_id}
        if action is ChatAction.EDIT:
            params["text"] = _text_parts(payload)
        elif action is ChatAction.REACTION_ADD:
            params["emoji_type"] = _required_text(payload, "reaction")
        result = await self.client.execute_action(provider_action, params)
        status = result.get("status")
        if status == "ok":
            data = result.get("data")
            return data if isinstance(data, Mapping) else {"provider_status": "ok"}
        if status == "unknown":
            raise DeliveryUnknownError
        raise RuntimeError("Feishu confirmed the action failed")


_NAPCAT_CAPABILITIES = frozenset(
    {
        ChatAction.RECALL,
        ChatAction.REACTION_ADD,
        ChatAction.REACTION_REMOVE,
        ChatAction.MARK_READ,
        ChatAction.FORWARD,
        ChatAction.POKE,
        ChatAction.ANNOUNCEMENT_PUBLISH,
        ChatAction.ANNOUNCEMENT_DELETE,
        ChatAction.PIN,
        ChatAction.UNPIN,
    }
)


@dataclass(slots=True)
class NapCatChatFacade:
    """Allowlisted wrapper around the NapCat private client."""

    client: NapCatPrivateClient
    platform: str = "qq"

    def capabilities(self) -> Mapping[ChatAction, bool]:
        return {action: action in _NAPCAT_CAPABILITIES for action in ChatAction}

    async def perform(
        self,
        action: ChatAction,
        *,
        target: ChatTarget,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        if action not in _NAPCAT_CAPABILITIES:
            raise CapabilityError(f"NapCat does not support {action.value!r}")
        message_id = (
            _integer_message_id(target)
            if action
            in {
                ChatAction.RECALL,
                ChatAction.REACTION_ADD,
                ChatAction.REACTION_REMOVE,
                ChatAction.MARK_READ,
                ChatAction.PIN,
                ChatAction.UNPIN,
            }
            else None
        )
        if action is ChatAction.RECALL:
            assert message_id is not None
            result = await self.client.delete_msg(message_id)
        elif action in {ChatAction.REACTION_ADD, ChatAction.REACTION_REMOVE}:
            assert message_id is not None
            result = await self.client.set_msg_emoji_like(
                message_id,
                _required_text(payload, "reaction"),
                action is ChatAction.REACTION_ADD,
            )
        elif action is ChatAction.MARK_READ:
            assert message_id is not None
            result = await self.client.mark_msg_as_read(message_id)
        elif action is ChatAction.FORWARD:
            source_ids = payload.get("message_ids")
            if not isinstance(source_ids, list) or len(source_ids) != 1:
                raise CapabilityError("NapCat single-message forward requires one message id")
            source_id = _integer(source_ids[0], "message_ids[0]")
            if "group_id" in target.provider_target:
                result = await self.client.forward_group_single_msg(
                    source_id,
                    _integer(target.provider_target["group_id"], "group_id"),
                )
            else:
                result = await self.client.forward_friend_single_msg(
                    source_id,
                    _integer(target.provider_target.get("user_id"), "user_id"),
                )
        elif action is ChatAction.POKE:
            group_id = target.provider_target.get("group_id")
            result = await self.client.send_poke(
                _integer(payload.get("target_id"), "target_id"),
                _integer(group_id, "group_id") if group_id is not None else None,
            )
        elif action is ChatAction.ANNOUNCEMENT_PUBLISH:
            result = await self.client.send_group_notice(
                _integer(target.provider_target.get("group_id"), "group_id"),
                _required_text(payload, "content"),
            )
        elif action is ChatAction.ANNOUNCEMENT_DELETE:
            result = await self.client.delete_group_notice(
                _integer(target.provider_target.get("group_id"), "group_id"),
                _required_text(payload, "announcement_id"),
            )
        elif action is ChatAction.PIN:
            assert message_id is not None
            result = await self.client.set_essence_msg(message_id)
        else:
            assert message_id is not None
            result = await self.client.delete_essence_msg(message_id)
        return dict(result)


@dataclass(slots=True)
class AylaChatFacade:
    """Ayla 应用通道命令 facade（本期能力空）。

    Ayla 是独立应用聊天通道：撤回/已读/表情等命令操作由 Ayla 应用内
    自有逻辑处理，Elysium 命令端点不代为执行。本期 `capabilities()` 全
    False，保证命令端点对 ayla 流以 `capability_disabled` 拒绝、可观测，
    不误路由到其它平台 facade。
    """

    client: Any | None = None
    platform: str = "ayla"

    def capabilities(self) -> Mapping[ChatAction, bool]:
        return {action: False for action in ChatAction}

    async def perform(
        self,
        action: ChatAction,
        *,
        target: ChatTarget,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        raise CapabilityError(f"Ayla does not support {action.value!r}")


def _message_id(target: ChatTarget) -> str:
    value = target.provider_message_id
    if value is None or not str(value).strip():
        raise ValueError("provider message id is required")
    return str(value)


def _integer_message_id(target: ChatTarget) -> int:
    return _integer(_message_id(target), "provider_message_id")


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer id")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer id") from exc
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _text_parts(payload: Mapping[str, Any]) -> str:
    values = payload.get("parts")
    if not isinstance(values, list):
        raise TypeError("parts are required")
    text = "".join(
        str(item.get("text") or "")
        for item in values
        if isinstance(item, Mapping) and item.get("type") == "text"
    )
    if not text:
        raise CapabilityError("Feishu edit currently supports text parts only")
    if any(
        isinstance(item, Mapping) and item.get("type") != "text"
        for item in values
    ):
        raise CapabilityError("Feishu edit currently supports text parts only")
    return text


__all__ = ["FeishuChatFacade", "NapCatChatFacade", "AylaChatFacade"]
