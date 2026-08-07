"""Performance contracts for the life_chatter global history read model."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.life_engine.core.chat_history import (
    collect_global_chat_history_entries_from_db,
)


@pytest.mark.asyncio
async def test_global_history_batches_metadata_without_per_message_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    messages = [
        SimpleNamespace(
            id=index,
            message_id=f"m{index}",
            stream_id="stream-one" if index % 2 else "stream-two",
            person_id="person-one",
            time=1_700_000_000.0 + index,
            message_type="text",
            content=f"body-{index}",
            processed_plain_text=f"body-{index}",
            reply_to=None,
            platform="feishu",
        )
        for index in range(1, 21)
    ]
    streams = [
        SimpleNamespace(
            stream_id="stream-one",
            group_name="私聊一",
            platform="feishu",
            chat_type="private",
        ),
        SimpleNamespace(
            stream_id="stream-two",
            group_name="私聊二",
            platform="feishu",
            chat_type="private",
        ),
    ]
    people = [
        SimpleNamespace(
            person_id="person-one",
            user_id="open-id-one",
            nickname="汐汐",
            cardname="",
        )
    ]

    class _QueryBuilder:
        def __init__(self, model: object) -> None:
            self.model_name = getattr(model, "__name__", type(model).__name__)

        def filter(self, **_kwargs: object) -> _QueryBuilder:
            return self

        def order_by(self, *_args: object) -> _QueryBuilder:
            return self

        def limit(self, *_args: object) -> _QueryBuilder:
            return self

        async def all(self) -> list[object]:
            calls.append(self.model_name)
            if self.model_name == "Messages":
                return list(reversed(messages))
            if self.model_name == "ChatStreams":
                return streams
            if self.model_name == "PersonInfo":
                return people
            raise AssertionError(self.model_name)

    monkeypatch.setattr("src.kernel.db.QueryBuilder", _QueryBuilder)
    entries = await collect_global_chat_history_entries_from_db(
        SimpleNamespace(stream_id="stream-one"),
        max_messages=10,
        stream_manager=None,
    )

    assert calls == ["Messages", "ChatStreams", "PersonInfo"]
    assert len(entries) == 10
    assert {entry.message.sender_name for entry in entries} == {"汐汐"}
    assert {entry.stream_name for entry in entries} == {"私聊一", "私聊二"}
