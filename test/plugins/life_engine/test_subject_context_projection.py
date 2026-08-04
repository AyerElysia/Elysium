"""Shared subject-context projection authority and pinning contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.subject_context_projection import (
    SUBJECT_CONTEXT_PROJECTION_VERSION,
    SubjectContextDraft,
    SubjectContextProjection,
)
from plugins.life_engine.service import core as service_core
from plugins.life_engine.service.core import LifeEngineService


class _Response:
    def __init__(self, message: str) -> None:
        self.message = message

    def __await__(self):
        async def collect() -> str:
            return self.message

        return collect().__await__()


def _write_authorities(workspace: Path) -> None:
    (workspace / "SOUL.md").write_text("soul-v1", encoding="utf-8")
    (workspace / "USER.md").write_text("user-v1", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("memory-v1", encoding="utf-8")


def _draft_from_sources(sources: tuple[Any, ...]) -> SubjectContextDraft:
    blocks = "\n".join(
        f'<subject-source path="{source.path}">\n'
        f"projection:{source.text}\n"
        "</subject-source>"
        for source in sources
    )
    return SubjectContextDraft(text=blocks, generator="test-author")


def _source_hashes(snapshot: dict[str, Any]) -> dict[str, str]:
    return {
        str(source["path"]): str(source["sha256"]) for source in snapshot["sources"]
    }


@pytest.mark.asyncio
async def test_each_authority_changes_revision_and_per_source_hash(
    tmp_path: Path,
) -> None:
    _write_authorities(tmp_path)

    async def author(_digest: str, sources: tuple[Any, ...]) -> SubjectContextDraft:
        return _draft_from_sources(sources)

    projection = SubjectContextProjection(
        str(tmp_path),
        projection_profile="voice_live",
        max_bytes=8192,
        author=author,
    )
    previous = await projection.ensure_current_snapshot()
    assert previous is not None
    first = previous

    for filename, content in (
        ("SOUL.md", "soul-v2"),
        ("USER.md", "user-v2"),
        ("MEMORY.md", "memory-v2"),
    ):
        before_hashes = _source_hashes(previous)
        (tmp_path / filename).write_text(content, encoding="utf-8")
        current = await projection.ensure_current_snapshot()
        assert current is not None
        after_hashes = _source_hashes(current)
        assert current["source_digest"] != previous["source_digest"]
        assert after_hashes[filename] != before_hashes[filename]
        assert all(
            after_hashes[name] == before_hashes[name]
            for name in before_hashes
            if name != filename
        )
        previous = current

    assert first["projection_profile"] == "voice_live"
    assert first["projection_algorithm"] == "llm_semantic_subject_continuity"
    assert first["projection_version"] == SUBJECT_CONTEXT_PROJECTION_VERSION
    assert first["authority"] == "derived_non_authoritative"
    assert set(first["budget"]["sources"]) == {
        "SOUL.md",
        "USER.md",
        "MEMORY.md",
    }
    assert all(source["sha256"] in first["text"] for source in first["sources"])
    assert projection.health_snapshot()["status"] == "ready"
    assert projection.notify_source_changed("USER.md") is True
    assert projection.health_snapshot()["status"] == "idle"
    restored = await projection.ensure_current_snapshot()
    assert restored is not None
    assert restored["projection_sha256"] == previous["projection_sha256"]
    assert all(
        values["original_bytes"] > 0 and values["delivered_bytes"] > 0
        for values in first["budget"]["sources"].values()
    )

    pinned = await projection.get_snapshot(
        str(first["source_digest"]),
        projection_version=int(first["projection_version"]),
    )
    assert pinned is not None
    assert pinned["projection_sha256"] == first["projection_sha256"]
    assert pinned["text"] == first["text"]


@pytest.mark.asyncio
async def test_profile_and_budget_are_part_of_immutable_projection_identity(
    tmp_path: Path,
) -> None:
    _write_authorities(tmp_path)

    async def author(_digest: str, sources: tuple[Any, ...]) -> SubjectContextDraft:
        return _draft_from_sources(sources)

    compact = SubjectContextProjection(
        str(tmp_path),
        projection_profile="voice_live",
        max_bytes=8192,
        author=author,
    )
    wider = SubjectContextProjection(
        str(tmp_path),
        projection_profile="voice_live",
        max_bytes=12288,
        author=author,
    )
    other_surface = SubjectContextProjection(
        str(tmp_path),
        projection_profile="conversation_router",
        max_bytes=8192,
        author=author,
    )

    compact_snapshot = await compact.ensure_current_snapshot()
    wider_snapshot = await wider.ensure_current_snapshot()
    other_snapshot = await other_surface.ensure_current_snapshot()

    assert compact_snapshot is not None
    assert wider_snapshot is not None
    assert other_snapshot is not None
    assert compact_snapshot["source_digest"] == wider_snapshot["source_digest"]
    assert compact_snapshot["projection_sha256"] != wider_snapshot["projection_sha256"]
    assert compact_snapshot["projection_sha256"] != other_snapshot["projection_sha256"]
    assert (
        await wider.get_snapshot(
            str(compact_snapshot["source_digest"]),
            projection_version=SUBJECT_CONTEXT_PROJECTION_VERSION,
        )
        == wider_snapshot
    )


@pytest.mark.asyncio
async def test_missing_authority_or_missing_source_block_fails_explicitly(
    tmp_path: Path,
) -> None:
    _write_authorities(tmp_path)
    (tmp_path / "USER.md").unlink()

    async def author(_digest: str, _sources: tuple[Any, ...]) -> SubjectContextDraft:
        return SubjectContextDraft(
            text=(
                '<subject-source path="SOUL.md">\nsoul\n</subject-source>\n'
                '<subject-source path="MEMORY.md">\nmemory\n</subject-source>'
            ),
            generator="malformed-test",
        )

    projection = SubjectContextProjection(
        str(tmp_path),
        projection_profile="voice_live",
        max_bytes=8192,
        author=author,
    )
    assert await projection.ensure_current_snapshot() is None
    assert "USER.md" in projection.health_snapshot()["degraded_reason"]

    (tmp_path / "USER.md").write_text("user", encoding="utf-8")
    assert await projection.ensure_current_snapshot() is None
    assert (
        "exactly one ordered block" in projection.health_snapshot()["degraded_reason"]
    )


@pytest.mark.asyncio
async def test_service_api_pins_revision_and_never_switches_historical_snapshot(
    tmp_path: Path,
) -> None:
    _write_authorities(tmp_path)
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = LifeEngineService(SimpleNamespace(config=config))

    async def author(
        _digest: str,
        sources: tuple[Any, ...],
        **kwargs: Any,
    ) -> SubjectContextDraft:
        assert kwargs["projection_kind"] == "voice_live"
        assert kwargs["max_bytes"] == 8192
        return _draft_from_sources(sources)

    service._author_subject_context_projection = author  # type: ignore[method-assign]

    first = await service.get_subject_context_projection_snapshot(
        projection_kind="voice_live",
        max_bytes=8192,
    )
    (tmp_path / "MEMORY.md").write_text("memory-v2", encoding="utf-8")
    second = await service.get_subject_context_projection_snapshot(
        projection_kind="voice_live",
        max_bytes=8192,
    )
    pinned = await service.get_subject_context_projection_snapshot(
        projection_kind="voice_live",
        max_bytes=8192,
        source_digest=str(first["source_digest"]),
        projection_version=int(first["projection_version"]),
    )

    assert second["source_digest"] != first["source_digest"]
    assert pinned["projection_sha256"] == first["projection_sha256"]
    assert pinned["text"] == first["text"]

    with pytest.raises(ValueError, match="projection_kind"):
        await service.get_subject_context_projection_snapshot(
            projection_kind="../voice",
            max_bytes=8192,
        )
    with pytest.raises(ValueError, match="max_bytes"):
        await service.get_subject_context_projection_snapshot(
            projection_kind="voice_live",
            max_bytes=1024,
        )
    with pytest.raises(ValueError, match="requires a historical source_digest"):
        await service.get_subject_context_projection_snapshot(
            projection_kind="voice_live",
            max_bytes=8192,
            projection_version=SUBJECT_CONTEXT_PROJECTION_VERSION,
        )
    with pytest.raises(ValueError, match="source_digest"):
        await service.get_subject_context_projection_snapshot(
            projection_kind="voice_live",
            max_bytes=8192,
            source_digest="../outside",
        )
    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        await service.get_subject_context_projection_snapshot(
            projection_kind="voice_live",
            max_bytes=12288,
            source_digest=str(first["source_digest"]),
            projection_version=int(first["projection_version"]),
        )


@pytest.mark.asyncio
async def test_corrupt_pinned_content_fails_without_overwriting_version(
    tmp_path: Path,
) -> None:
    _write_authorities(tmp_path)
    author_calls = 0

    async def author(_digest: str, sources: tuple[Any, ...]) -> SubjectContextDraft:
        nonlocal author_calls
        author_calls += 1
        return _draft_from_sources(sources)

    projection = SubjectContextProjection(
        str(tmp_path),
        projection_profile="voice_live",
        max_bytes=8192,
        author=author,
    )
    snapshot = await projection.ensure_current_snapshot()
    assert snapshot is not None
    revision = str(snapshot["source_digest"])
    version_path = projection.versions_dir / f"v1-{revision}.md"
    corrupted = version_path.read_text(encoding="utf-8") + "tampered\n"
    version_path.write_text(corrupted, encoding="utf-8")

    assert await projection.get_snapshot(revision) is None
    assert await projection.ensure_current_snapshot() is None
    assert version_path.read_text(encoding="utf-8") == corrupted
    assert author_calls == 2
    assert (
        "immutable router projection conflict"
        in projection.health_snapshot()["degraded_reason"]
    )


@pytest.mark.asyncio
async def test_missing_or_corrupt_manifest_never_gets_silently_reconstructed(
    tmp_path: Path,
) -> None:
    _write_authorities(tmp_path)
    author_calls = 0

    async def author(_digest: str, sources: tuple[Any, ...]) -> SubjectContextDraft:
        nonlocal author_calls
        author_calls += 1
        return _draft_from_sources(sources)

    projection = SubjectContextProjection(
        str(tmp_path),
        projection_profile="voice_live",
        max_bytes=8192,
        author=author,
    )
    snapshot = await projection.ensure_current_snapshot()
    assert snapshot is not None
    revision = str(snapshot["source_digest"])
    manifest_path = projection.versions_dir / f"v1-{revision}.json"
    original_manifest = manifest_path.read_text(encoding="utf-8")
    manifest_path.unlink()

    assert await projection.get_snapshot(revision) is None
    assert await projection.ensure_current_snapshot() is None
    assert not manifest_path.exists()
    assert author_calls == 1
    assert "manifest is missing" in projection.health_snapshot()["degraded_reason"]

    manifest_path.write_text(original_manifest, encoding="utf-8")
    manifest = json.loads(original_manifest)
    manifest["budget"]["sources"]["USER.md"]["delivered_bytes"] += 1
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert await projection.get_snapshot(revision) is None
    assert await projection.ensure_current_snapshot() is None
    assert author_calls == 1
    assert "budget metadata mismatch" in projection.health_snapshot()["degraded_reason"]

    manifest = json.loads(original_manifest)
    manifest["text"] = "private projection content must never enter the manifest"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert await projection.get_snapshot(revision) is None
    assert await projection.ensure_current_snapshot() is None
    assert author_calls == 1
    assert (
        "must not contain projection text"
        in projection.health_snapshot()["degraded_reason"]
    )


@pytest.mark.asyncio
async def test_subject_author_tries_next_model_after_per_source_budget_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_authorities(tmp_path)
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    config.chatter.subject_context_projection_task_name = "subject-test"
    service = LifeEngineService(SimpleNamespace(config=config))

    async def unused_author(
        _digest: str,
        _sources: tuple[Any, ...],
    ) -> SubjectContextDraft:
        raise AssertionError("reader author must not run")

    reader = SubjectContextProjection(
        str(tmp_path),
        projection_profile="voice_live",
        max_bytes=8192,
        author=unused_author,
    )
    sources, source_digest = reader._read_sources()
    oversized = (
        '<subject-source path="SOUL.md">\n' + ("世" * 800) + "\n</subject-source>\n"
        '<subject-source path="USER.md">\nuser\n</subject-source>\n'
        '<subject-source path="MEMORY.md">\nmemory\n</subject-source>'
    )
    valid = _draft_from_sources(sources).text
    requested_models: list[str] = []

    class _ProjectionRequest:
        def __init__(self, model_identifier: str) -> None:
            self.model_identifier = model_identifier

        def add_payload(self, _payload: object) -> None:
            return

        async def send(self, *, stream: bool = False) -> _Response:
            assert stream is False
            requested_models.append(self.model_identifier)
            return _Response(
                oversized if self.model_identifier == "oversized" else valid
            )

    monkeypatch.setattr(
        service_core,
        "get_model_set_by_task",
        lambda _task: [
            {"model_identifier": "oversized"},
            {"model_identifier": "valid-second"},
        ],
    )
    monkeypatch.setattr(
        service_core,
        "create_llm_request",
        lambda model_set, request_name: _ProjectionRequest(
            model_set[0]["model_identifier"]
        ),
    )

    draft = await service._author_subject_context_projection(
        source_digest,
        sources,
        projection_kind="voice_live",
        max_chars=reader.max_chars,
        max_bytes=reader.max_bytes,
    )

    assert draft.text == valid
    assert draft.generator.endswith("model:valid-second")
    assert requested_models == ["oversized", "valid-second"]
