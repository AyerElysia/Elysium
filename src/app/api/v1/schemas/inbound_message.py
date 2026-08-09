"""Strict public schemas for inbound message injection."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from .common import VersionedModel

Identifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)
]
ChatType = Literal["private", "group"]


class InboundMessageInjectRequest(VersionedModel):
    """Inject one inbound message into the standard receive pipeline.

    独立应用收到用户消息后，通过此请求把消息交给 Elysium 主链
    （``ON_MESSAGE_RECEIVED`` → Distributor → Chatter）触发爱莉思考；
    回复由发送/回复命令端点发回应用侧，本端点不直接产生对外发送。

    ``platform``/``chat_type`` 可选：显式提供时直接作为注入消息的平台与
    聊天类型，覆盖账本历史投影（避免把独立应用消息误判为已停用平台的来源）；
    省略时从权威账本投影该 stream 的既有 provider 身份，账本不可投影则拒绝。
    """

    stream_id: Identifier
    content: str = Field(min_length=1, max_length=100_000)
    sender_name: Identifier | None = None
    sender_id: Identifier | None = None
    sender_cardname: Identifier | None = None
    chat_type: ChatType | None = None
    platform: Identifier | None = None


class InboundMessageInjectResult(VersionedModel):
    """Confirmation that the inbound message entered the receive pipeline."""

    message_id: str
    stream_id: str
    accepted: bool = True


__all__ = [
    "ChatType",
    "Identifier",
    "InboundMessageInjectRequest",
    "InboundMessageInjectResult",
]
