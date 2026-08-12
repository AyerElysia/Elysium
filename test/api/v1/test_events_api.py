"""P3-03 授权事件 query 与 subscription 契约测试。"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plugins.life_engine.service.event_bus import (
    LifeEvent,
    RawEventGapError,
    RawEventStore,
)
from plugins.life_engine.storage.contracts import StorageRuntimeClosed
from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.events import EventQueryFailure, EventQueryService
from src.app.api.v1.policy import PLATFORM_SERVICE_AUDIENCE
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.schemas.events import EventFilter
from src.app.api.v1.tokens import SignedValueCodec


def _life_event(
    event_id: str,
    *,
    event_type: str = "chat.message.received",
    visibility: object = "private",
    actor_id: str = "actor-owner",
    stream_id: str = "stream-1",
    content: str = "private content",
) -> LifeEvent:
    now = datetime.now(UTC).isoformat()
    return LifeEvent(
        event_id=event_id,
        sequence=0,
        timestamp=now,
        source="test_adapter",
        channel="chat",
        event_type=event_type,
        content=content,
        stream_id=stream_id,
        source_instance_id="chat_global",
        metadata={
            "actor_id": actor_id,
            "visibility": visibility,
        },
        occurrence_id=f"occ_{event_id}",
    )


def _context(
    tmp_path: Path,
    store: RawEventStore,
) -> tuple[APIContext, AuthStore, EventQueryService]:
    auth = AuthStore(
        tmp_path / "auth.sqlite3",
        installation_id="installation-events",
    )
    codec = SignedValueCodec("e" * 48)
    service = EventQueryService(
        node_id="node-events",
        codec=codec,
        store_provider=lambda: store,
        poll_interval=0.01,
        heartbeat_interval=0.02,
    )
    return (
        APIContext(
            store=auth,
            codec=codec,
            installation_id="installation-events",
            events=service,
        ),
        auth,
        service,
    )


def _access_token(
    context: APIContext,
    *,
    actor_id: str,
    resource_grants: tuple[str, ...] = (),
) -> str:
    secret = f"events-secret-{actor_id}-long-enough"
    context.store.add_credential(
        actor_id=actor_id,
        audience=PLATFORM_SERVICE_AUDIENCE,
        role="platform_service",
        secret=secret,
        scopes=("events:read",),
        resource_grants=resource_grants,
    )
    response = TestClient(create_api_app(context)).post(
        "/auth/sessions",
        json={
            "grant_type": "service_credential",
            "audience": PLATFORM_SERVICE_AUDIENCE,
            "service_credential": secret,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_query_cursor_tracks_scanned_authoritative_position_without_leak(
    tmp_path: Path,
) -> None:
    store = RawEventStore(tmp_path / "life")
    hidden = await store.append(_life_event("evt-hidden"))
    visible = await store.append(
        _life_event("evt-public", visibility={"scope": "public", "audience": []})
    )
    context, auth, service = _context(tmp_path, store)
    session, _, _ = auth.issue_session_from_credential(
        credential=_register_service(auth, actor_id="actor-reader"),
        audience=PLATFORM_SERVICE_AUDIENCE,
        codec=context.codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    try:
        first = await service.query(
            cursor=None,
            limit=10,
            event_filter=EventFilter(),
            session=session,
        )
        assert [item.event_id for item in first.events] == [visible.event_id]
        assert first.events[0].payload is None
        assert first.scanned_count == 2
        position = context.codec.decode_cursor(
            first.next_cursor,
            ledger="life-events-v1",
        )
        assert position == visible.sequence
        assert hidden.event_id not in first.model_dump_json()

        resumed = await service.query(
            cursor=first.next_cursor,
            limit=10,
            event_filter=EventFilter(),
            session=session,
        )
        assert resumed.events == ()
        assert resumed.scanned_count == 0
    finally:
        auth.close()


def _register_service(auth: AuthStore, *, actor_id: str) -> str:
    secret = f"registered-secret-{actor_id}-long-enough"
    auth.add_credential(
        actor_id=actor_id,
        audience=PLATFORM_SERVICE_AUDIENCE,
        role="platform_service",
        secret=secret,
        scopes=("events:read",),
    )
    return secret


@pytest.mark.asyncio
async def test_get_event_hides_existence_and_payload_requires_authorization(
    tmp_path: Path,
) -> None:
    store = RawEventStore(tmp_path / "life")
    private = await store.append(_life_event("evt-private"))
    context, auth, service = _context(tmp_path, store)
    reader_secret = _register_service(auth, actor_id="actor-other")
    reader, _, _ = auth.issue_session_from_credential(
        credential=reader_secret,
        audience=PLATFORM_SERVICE_AUDIENCE,
        codec=context.codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    try:
        with pytest.raises(EventQueryFailure) as hidden:
            await service.get_event(
                private.event_id,
                event_filter=EventFilter(),
                session=reader,
            )
        assert hidden.value.code == "resource_not_found"

        granted_secret = "granted-secret-long-enough-12345"
        auth.add_credential(
            actor_id="actor-granted",
            audience=PLATFORM_SERVICE_AUDIENCE,
            role="platform_service",
            secret=granted_secret,
            scopes=("events:read",),
            resource_grants=(f"event:{private.event_id}",),
        )
        granted, _, _ = auth.issue_session_from_credential(
            credential=granted_secret,
            audience=PLATFORM_SERVICE_AUDIENCE,
            codec=context.codec,
            access_ttl=timedelta(minutes=5),
            refresh_ttl=timedelta(hours=1),
        )
        event = await service.get_event(
            private.event_id,
            event_filter=EventFilter(projection="full", include_payload=True),
            session=granted,
        )
        assert event.payload is not None
        assert event.payload["content"] == "private content"
    finally:
        auth.close()


@pytest.mark.asyncio
async def test_stream_history_to_live_boundary_is_complete_and_resumable(
    tmp_path: Path,
) -> None:
    store = RawEventStore(tmp_path / "life")
    first = await store.append(
        _life_event("evt-first", visibility={"scope": "public", "audience": []})
    )
    context, auth, service = _context(tmp_path, store)
    secret = _register_service(auth, actor_id="actor-stream")
    session, _, _ = auth.issue_session_from_credential(
        credential=secret,
        audience=PLATFORM_SERVICE_AUDIENCE,
        codec=context.codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    stream = service.stream(cursor=None, event_filter=EventFilter(), session=session)
    try:
        first_frame = await anext(stream)
        assert first.event_id in first_frame
        cursor = first_frame.split("id: ", 1)[1].splitlines()[0]

        second = await store.append(
            _life_event("evt-second", visibility={"scope": "public", "audience": []})
        )
        second_frame = await asyncio.wait_for(anext(stream), timeout=1)
        assert second.event_id in second_frame

        resumed = await service.query(
            cursor=cursor,
            limit=10,
            event_filter=EventFilter(),
            session=session,
        )
        assert [item.event_id for item in resumed.events] == [second.event_id]
    finally:
        await stream.aclose()
        auth.close()


@pytest.mark.asyncio
async def test_stream_heartbeat_survives_busy_tail(tmp_path: Path) -> None:
    """尾部持续有新事件（has_more 恒 True）时，heartbeat 仍按时发出。

    复现真实缺陷：事件库尾部持续产生不匹配过滤的事件（如 heartbeat），
    query 的 probe 恒返回结果 → has_more 恒为 True → stream 循环永不执行
    heartbeat 分支 → 长连接读超时误判断线。修复后 heartbeat 独立于
    has_more，只要超过间隔没有产出事件就必须发心跳帧。
    """

    class BusyStore:
        """read_since 恒返回一条不匹配事件（has_more 恒 True），永不 exhausted。"""

        def __init__(self) -> None:
            self.calls = 0

        async def read_since(self, sequence: int, *, limit: int | None = None):
            del sequence, limit
            self.calls += 1
            return [
                _life_event(
                    f"heartbeat-{self.calls}",
                    event_type="life_engine.heartbeat",
                    stream_id="other-stream",
                )
            ]

        async def read_tail(self, limit: int = 100):
            del limit
            return []

    context, auth, _ = _context(tmp_path, RawEventStore(tmp_path / "unused"))
    service = EventQueryService(
        node_id="node-events",
        codec=context.codec,
        store_provider=lambda: BusyStore(),  # type: ignore[return-value]
        poll_interval=0.01,
        heartbeat_interval=0.05,
    )
    secret = _register_service(auth, actor_id="actor-heartbeat")
    session, _, _ = auth.issue_session_from_credential(
        credential=secret,
        audience=PLATFORM_SERVICE_AUDIENCE,
        codec=context.codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    stream = service.stream(
        cursor=None,
        event_filter=EventFilter(event_type=("chat.message",)),
        session=session,
    )
    try:
        # 事件流里的 heartbeat 事件不匹配 chat.message 过滤 → 不会 yield；
        # 但 has_more 恒 True 时也必须按时产出 heartbeat 注释帧。
        frame = await asyncio.wait_for(anext(stream), timeout=1)
        assert frame.startswith(": heartbeat")
    finally:
        await stream.aclose()
        auth.close()


@pytest.mark.asyncio
async def test_stream_ends_gracefully_on_storage_closed(tmp_path: Path) -> None:
    """存储引擎已关闭（进程关闭窗口）时，订阅流优雅结束而非抛 ASGI Exception Group。

    复现真实缺陷：Elysium 关闭时 SSE 订阅仍在查询，read_since 抛
    StorageRuntimeClosed → stream 生成器把它抛成 unhandled TaskGroup error
    刷到控制台。修复后捕获并 return（生成器结束，anext 抛 StopAsyncIteration）。
    """

    class ClosingStore:
        async def read_since(self, sequence: int, *, limit: int | None = None):
            del sequence, limit
            raise StorageRuntimeClosed("storage runtime is closed")

        async def read_tail(self, limit: int = 100):
            del limit
            return []

    context, auth, _ = _context(tmp_path, RawEventStore(tmp_path / "unused"))
    service = EventQueryService(
        node_id="node-events",
        codec=context.codec,
        store_provider=lambda: ClosingStore(),  # type: ignore[return-value]
    )
    secret = _register_service(auth, actor_id="actor-closing")
    session, _, _ = auth.issue_session_from_credential(
        credential=secret,
        audience=PLATFORM_SERVICE_AUDIENCE,
        codec=context.codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    stream = service.stream(
        cursor=None,
        event_filter=EventFilter(),
        session=session,
    )
    try:
        # 优雅结束：生成器 return → StopAsyncIteration（不是 StorageRuntimeClosed 上抛）
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1)
    finally:
        await stream.aclose()
        auth.close()


@pytest.mark.asyncio
async def test_stream_ends_gracefully_on_alien_storage_closed_class(tmp_path: Path) -> None:
    """双模块路径下的 StorageRuntimeClosed 也要优雅结束（按异常名+消息识别）。

    实测（2026-08-12）：运行进程里 storage.contracts 以 life_engine.*（插件目录
    进 sys.path）加载，与 src 侧 plugins.life_engine.* 是两个类对象，except
    StorageRuntimeClosed 匹配不到；兜底按类名+消息识别关闭竞态。
    """

    class StorageRuntimeClosed(Exception):
        """模拟另一模块路径的同名异常类（与真实类同名但非同一类对象）。"""

    class ClosingAlienStore:
        async def read_since(self, sequence: int, *, limit: int | None = None):
            del sequence, limit
            raise StorageRuntimeClosed("storage runtime is closed")

        async def read_tail(self, limit: int = 100):
            del limit
            return []

    context, auth, _ = _context(tmp_path, RawEventStore(tmp_path / "unused"))
    service = EventQueryService(
        node_id="node-events",
        codec=context.codec,
        store_provider=lambda: ClosingAlienStore(),  # type: ignore[return-value]
    )
    secret = _register_service(auth, actor_id="actor-alien-closing")
    session, _, _ = auth.issue_session_from_credential(
        credential=secret,
        audience=PLATFORM_SERVICE_AUDIENCE,
        codec=context.codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    stream = service.stream(
        cursor=None,
        event_filter=EventFilter(),
        session=session,
    )
    try:
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1)
    finally:
        await stream.aclose()
        auth.close()


@pytest.mark.asyncio
async def test_history_gap_maps_to_safe_recovery_cursor(tmp_path: Path) -> None:
    class GapStore:
        async def read_since(self, sequence: int, *, limit: int | None = None):
            del limit
            raise RawEventGapError(sequence, 8)

    context, auth, _ = _context(tmp_path, RawEventStore(tmp_path / "unused"))
    service = EventQueryService(
        node_id="node-events",
        codec=context.codec,
        store_provider=lambda: GapStore(),  # type: ignore[return-value]
    )
    secret = _register_service(auth, actor_id="actor-gap")
    session, _, _ = auth.issue_session_from_credential(
        credential=secret,
        audience=PLATFORM_SERVICE_AUDIENCE,
        codec=context.codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    cursor = context.codec.encode_cursor(2, ledger="life-events-v1")
    try:
        with pytest.raises(EventQueryFailure) as gap:
            await service.query(
                cursor=cursor,
                limit=10,
                event_filter=EventFilter(),
                session=session,
            )
        assert gap.value.code == "history_gap"
        assert context.codec.decode_cursor(
            gap.value.recovery_cursor or "",
            ledger="life-events-v1",
        ) == 7
    finally:
        auth.close()


def test_event_http_query_validate_and_route_order(tmp_path: Path) -> None:
    store = RawEventStore(tmp_path / "life")
    asyncio.run(
        store.append(
            _life_event("evt-http", visibility={"scope": "public", "audience": []})
        )
    )
    context, auth, _ = _context(tmp_path, store)
    token = _access_token(context, actor_id="actor-http")
    client = TestClient(create_api_app(context))
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = client.get("/events", headers=headers)
        assert response.status_code == 200
        assert response.json()["events"][0]["event_id"] == "evt-http"

        validation = client.post(
            "/event-subscriptions/validate",
            headers=headers,
            json={"event_type": ["chat.message"], "projection": "summary"},
        )
        assert validation.status_code == 200
        assert validation.json()["transport"] == "sse"

        schema = create_api_app(context).openapi()
        assert schema["paths"]["/events"]["get"]["operationId"] == "queryEvents"
        assert schema["paths"]["/events/stream"]["get"]["operationId"] == "streamEvents"
        assert schema["paths"]["/events/{event_id}"]["get"]["operationId"] == "getEvent"
        assert "/events/ws" not in schema["paths"]
    finally:
        auth.close()
