from __future__ import annotations

import pytest

from src.core.transport.message_receive.converter import MessageConverter
from src.core.transport.wire import UserRole


def _envelope(role: str | UserRole) -> dict:
    return {
        "message_info": {
            "message_id": "role-message",
            "platform": "qq",
            "user_info": {
                "user_id": "user-1",
                "user_nickname": "Tester",
                "role": role,
            },
        },
        "message_segment": [{"type": "text", "data": "hello"}],
    }


@pytest.mark.parametrize(
    ("role", "expected"),
    [("member", "member"), (UserRole.OPERATOR, "operator")],
)
async def test_envelope_to_message_accepts_role_string_and_enum(
    role: str | UserRole,
    expected: str,
) -> None:
    message = await MessageConverter().envelope_to_message(_envelope(role))

    assert message.sender_role == expected
