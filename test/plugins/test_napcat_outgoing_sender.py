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


def _group_voice_envelope() -> dict:
    return {
        "message_info": {
            "group_info": {"group_id": "654321"},
            "user_info": {"user_id": "123456"},
        },
        "message_segment": {
            "type": "voice",
            "data": "UklGRg==",
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

    receipt = await sender.send(_private_text_envelope())

    assert receipt == {
        "status": "ok",
        "retcode": 0,
        "data": {"message_id": "message-1"},
    }
    client.call.assert_awaited_once_with(
        "send_private_msg",
        {
            "user_id": 123456,
            "message": [{"type": "text", "data": {"text": "hello"}}],
            "timeout": 18_000,
        },
        timeout=23.0,
    )


async def test_napcat_sender_sends_local_voice_to_group_as_onebot_record() -> None:
    client = SimpleNamespace(
        call=AsyncMock(
            return_value={
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": "group-voice-1"},
            }
        )
    )
    sender = OutgoingSender(client, lambda: _config())

    receipt = await sender.send(_group_voice_envelope())

    assert receipt == {
        "status": "ok",
        "retcode": 0,
        "data": {"message_id": "group-voice-1"},
    }
    client.call.assert_awaited_once_with(
        "send_group_msg",
        {
            "group_id": 654321,
            "message": [
                {"type": "record", "data": {"file": "base64://UklGRg=="}}
            ],
            "timeout": 20_000,
        },
        timeout=25.0,
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
