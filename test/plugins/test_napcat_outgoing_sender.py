from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.napcat_adapter.outgoing.sender import (
    NapCatDeliveryUnknownError,
    OutgoingSender,
)


def _private_text_envelope() -> dict:
    return {
        "message_info": {
            "user_info": {"user_id": "123456"},
        },
        "message_segment": {
            "type": "text",
            "data": "hello",
        },
    }


def _config(timeout_seconds: float = 20.0) -> SimpleNamespace:
    return SimpleNamespace(
        features=SimpleNamespace(
            message_send_timeout_seconds=timeout_seconds,
        )
    )


async def test_napcat_sender_passes_bounded_nt_receipt_timeout() -> None:
    client = SimpleNamespace(
        call=AsyncMock(
            return_value={
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": "message-1"},
            }
        )
    )
    sender = OutgoingSender(client, lambda: _config(18.0))

    await sender.send(_private_text_envelope())

    client.call.assert_awaited_once_with(
        "send_private_msg",
        {
            "user_id": 123456,
            "message": [{"type": "text", "data": {"text": "hello"}}],
            "timeout": 18_000,
        },
        timeout=23.0,
    )


async def test_napcat_nt_send_receipt_timeout_is_delivery_unknown() -> None:
    client = SimpleNamespace(
        call=AsyncMock(
            return_value={
                "status": "failed",
                "retcode": 1200,
                "data": None,
                "message": (
                    "Timeout: NTEvent serviceAndMethod:"
                    "NodeIKernelMsgService/sendMsg ListenerName:"
                    "NodeIKernelMsgListener/onMsgInfoListUpdate EventRet:\n{}\n"
                ),
            }
        )
    )
    sender = OutgoingSender(client, lambda: _config())

    with pytest.raises(NapCatDeliveryUnknownError) as exc_info:
        await sender.send(_private_text_envelope())

    assert exc_info.value.delivery_unknown is True
    assert exc_info.value.retcode == 1200
    assert exc_info.value.part == 1
    assert exc_info.value.total == 1


async def test_napcat_non_timeout_failure_remains_a_normal_failure() -> None:
    client = SimpleNamespace(
        call=AsyncMock(
            return_value={
                "status": "failed",
                "retcode": 1200,
                "data": None,
                "message": "permission denied",
            }
        )
    )
    sender = OutgoingSender(client, lambda: _config())

    with pytest.raises(RuntimeError, match="消息发送失败") as exc_info:
        await sender.send(_private_text_envelope())

    assert not isinstance(exc_info.value, NapCatDeliveryUnknownError)
