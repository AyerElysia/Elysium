"""Selected-store path identities must survive the continuity-review boundary.

Fixtures are entirely in memory. Public snapshot keys are filenames; immutable
head/version records retain the canonical selected-store namespace.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any, cast

import pytest

from plugins.life_engine.memory.continuity_session import (
    ContinuityReviewActorContext,
    ContinuityReviewSession,
    ContinuityReviewStale,
)
from plugins.life_engine.storage.subject_contracts import (
    SUBJECT_AUTHORITY_PATHS,
    SubjectAuthorityPort,
    SubjectAuthoritySnapshot,
    SubjectDocumentCommit,
    SubjectDocumentHead,
    SubjectDocumentPath,
    SubjectDocumentVersion,
    subject_authority_logical_path,
    subject_revision_from_contents,
)


def _selected_snapshot() -> SubjectAuthoritySnapshot:
    contents: dict[SubjectDocumentPath, bytes] = {
        "SOUL.md": b"# SOUL\nsynthetic identity fixture\n",
        "USER.md": b"# USER\nsynthetic relationship fixture\n",
        "MEMORY.md": "# MEMORY\n隔离测试原文。\n".encode(),
    }
    commits: dict[SubjectDocumentPath, SubjectDocumentCommit] = {}
    for path in SUBJECT_AUTHORITY_PATHS:
        raw = contents[path]
        logical_path = subject_authority_logical_path(path)
        version = SubjectDocumentVersion(
            version_id=f"selected-version:{path}",
            document_id=f"selected-document:{path}",
            logical_path=logical_path,
            parent_version_id="",
            occurrence_id=f"synthetic-seed:{path}",
            semantic_actor_id="test-active-actor",
            semantic_source_id=f"synthetic-source:{path}",
            occurred_at="2026-09-07T00:00:00+00:00",
            recorded_by="isolated-regression-test",
            recorded_source="in-memory-selected-snapshot",
            recorded_at="2026-09-07T00:00:00+00:00",
            provenance_status="complete",
            content_bytes=raw,
            content_hash=hashlib.sha256(raw).hexdigest(),
            byte_length=len(raw),
            byte_fidelity="exact_bytes",
            encoding="utf-8",
            newline_style="lf",
            change_context={},
        )
        head = SubjectDocumentHead(
            document_id=version.document_id,
            logical_path=logical_path,
            declared_owner="elysia",
            current_version_id=version.version_id,
            revision=1,
        )
        commits[path] = SubjectDocumentCommit(version=version, head=head)
    return SubjectAuthoritySnapshot(
        commits=commits,
        revision=subject_revision_from_contents(contents),
        change_marker="synthetic-selected-head-marker",
    )


class _ReadOnlyAuthority:
    def __init__(self, snapshot: SubjectAuthoritySnapshot) -> None:
        self.snapshot = snapshot
        self.read_calls = 0

    async def read_subject_authority(self) -> SubjectAuthoritySnapshot:
        self.read_calls += 1
        return self.snapshot

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"open/read_source must not use authority {name}")


class _UnusedWritePort:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"open/read_source must not use write port {name}")


async def _active(actor_id: str) -> bool:
    return actor_id == "test-active-actor"


def _actor() -> ContinuityReviewActorContext:
    return ContinuityReviewActorContext(
        consciousness_instance_id="test-active-actor",
        stream_scope="isolated:selected-path-regression",
        source_occurrence_id="synthetic-message:open",
        action_occurrence_id="synthetic-tool-call:open",
        occurred_at="2026-09-07T00:00:00+00:00",
    )


def _session(authority: _ReadOnlyAuthority) -> ContinuityReviewSession:
    return ContinuityReviewSession(
        subject_authority=cast(SubjectAuthorityPort, authority),
        boundary_repository=cast(Any, _UnusedWritePort()),
        candidate_ledger=cast(Any, _UnusedWritePort()),
        validate_active_actor=_active,
    )


@pytest.mark.asyncio
async def test_open_and_read_source_preserve_selected_store_identity() -> None:
    snapshot = _selected_snapshot()
    authority = _ReadOnlyAuthority(snapshot)
    session = _session(authority)
    memory = snapshot.commits["MEMORY.md"].version

    opened = await session.open(_actor())

    assert set(snapshot.commits) == set(SUBJECT_AUTHORITY_PATHS)
    assert opened.subject_revision == snapshot.revision
    assert opened.memory_version_id == memory.version_id
    assert opened.memory_sha256 == memory.content_hash
    assert opened.memory_bytes == memory.byte_length
    assert opened.page.text.encode() == memory.content_bytes
    assert opened.as_dict()["authority_written"] is False
    page = await session.read_source(
        _actor(),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        offset=0,
        max_bytes=opened.memory_bytes,
    )
    assert page.text.encode() == memory.content_bytes
    assert page.page_sha256 == memory.content_hash
    assert authority.read_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("path", SUBJECT_AUTHORITY_PATHS)
@pytest.mark.parametrize("record", ("head", "version"))
@pytest.mark.parametrize("path_shape", ("bare", "other_prefix", "nested"))
async def test_open_rejects_noncanonical_same_basename_record(
    path: SubjectDocumentPath,
    record: str,
    path_shape: str,
) -> None:
    snapshot = _selected_snapshot()
    original = snapshot.commits[path]
    noncanonical = {
        "bare": path,
        "other_prefix": f"other_workspace/{path}",
        "nested": f"life_engine_workspace/nested/{path}",
    }[path_shape]
    if record == "head":
        changed = replace(
            original,
            head=replace(original.head, logical_path=noncanonical),
        )
    else:
        changed = replace(
            original,
            version=replace(original.version, logical_path=noncanonical),
        )
    commits = dict(snapshot.commits)
    commits[path] = changed
    authority = _ReadOnlyAuthority(replace(snapshot, commits=commits))

    with pytest.raises(
        ContinuityReviewStale,
        match=re.escape(f"SubjectAuthoritySnapshotEvidenceMismatch:{path}"),
    ):
        await _session(authority).open(_actor())

    assert authority.read_calls == 1
