"""Router failure semantics and versioned context-projection tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core import router
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.router_context_projection import (
    RouterContextDraft,
    RouterContextProjection,
    _sources_from_contents,
    read_subject_authority_sources,
)
from plugins.life_engine.service import core as service_core
from plugins.life_engine.service.core import LifeEngineService
from src.core.config.core_config import CoreConfig


class _Response:
    def __init__(self, message: str, *, reasoning: str = "") -> None:
        self.message = message
        self.reasoning_content = reasoning

    def __await__(self):
        async def collect() -> str:
            return self.message

        return collect().__await__()


class _Request:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.model_set = [{"model_identifier": "cloud-test", "max_context": 100_000}]
        self.payloads: list[object] = []

    def add_payload(self, payload: object) -> None:
        self.payloads.append(payload)

    async def send(self, *, stream: bool = False) -> _Response:
        assert stream is False
        return self.response


class _Chatter:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.tasks: list[str] = []

    def create_request(
        self,
        task: str = "expression",
        sub_task: str = "",
        *,
        with_reminder: str = "",
    ) -> _Request:
        assert sub_task == "router"
        assert with_reminder == "agent"
        self.tasks.append(task)
        return _Request(self.responses[task])


@pytest.fixture(autouse=True)
def _reset_router_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "_circuit_consecutive_failures", 0)
    monkeypatch.setattr(router, "_circuit_open_until", 0.0)
    monkeypatch.setattr(
        router,
        "get_core_config",
        lambda: SimpleNamespace(personality=SimpleNamespace(nickname="Elysia")),
    )
    monkeypatch.setattr(
        router,
        "_build_router_prompt",
        _fake_router_prompt,
    )


async def _fake_router_prompt(**_: object) -> str:
    return "router prompt"


def _stream() -> SimpleNamespace:
    return SimpleNamespace(bot_id="3427056465")


@pytest.mark.asyncio
async def test_reasoning_only_router_response_falls_back_to_agent() -> None:
    chatter = _Chatter(
        {
            "router": _Response(
                "",
                reasoning='{"reason":"tentative","should_respond":false}',
            ),
            "agent": _Response(
                '{"reason":"conversation is still between others",'
                '"should_respond":false}'
            ),
        }
    )

    result = await router.route_should_respond(
        chatter=chatter,
        logger=SimpleNamespace(
            debug=lambda *_: None,
            info=lambda *_: None,
            warning=lambda *_: None,
            error=lambda *_: None,
        ),
        unreads_text="new message",
        chat_stream=_stream(),
    )

    assert result["should_respond"] is False
    assert chatter.tasks == ["router", "agent"]


@pytest.mark.asyncio
async def test_router_rejects_string_boolean_and_uses_fallback() -> None:
    chatter = _Chatter(
        {
            "router": _Response(
                '{"reason":"not a real bool","should_respond":"false"}'
            ),
            "agent": _Response('{"reason":"hand to expression","should_respond":true}'),
        }
    )

    result = await router.route_should_respond(
        chatter=chatter,
        logger=SimpleNamespace(
            debug=lambda *_: None,
            info=lambda *_: None,
            warning=lambda *_: None,
            error=lambda *_: None,
        ),
        unreads_text="new message",
        chat_stream=_stream(),
    )

    assert result["should_respond"] is True
    assert chatter.tasks == ["router", "agent"]


@pytest.mark.asyncio
async def test_router_records_each_successful_generation_before_using_it() -> None:
    response = _Response(
        '{"reason":"交给表达层","should_respond":true}',
        reasoning="先判断这批消息是否应进入表达层",
    )
    response.request_record_id = "router-request-1"
    chatter = _Chatter({"router": response, "agent": response})
    recorded: list[tuple[object, str]] = []

    async def record_activity(raw_response: object, task: str) -> None:
        recorded.append((raw_response, task))

    result = await router.route_should_respond(
        chatter=chatter,
        logger=SimpleNamespace(
            debug=lambda *_: None,
            info=lambda *_: None,
            warning=lambda *_: None,
            error=lambda *_: None,
        ),
        unreads_text="new message",
        chat_stream=_stream(),
        activity_recorder=record_activity,
    )

    assert result["should_respond"] is True
    assert recorded == [(response, "router")]


@pytest.mark.asyncio
async def test_router_never_uses_a_generation_that_failed_activity_recording() -> None:
    chatter = _Chatter(
        {
            "router": _Response(
                '{"reason":"unrecorded","should_respond":false}'
            ),
            "agent": _Response(
                '{"reason":"recorded fallback","should_respond":true}'
            ),
        }
    )
    attempts: list[str] = []

    async def record_activity(_response: object, task: str) -> None:
        attempts.append(task)
        if task == "router":
            raise RuntimeError("ledger unavailable")

    result = await router.route_should_respond(
        chatter=chatter,
        logger=SimpleNamespace(
            debug=lambda *_: None,
            info=lambda *_: None,
            warning=lambda *_: None,
            error=lambda *_: None,
        ),
        unreads_text="new message",
        chat_stream=_stream(),
        activity_recorder=record_activity,
    )

    assert result["should_respond"] is True
    assert attempts == ["router", "agent"]


@pytest.mark.asyncio
async def test_router_exhaustion_preserves_message_for_subject() -> None:
    chatter = _Chatter(
        {
            "router": _Response(""),
            "agent": _Response("not json"),
        }
    )

    result = await router.route_should_respond(
        chatter=chatter,
        logger=SimpleNamespace(
            debug=lambda *_: None,
            info=lambda *_: None,
            warning=lambda *_: None,
            error=lambda *_: None,
        ),
        unreads_text="new message",
        chat_stream=_stream(),
    )

    assert result["should_respond"] is True
    assert "主体判断" in result["reason"]


@pytest.mark.asyncio
async def test_router_skips_duplicate_transport_fallback_chain() -> None:
    sends: list[tuple[str, list[str]]] = []
    info_logs: list[str] = []

    class TransportRequest(_Request):
        def __init__(self, task: str) -> None:
            super().__init__(_Response(""))
            self.task = task
            self.model_set = [
                {
                    "api_provider": "gateway",
                    "base_url": "http://127.0.0.1:3000/v1",
                    "model_identifier": "same-model",
                    "max_context": 100_000,
                }
            ]

        async def send(self, *, stream: bool = False) -> _Response:
            sends.append(
                (
                    self.task,
                    [str(model["model_identifier"]) for model in self.model_set],
                )
            )
            raise RuntimeError("gateway unavailable")

    class TransportChatter:
        def create_request(
            self,
            task: str = "expression",
            sub_task: str = "",
            *,
            with_reminder: str = "",
        ) -> TransportRequest:
            del sub_task, with_reminder
            return TransportRequest(task)

    result = await router.route_should_respond(
        chatter=TransportChatter(),
        logger=SimpleNamespace(
            debug=lambda *_: None,
            info=lambda message: info_logs.append(str(message)),
            warning=lambda *_: None,
            error=lambda *_: None,
        ),
        unreads_text="new message",
        chat_stream=_stream(),
    )

    assert result["should_respond"] is True
    assert sends == [("router", ["same-model"])]
    assert any("传输候选完全重复" in message for message in info_logs)


@pytest.mark.asyncio
async def test_router_fallback_keeps_only_new_transport_candidates() -> None:
    sends: list[tuple[str, list[str]]] = []

    def model(identifier: str) -> dict[str, object]:
        return {
            "api_provider": "gateway",
            "base_url": "http://127.0.0.1:3000/v1",
            "model_identifier": identifier,
            "max_context": 100_000,
        }

    class TransportRequest(_Request):
        def __init__(self, task: str) -> None:
            super().__init__(
                _Response('{"reason":"new backup","should_respond":false}')
            )
            self.task = task
            self.model_set = (
                [model("failed")]
                if task == "router"
                else [model("failed"), model("new-backup")]
            )

        async def send(self, *, stream: bool = False) -> _Response:
            sends.append(
                (
                    self.task,
                    [str(item["model_identifier"]) for item in self.model_set],
                )
            )
            if self.task == "router":
                raise RuntimeError("gateway unavailable")
            return self.response

    class TransportChatter:
        def create_request(
            self,
            task: str = "expression",
            sub_task: str = "",
            *,
            with_reminder: str = "",
        ) -> TransportRequest:
            del sub_task, with_reminder
            return TransportRequest(task)

    result = await router.route_should_respond(
        chatter=TransportChatter(),
        logger=SimpleNamespace(
            debug=lambda *_: None,
            info=lambda *_: None,
            warning=lambda *_: None,
            error=lambda *_: None,
        ),
        unreads_text="new message",
        chat_stream=_stream(),
    )

    assert result["should_respond"] is False
    assert sends == [
        ("router", ["failed"]),
        ("agent", ["new-backup"]),
    ]


def _write_sources(workspace: Path, *, user: str = "USER v1") -> None:
    (workspace / "SOUL.md").write_text("SOUL authority", encoding="utf-8")
    (workspace / "USER.md").write_text(user, encoding="utf-8")
    (workspace / "MEMORY.md").write_text("MEMORY authority", encoding="utf-8")


def test_subject_authority_revision_tracks_each_exact_source(tmp_path: Path) -> None:
    _write_sources(tmp_path)
    sources, initial_revision = read_subject_authority_sources(tmp_path)

    assert [source.path for source in sources] == ["SOUL.md", "USER.md", "MEMORY.md"]
    assert all(len(source.sha256) == 64 for source in sources)
    revisions = {initial_revision}
    for name in ("SOUL.md", "USER.md", "MEMORY.md"):
        path = tmp_path / name
        original = path.read_bytes()
        path.write_bytes(original + b"\nexact-change")
        changed_sources, revision = read_subject_authority_sources(tmp_path)
        assert revision not in revisions
        assert next(item for item in changed_sources if item.path == name).size_bytes == len(
            original
        ) + len(b"\nexact-change")
        revisions.add(revision)
        path.write_bytes(original)

    restored_sources, restored_revision = read_subject_authority_sources(tmp_path)
    assert restored_revision == initial_revision
    assert restored_sources == sources


@pytest.mark.asyncio
async def test_projection_is_content_addressed_and_keeps_old_versions(
    tmp_path: Path,
) -> None:
    _write_sources(tmp_path)
    calls: list[str] = []

    async def author(source_digest, sources):
        calls.append(source_digest)
        assert {source.path for source in sources} == {
            "SOUL.md",
            "USER.md",
            "MEMORY.md",
        }
        return RouterContextDraft(
            text=f"projection {len(calls)}",
            generator="test-author",
        )

    projection = RouterContextProjection(tmp_path, author=author)
    first = await projection.refresh()
    first_digest = projection.health_snapshot()["latest_source_digest"]

    assert "derived" in first.lower()
    assert await projection.refresh() == first
    assert calls == [first_digest]

    (tmp_path / "USER.md").write_text("USER v2", encoding="utf-8")
    second = await projection.refresh()
    second_digest = projection.health_snapshot()["latest_source_digest"]

    assert second != first
    assert second_digest != first_digest
    assert (projection.versions_dir / f"{first_digest}.md").is_file()
    assert (projection.versions_dir / f"{second_digest}.md").is_file()
    latest = json.loads(projection.latest_path.read_text(encoding="utf-8"))
    assert latest["source_digest"] == second_digest
    assert latest["authority"] == "derived_non_authoritative"


@pytest.mark.asyncio
async def test_selected_projection_uses_remote_subject_and_runtime_stores_only(
    tmp_path: Path,
) -> None:
    contents = {
        "SOUL.md": b"REMOTE SOUL",
        "USER.md": b"REMOTE USER",
        "MEMORY.md": b"REMOTE MEMORY",
    }
    _, revision = _sources_from_contents(contents)

    class _SubjectStore:
        def __init__(self) -> None:
            self.marker_calls = 0
            self.snapshot_calls = 0

        async def current_subject_change_marker(self) -> str:
            self.marker_calls += 1
            return revision

        async def current_subject_revision(self) -> str:
            raise AssertionError("热路径应优先读取轻量 subject head marker")

        async def read_subject_authority(self):
            self.snapshot_calls += 1
            commits = {
                name: SimpleNamespace(
                    version=SimpleNamespace(content_bytes=raw)
                )
                for name, raw in contents.items()
            }
            return SimpleNamespace(
                commits=commits,
                revision=revision,
                change_marker=revision,
            )

    class _RuntimeStore:
        def __init__(self) -> None:
            self.states: dict[tuple[str, str], SimpleNamespace] = {}

        async def get_state(self, namespace: str, state_key: str):
            return self.states.get((namespace, state_key))

        async def put_state(self, **kwargs):
            key = (str(kwargs["namespace"]), str(kwargs["state_key"]))
            current = self.states.get(key)
            expected = int(kwargs["expected_revision"])
            actual = int(current.revision) if current is not None else 0
            assert expected == actual
            record = SimpleNamespace(
                revision=actual + 1,
                payload=dict(kwargs["payload"]),
            )
            self.states[key] = record
            return record

    calls = 0

    async def author(source_digest, sources):
        nonlocal calls
        calls += 1
        assert source_digest == revision
        assert [source.text for source in sources] == [
            "REMOTE SOUL",
            "REMOTE USER",
            "REMOTE MEMORY",
        ]
        return RouterContextDraft(text="remote projection", generator="test-author")

    runtime_store = _RuntimeStore()
    subject_store = _SubjectStore()
    projection = RouterContextProjection(
        tmp_path,
        author=author,
        subject_store=subject_store,
        runtime_store=runtime_store,
    )

    first = await projection.refresh()
    assert "remote projection" in first
    assert await projection.ensure_current() == first
    assert calls == 1
    assert subject_store.snapshot_calls == 1
    assert subject_store.marker_calls == 1
    assert not projection.runtime_dir.exists()

    snapshot = await projection.ensure_current_snapshot()
    assert snapshot is not None
    assert snapshot["source_digest"] == revision
    assert snapshot["text"] == first
    assert await projection.get_snapshot(revision) == snapshot


@pytest.mark.asyncio
async def test_projection_failure_preserves_last_version_but_never_serves_it_stale(
    tmp_path: Path,
) -> None:
    _write_sources(tmp_path)
    should_fail = False

    async def author(source_digest, sources):
        del source_digest, sources
        if should_fail:
            raise RuntimeError("provider unavailable")
        return RouterContextDraft(text="current projection", generator="test-author")

    projection = RouterContextProjection(tmp_path, author=author)
    first = await projection.refresh()
    latest_before = projection.latest_path.read_text(encoding="utf-8")

    should_fail = True
    (tmp_path / "MEMORY.md").write_text("changed memory", encoding="utf-8")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await projection.ensure_current()
    assert projection.latest_path.read_text(encoding="utf-8") == latest_before
    assert "current projection" in first
    health = projection.health_snapshot()
    assert health["status"] == "degraded"
    assert health["fresh"] is False
    assert "provider unavailable" in health["degraded_reason"]


@pytest.mark.asyncio
async def test_projection_watcher_refreshes_notified_write_and_stops_cleanly(
    tmp_path: Path,
) -> None:
    _write_sources(tmp_path)
    refreshed = asyncio.Event()
    call_count = 0

    async def author(source_digest, sources):
        nonlocal call_count
        del source_digest, sources
        call_count += 1
        refreshed.set()
        return RouterContextDraft(
            text=f"projection {call_count}",
            generator="test-author",
        )

    projection = RouterContextProjection(
        tmp_path,
        author=author,
        poll_interval_seconds=60.0,
    )
    task = asyncio.create_task(projection.run())
    await asyncio.wait_for(refreshed.wait(), timeout=2.0)

    refreshed.clear()
    (tmp_path / "USER.md").write_text("same size", encoding="utf-8")
    assert projection.notify_source_changed("USER.md") is True
    await asyncio.wait_for(refreshed.wait(), timeout=2.0)

    projection.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert projection.health_snapshot()["running"] is False
    assert call_count == 2


@pytest.mark.asyncio
async def test_projection_watcher_retries_provider_failure_without_source_edit(
    tmp_path: Path,
) -> None:
    _write_sources(tmp_path)
    recovered = asyncio.Event()
    call_count = 0

    async def author(source_digest, sources):
        nonlocal call_count
        del source_digest, sources
        call_count += 1
        if call_count == 1:
            raise RuntimeError("temporary provider failure")
        recovered.set()
        return RouterContextDraft(text="recovered projection", generator="test-author")

    projection = RouterContextProjection(
        tmp_path,
        author=author,
        poll_interval_seconds=60.0,
        retry_base_seconds=0.05,
    )
    task = asyncio.create_task(projection.run())
    await asyncio.wait_for(recovered.wait(), timeout=2.0)

    projection.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
    health = projection.health_snapshot()
    assert call_count == 2
    assert health["fresh"] is True
    assert health["degraded_reason"] == ""
    assert health["retry_delay_seconds"] == 0.0


@pytest.mark.asyncio
async def test_projection_author_tries_next_cloud_model_after_empty_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_sources(tmp_path)
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    config.chatter.router_context_projection_task_name = "projection-test"
    service = LifeEngineService(
        SimpleNamespace(
            config=config,
            global_storage_config=CoreConfig(
                storage=CoreConfig.StorageSection(backend="local")
            ),
        )
    )
    projection = RouterContextProjection(
        tmp_path,
        author=service._author_router_context_projection,
    )
    sources, source_digest = projection._read_sources()
    requested_models: list[str] = []

    class _ProjectionRequest:
        def __init__(self, model_identifier: str) -> None:
            self.model_identifier = model_identifier
            self.payloads: list[object] = []

        def add_payload(self, payload: object) -> None:
            self.payloads.append(payload)

        async def send(self, *, stream: bool = False) -> _Response:
            assert stream is False
            requested_models.append(self.model_identifier)
            if self.model_identifier == "thinking-empty":
                return _Response("", reasoning="long reasoning without final")
            return _Response("compact projection")

    monkeypatch.setattr(
        service_core,
        "get_model_set_by_task",
        lambda task: [
            {"model_identifier": "thinking-empty"},
            {"model_identifier": "cloud-second"},
        ],
    )
    monkeypatch.setattr(
        service_core,
        "create_llm_request",
        lambda model_set, request_name: _ProjectionRequest(
            model_set[0]["model_identifier"]
        ),
    )

    draft = await service._author_router_context_projection(
        source_digest,
        sources,
    )

    assert draft.text == "compact projection"
    assert draft.generator.endswith("model:cloud-second")
    assert requested_models == ["thinking-empty", "cloud-second"]
