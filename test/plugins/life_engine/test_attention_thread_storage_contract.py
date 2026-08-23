"""Selectable local contract for subject-level AttentionThread authority."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.attention_threads import (
    AttentionThreadActorInactive,
    AttentionThreadCommand,
    AttentionThreadConflict,
    AttentionThreadPageQuery,
    AttentionThreadProjectionConflict,
    AttentionThreadTransitionError,
    InstanceFocus,
)
from plugins.life_engine.storage.attention_factory import (
    AttentionThreadStores,
    open_attention_thread_stores,
)
from plugins.life_engine.storage.authority import (
    FileAuthorityRegistry,
    StaleAuthorityToken,
)
from plugins.life_engine.storage.domain_factory import open_presence_world_stores
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="attention-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="1" * 64,
        root_hashes={"attention_threads": "2" * 64},
        frontiers={"attention_threads": 0},
        created_at="2026-08-06T00:00:00+00:00",
        verified_at="2026-08-06T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@asynccontextmanager
async def _local_stores(
    tmp_path: Path,
) -> AsyncIterator[tuple[object, AttentionThreadStores, object]]:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    generation = _generation()
    await registry.register_generation(generation)
    token = await registry.activate_generation(
        generation.generation_id,
        expected_epoch=0,
        owner_id="attention-contract",
        lease_seconds=300,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=generation.generation_id,
            schema_version=1,
            authority_epoch=token.authority_epoch,
            authority_owner_id=token.owner_id,
            fencing_token_env="TEST_ATTENTION_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "life.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"TEST_ATTENTION_FENCE": token.fencing_token},
    )
    presence_world = await open_presence_world_stores(
        runtime,
        initialize_schema=True,
    )
    stores = await open_attention_thread_stores(
        runtime,
        initialize_schema=True,
    )
    try:
        yield runtime, stores, presence_world.presence
    finally:
        await runtime.close()
        try:
            await registry.revoke(token)
        except StaleAuthorityToken:
            pass


async def _register_actor(
    presence: object,
    *,
    instance_id: str = "consciousness:contract:1",
    status: str = "active",
    lease_expires_at: str = "",
) -> int:
    result = await presence.commit(  # type: ignore[attr-defined]
        {
            "instance_id": instance_id,
            "kind": "contract",
            "display_name": "contract actor",
            "status": status,
            "created_at": "2026-08-06T00:00:00+00:00",
            "last_active_at": "2026-08-06T00:00:00+00:00",
            "suspended_at": "",
            "stream_ids": [f"stream:{instance_id}"],
            "perception_filter": {},
            "metadata": {},
            "session_id": "session:contract",
            "process_epoch": "process:contract",
            "lease_expires_at": lease_expires_at,
            "lease_duration_seconds": None,
            "revision": 0,
        },
        expected_revision=None,
        event_type="consciousness.instance_registered",
        event_payload={"occurred_at": "2026-08-06T00:00:00+00:00"},
    )
    return result.revision


def _command(
    identity: str,
    *,
    action: str = "open",
    expected_revision: int = 0,
    statement: str = "我想继续留意同一主体在不同意识实例间如何保持连续。",
    thread_id: str = "attention:thread:continuity",
    actor: str = "consciousness:contract:1",
) -> AttentionThreadCommand:
    return AttentionThreadCommand(
        occurrence_id=f"attention:decision:{identity}",
        thread_id=thread_id,
        action=action,  # type: ignore[arg-type]
        actor_consciousness_instance_id=actor,
        source_instance_id=actor,
        source_occurrence_ids=(f"life:event:{identity}",),
        causation_occurrence_id=f"life:cause:{identity}",
        expected_revision=expected_revision,
        public_statement=statement,
        occurred_at="2026-08-06T01:02:03.123456+00:00",
    )


async def test_attention_authority_is_actor_gated_idempotent_and_cas_safe(
    tmp_path: Path,
) -> None:
    async with _local_stores(tmp_path) as (_, stores, presence):
        await _register_actor(presence)
        first_command = _command("open")
        first = await stores.authority.decide(first_command)
        replay = await stores.authority.decide(first_command)
        assert first.revision == 1
        assert replay == replace(first, idempotent_replay=True)

        with pytest.raises(AttentionThreadConflict):
            await stores.authority.decide(
                replace(first_command, public_statement="同 occurrence 的另一份内容")
            )
        with pytest.raises(AttentionThreadConflict):
            await stores.authority.decide(
                _command(
                    "stale",
                    action="note",
                    expected_revision=9,
                    statement="基于过期版本的决定不得自动合并。",
                )
            )
        page = await stores.authority.event_page(first.thread_id)
        assert [item.occurrence_id for item in page.items] == [
            first_command.occurrence_id
        ]

        with pytest.raises(AttentionThreadActorInactive):
            await stores.authority.decide(
                _command("missing-actor", actor="consciousness:missing")
            )


async def test_attention_explicit_transitions_concurrency_and_terminal_state(
    tmp_path: Path,
) -> None:
    async with _local_stores(tmp_path) as (_, stores, presence):
        await _register_actor(presence)
        await stores.authority.decide(_command("open"))

        results = await asyncio.gather(
            stores.authority.decide(
                _command(
                    "note-a",
                    action="note",
                    expected_revision=1,
                    statement="我选择先记录 A。",
                )
            ),
            stores.authority.decide(
                _command(
                    "note-b",
                    action="note",
                    expected_revision=1,
                    statement="我选择先记录 B。",
                )
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(value, Exception) for value in results) == 1
        assert sum(isinstance(value, AttentionThreadConflict) for value in results) == 1
        view = await stores.authority.get("attention:thread:continuity")
        assert view is not None and view.revision == 2

        paused = await stores.authority.decide(
            _command(
                "pause",
                action="pause",
                expected_revision=2,
                statement="",
            )
        )
        assert paused.status == "paused"
        with pytest.raises(AttentionThreadTransitionError):
            await stores.authority.decide(
                _command(
                    "paused-note",
                    action="note",
                    expected_revision=3,
                    statement="后台不能绕过显式恢复。",
                )
            )
        assert len((await stores.authority.event_page(paused.thread_id)).items) == 3

        await stores.authority.decide(
            _command(
                "resume",
                action="resume",
                expected_revision=3,
                statement="",
            )
        )
        closed = await stores.authority.decide(
            _command(
                "close",
                action="close",
                expected_revision=4,
                statement="我明确选择在这里结束这条关注。",
            )
        )
        assert closed.status == "closed"
        with pytest.raises(AttentionThreadTransitionError):
            await stores.authority.decide(
                _command(
                    "reopen",
                    action="resume",
                    expected_revision=5,
                    statement="",
                )
            )


async def test_attention_projection_paging_chunks_focus_and_restart(
    tmp_path: Path,
) -> None:
    async with _local_stores(tmp_path) as (runtime, stores, presence):
        await _register_actor(presence)
        for index in range(12):
            await stores.authority.decide(
                _command(
                    f"open-{index}",
                    statement=f"第 {index} 条" + "爱莉希雅🌸" * 3_000,
                    thread_id=f"attention:thread:{index:02d}",
                )
            )

        query = AttentionThreadPageQuery(
            limit=4,
            max_bytes=8 * 1024,
            projection_kind="heartbeat",
        )
        first = await stores.authority.page(query)
        assert len(first.content.encode("utf-8")) <= query.max_bytes
        assert first.omitted_count > 0
        assert first.continuation
        second = await stores.authority.page(
            replace(query, continuation=first.continuation)
        )
        assert {item.thread_id for item in first.items}.isdisjoint(
            item.thread_id for item in second.items
        )
        assert second.source_frontier == first.source_frontier
        with pytest.raises(AttentionThreadProjectionConflict):
            await stores.authority.page(
                replace(query, continuation=first.continuation[:-2] + "xx")
            )

        event = (await stores.authority.event_page("attention:thread:00")).items[0]
        chunks = []
        offset = 0
        while True:
            chunk = await stores.authority.read_statement_chunk(
                event.event_id,
                offset_bytes=offset,
                max_bytes=127,
            )
            chunks.append(chunk.content)
            offset = chunk.next_offset_bytes
            if chunk.complete:
                break
        assert "".join(chunks) == event.public_statement
        with pytest.raises(ValueError, match="splits UTF-8"):
            await stores.authority.read_statement_chunk(
                event.event_id,
                offset_bytes=len("第 0 条爱".encode()) - 1,
                max_bytes=127,
            )

        focus = InstanceFocus(
            instance_id="consciousness:contract:1",
            focus_occurrence_id="focus:contract:1",
            source_occurrence_id="life:event:focus:1",
            entered_at="2026-08-06T01:00:00+00:00",
            expires_at="2099-08-06T01:05:00+00:00",
            revision=1,
            thread_id="attention:thread:00",
        )
        assert await stores.focus.set_focus(focus) == focus
        assert await stores.focus.set_focus(focus) == focus
        assert await stores.focus.get_focus(focus.instance_id) == focus
        focused_page = await stores.authority.page(
            replace(query, focus_instance_id=focus.instance_id)
        )
        assert 'focus_instance="consciousness:contract:1"' in focused_page.content
        assert 'focus_thread_ref="attention:thread:00"' in focused_page.content

        reopened = await open_attention_thread_stores(runtime)
        assert await reopened.authority.get("attention:thread:00") == (
            await stores.authority.get("attention:thread:00")
        )
        assert await reopened.focus.get_focus(focus.instance_id) == focus
        await reopened.focus.clear_focus(focus.instance_id, expected_revision=1)
        assert await stores.focus.get_focus(focus.instance_id) is None

        await stores.authority.decide(
            _command(
                "frontier-change",
                statement="我明确打开另一条线索。",
                thread_id="attention:thread:frontier-change",
            )
        )
        with pytest.raises(AttentionThreadProjectionConflict, match="frontier"):
            await stores.authority.page(
                replace(query, continuation=first.continuation)
            )


async def test_attention_event_rows_are_database_immutable_and_health_is_content_free(
    tmp_path: Path,
) -> None:
    async with _local_stores(tmp_path) as (runtime, stores, presence):
        await _register_actor(presence)
        commit = await stores.authority.decide(_command("open"))
        with pytest.raises(DBAPIError, match="AttentionThreadEventImmutable"):
            async with runtime.unit_of_work() as uow:  # type: ignore[attr-defined]
                await uow.session.execute(
                    text(
                        """UPDATE attention_thread_events
                        SET public_statement = 'tampered'
                        WHERE event_id = :event_id"""
                    ),
                    {"event_id": commit.event_id},
                )

        health = await stores.authority.health_snapshot()
        assert health == {
            "status": "healthy",
            "event_count": 1,
            "source_frontier": 1,
            "threads": {"open": 1, "paused": 0, "closed": 0},
            "instance_focus_count": 0,
            "replayed_thread_count": 1,
            "consistency_error_types": (),
            "schema_version": 2,
        }
        assert "statement" not in str(health).lower()


async def test_attention_conflict_carries_current_revision_and_thread_exists(
    tmp_path: Path,
) -> None:
    """Stale revision / missing thread conflicts must expose recoverable hints."""
    async with _local_stores(tmp_path) as (_, stores, presence):
        await _register_actor(presence)
        opened = await stores.authority.decide(_command("open"))
        assert opened.revision == 1

        # 1) stale revision on an existing thread -> thread_exists + current revision
        with pytest.raises(AttentionThreadConflict) as caught:
            await stores.authority.decide(
                _command(
                    "stale-note",
                    action="note",
                    thread_id="attention:thread:continuity",
                    expected_revision=9,
                    statement="基于过期版本的决定不得自动合并。",
                )
            )
        exc = caught.value
        assert exc.thread_id == "attention:thread:continuity"
        assert exc.current_revision == 1
        assert exc.thread_exists is True

        # 2) missing thread with expected_revision != 0 -> not exists, current 0
        with pytest.raises(AttentionThreadConflict) as caught:
            await stores.authority.decide(
                _command(
                    "missing-thread-note",
                    action="note",
                    thread_id="attention:thread:does-not-exist",
                    expected_revision=3,
                    statement="指向不存在线索的提交应携带 not-exists 提示。",
                )
            )
        exc = caught.value
        assert exc.thread_id == "attention:thread:does-not-exist"
        assert exc.current_revision == 0
        assert exc.thread_exists is False

        # 3) opening an already-open thread with expected_revision=0 -> conflict
        with pytest.raises(AttentionThreadConflict) as caught:
            await stores.authority.decide(
                _command("reopen-existing", thread_id="attention:thread:continuity")
            )
        exc = caught.value
        assert exc.current_revision == 1
        assert exc.thread_exists is True
