"""阶段三账本读取面契约：新 LifeEventStorePort 必须被 /api/v1 识别。

回归背景：``event_store_from_bot`` 原先只接受旧 ``RawEventStore``，导致阶段二
新存储后端（Local/MySQL ``LifeEventStorePort``）被误判为不可用，聊天与事件
查询、聊天发送的 stream 解析全部返回 ``component_unavailable``。本文件锁定
修复后的契约：按最小读取面（``read_since``）识别账本，而不是具体类型。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.service.event_bus import (
    LifeEvent,
    RawEventGapError,
)
from src.app.api.v1.chat import ChatQueryService
from src.app.api.v1.events import (
    EventQueryService,
    LifeEventLedgerReader,
    event_store_from_bot,
)
from src.app.api.v1.tokens import SignedValueCodec


def _codec() -> SignedValueCodec:
    return SignedValueCodec("s" * 48)


def _life_event(sequence: int) -> LifeEvent:
    return LifeEvent(
        event_id=f"evt_{sequence}",
        sequence=sequence,
        timestamp="2026-08-09T12:00:00+00:00",
        source="test_store",
        channel="chat",
        event_type="chat.message.received",
        content="hi",
        stream_id="stream-port",
        source_instance_id="chat_global",
        metadata={
            "actor_id": "actor-owner",
            "visibility": "private",
            "chat": {
                "message_id": f"msg_{sequence}",
                "stream_id": "stream-port",
                "platform": "feishu",
                "chat_type": "private",
                "direction": "received",
                "message_type": "text",
                "parts": [{"type": "text", "text": "hi"}],
            },
            "provider_identity": {
                "provider": "feishu",
                "adapter_signature": "feishu-adapter",
                "feishu_open_id": "ou_owner",
            },
        },
        occurrence_id=f"occ_{sequence}",
    )


class _ReaderFake:
    """只实现 LifeEventStorePort 读取面的轻量 fake。

    它故意不是 ``RawEventStore`` 也不是完整 ``LifeEventStorePort``，用于证明
    /api/v1 按鸭子类型（``read_since``/``read_tail``）工作，不依赖具体类型层次。
    """

    def __init__(self) -> None:
        self._events = [_life_event(1), _life_event(2), _life_event(3)]

    async def read_since(
        self,
        position: int,
        *,
        limit: int | None = None,
    ) -> list[LifeEvent]:
        batch = [e for e in self._events if e.sequence > position]
        if limit is not None:
            batch = batch[: int(limit)]
        return batch

    async def read_tail(self, limit: int = 100) -> list[LifeEvent]:
        batch = self._events[-int(limit) :]
        return list(batch)


class _GapReaderFake(_ReaderFake):
    """读取面存在但历史有缺口，验证 gap 仍正确映射。"""

    async def read_since(
        self,
        position: int,
        *,
        limit: int | None = None,
    ) -> list[LifeEvent]:
        raise RawEventGapError(position, 8)


def _bot_with_store(store: Any) -> Any:
    event_bus = SimpleNamespace(store=store)
    service = SimpleNamespace(_event_bus=event_bus)
    plugin = SimpleNamespace(_service=service)
    manager = SimpleNamespace(get_all_plugins=lambda: {"life_engine": plugin})
    return SimpleNamespace(plugin_manager=manager)


class TestEventStoreFromBotRecognizesReader:
    def test_new_store_port_shape_is_recognized(self) -> None:
        """LifeEventStorePort 形态（只有 read_since）能被识别，不再 component_unavailable。"""
        store = _ReaderFake()
        result = event_store_from_bot(_bot_with_store(store))
        assert result is store

    def test_legacy_raw_store_shape_is_recognized(self) -> None:
        """旧 RawEventStore 形态仍然被识别（不破坏既有部署）。"""

        import tempfile
        from pathlib import Path

        from plugins.life_engine.service.event_bus import RawEventStore

        with tempfile.TemporaryDirectory(prefix="rawstore_") as tmp:
            raw = RawEventStore(Path(tmp) / "life")
            result = event_store_from_bot(_bot_with_store(raw))
            assert result is raw

    def test_store_without_read_since_is_rejected(self) -> None:
        """没有读取面的对象仍被拒绝。"""
        store = SimpleNamespace(append=lambda e: e)  # 只有写入没有读取
        assert event_store_from_bot(_bot_with_store(store)) is None

    def test_missing_plugin_is_rejected(self) -> None:
        assert event_store_from_bot(SimpleNamespace(plugin_manager=None)) is None
        assert event_store_from_bot(SimpleNamespace()) is None


class TestEventQueryServiceAcceptsReader:
    async def test_query_reads_through_reader(self) -> None:
        service = EventQueryService(
            node_id="node-port",
            codec=_codec(),
            store_provider=lambda: _ReaderFake(),
            poll_interval=0.01,
            heartbeat_interval=0.02,
        )
        from datetime import UTC, datetime, timedelta

        from src.app.api.v1.auth_store import SessionRecord

        session = SessionRecord(
            session_id="sess_1",
            actor_id="actor-owner",
            audience="elysium-user-frontend",
            role="user",
            scopes=("events:read",),
            resource_grants=(),
            access_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            refresh_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        from src.app.api.v1.schemas.events import EventFilter

        page = await service.query(
            cursor=None,
            limit=10,
            event_filter=EventFilter(),
            session=session,
        )
        assert len(page.events) == 3

    async def test_gap_still_maps_to_history_gap(self) -> None:
        from datetime import UTC, datetime, timedelta

        from src.app.api.v1.auth_store import SessionRecord
        from src.app.api.v1.events import EventQueryFailure
        from src.app.api.v1.schemas.events import EventFilter

        service = EventQueryService(
            node_id="node-port",
            codec=_codec(),
            store_provider=lambda: _GapReaderFake(),
            poll_interval=0.01,
            heartbeat_interval=0.02,
        )
        session = SessionRecord(
            session_id="sess_1",
            actor_id="actor-owner",
            audience="elysium-user-frontend",
            role="user",
            scopes=("events:read",),
            resource_grants=(),
            access_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            refresh_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        with pytest.raises(EventQueryFailure) as exc:
            await service.query(
                cursor=_codec().encode_cursor(2, ledger="life-events-v1"),
                limit=10,
                event_filter=EventFilter(),
                session=session,
            )
        assert exc.value.code == "history_gap"


class TestChatQueryServiceAcceptsReader:
    async def test_streams_read_through_reader(self) -> None:
        from datetime import UTC, datetime, timedelta

        from src.app.api.v1.auth_store import SessionRecord

        service = ChatQueryService(
            codec=_codec(),
            store_provider=lambda: _ReaderFake(),
        )
        session = SessionRecord(
            session_id="sess_1",
            actor_id="actor-owner",
            audience="elysium-user-frontend",
            role="user",
            scopes=("chat:read",),
            resource_grants=(),
            access_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            refresh_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        page = await service.query_streams(cursor=None, limit=10, session=session)
        assert len(page.streams) == 1
        assert page.streams[0].stream_id == "stream-port"


@pytest.mark.parametrize(
    "store",
    [
        _ReaderFake(),
        _GapReaderFake(),
    ],
)
def test_reader_protocol_structural_check(store: Any) -> None:
    """LifeEventLedgerReader 按结构（read_since 存在）判定，不依赖类型层次。"""
    assert callable(getattr(store, "read_since", None))
    assert isinstance(store, LifeEventLedgerReader)
