"""Hand one danmaku to the existing live text path.

Platform ``live`` is already special: LifeChatter answers with ``life_send_text``,
the voice action is blocked, and the sender records the text without an
external adapter. Nothing here speaks or posts back to Bilibili.
"""

from __future__ import annotations

from uuid import uuid4

from src.core.models.message import Message
from src.core.models.stream import ChatStream

from .listen import ROOM_ID, Danmaku

PLATFORM = "live"
STREAM_ID = ChatStream.generate_stream_id(PLATFORM, group_id=ROOM_ID)
ADAPTER_SIGNATURE = "live_danmaku:router:live_danmaku"


def build_live_message(item: Danmaku) -> Message:
    message = Message(
        message_id=f"live_danmaku_{uuid4().hex}",
        content=item.text,
        processed_plain_text=item.text,
        sender_id=str(item.uid),
        sender_name=item.name,
        platform=PLATFORM,
        chat_type="group",
        stream_id=STREAM_ID,
    )
    message.extra.update(
        {
            "group_id": ROOM_ID,
            "group_name": "B站直播间 31581575",
        }
    )
    return message


async def submit_danmaku(item: Danmaku) -> bool:
    if not item.text.strip():
        return False
    from src.core.transport.message_receive import get_message_receiver

    return bool(
        await get_message_receiver().receive_message(
            build_live_message(item),
            ADAPTER_SIGNATURE,
        )
    )
