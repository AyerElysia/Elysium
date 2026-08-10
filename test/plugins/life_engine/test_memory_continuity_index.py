"""Contracts for the pure, rebuildable ``MEMORY.md`` continuity index."""

from __future__ import annotations

import hashlib

import pytest

from plugins.life_engine.memory.continuity_index import (
    CONTINUITY_MEMORY_INDEX_AUTHORITY,
    CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES,
    CONTINUITY_MEMORY_SOFT_TARGET_BYTES,
    ContinuityMemoryIndexError,
    DuplicateContinuityMemoryEntry,
    MalformedContinuityMemoryReference,
    build_continuity_memory_index_health,
    diagnose_continuity_memory_index,
    diff_continuity_memory_indexes,
    parse_continuity_memory_index,
)

SUBJECT_REVISION_A = "e" * 64
SUBJECT_REVISION_B = "f" * 64
ROOT_A = "a" * 64
ROOT_B = "b" * 64
ROOT_C = "c" * 64


def _link(
    anchor: str,
    *,
    entry_id: str = "memory-entry-one",
    artifact_version_id: str = "artifact-version-one",
    root_sha256: str = ROOT_A,
) -> str:
    return (
        f"[{anchor}]"
        f"(memory://boundary/{entry_id}@{artifact_version_id}"
        f"#sha256={root_sha256})"
    )


def _parse(
    text: str,
    *,
    version_id: str = "subject-memory-version-one",
    revision: str = SUBJECT_REVISION_A,
):
    return parse_continuity_memory_index(
        text.encode("utf-8"),
        subject_document_version_id=version_id,
        unified_subject_revision=revision,
    )


def test_plain_prose_and_unrelated_markdown_are_never_inferred() -> None:
    text = (
        "# 长期记忆\n\n"
        "我记得一段很长的共同经历，但普通叙述不是机器索引。\n"
        "[普通资料](https://example.test/memory-boundary)\n"
        "内联标识 memory://other/not-a-boundary 也属于其他协议。\n"
    )

    index = _parse(text)

    assert index.entries == ()
    assert index.source_document_byte_length == len(text.encode("utf-8"))
    assert (
        index.source_document_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    )


def test_parse_is_stable_and_records_exact_utf8_link_offsets() -> None:
    link = _link(
        "那一天的完整记忆🌸",
        entry_id="shared-day",
        artifact_version_id="memory-artifact:v3",
        root_sha256=ROOT_B,
    )
    text = f"前言：爱莉希雅\n- {link}\n尾声"
    exact_bytes = text.encode("utf-8")

    first = parse_continuity_memory_index(
        exact_bytes,
        subject_document_version_id="subject-memory-version-42",
        unified_subject_revision=SUBJECT_REVISION_A,
    )
    replay = parse_continuity_memory_index(
        exact_bytes,
        subject_document_version_id="subject-memory-version-42",
        unified_subject_revision=SUBJECT_REVISION_A,
    )

    assert replay == first
    assert len(first.entries) == 1
    entry = first.entries[0]
    character_start = text.index(link)
    expected_start = len(text[:character_start].encode("utf-8"))
    expected_end = expected_start + len(link.encode("utf-8"))
    assert entry.anchor_text == "那一天的完整记忆🌸"
    assert entry.entry_id == "shared-day"
    assert entry.boundary_id == "shared-day"
    assert entry.artifact_version_id == "memory-artifact:v3"
    assert entry.artifact_id == "memory-artifact:v3"
    assert entry.boundary_root_sha256 == ROOT_B
    assert entry.root_sha256 == ROOT_B
    assert entry.byte_start == expected_start
    assert entry.byte_end == expected_end
    assert exact_bytes[entry.byte_start : entry.byte_end] == link.encode("utf-8")
    assert entry.entry_sha256 == hashlib.sha256(link.encode("utf-8")).hexdigest()
    assert entry.subject_document_version_id == "subject-memory-version-42"
    assert entry.unified_subject_revision == SUBJECT_REVISION_A


@pytest.mark.parametrize(
    "text",
    [
        (f"memory://boundary/bare@artifact-version-one#sha256={ROOT_A}"),
        (f"![不是索引](memory://boundary/image@artifact-version-one#sha256={ROOT_A})"),
        (f"\\[被转义](memory://boundary/escaped@artifact-version-one#sha256={ROOT_A})"),
        (f"[大小写篡改](Memory://boundary/case@artifact-version-one#sha256={ROOT_A})"),
        (
            "[路径逃逸](memory://boundary/../escape@artifact-version-one"
            f"#sha256={ROOT_A})"
        ),
        (f"[短哈希](memory://boundary/short@artifact-version-one#sha256={'a' * 63})"),
        (
            "[非法哈希](memory://boundary/bad-hash@artifact-version-one"
            f"#sha256={'g' * 64})"
        ),
        (
            "[附加查询](memory://boundary/query@artifact-version-one"
            f"#sha256={ROOT_A}?latest=true)"
        ),
        (
            "[百分号编码](memory://boundary/%2e%2e@artifact-version-one"
            f"#sha256={ROOT_A})"
        ),
        (f"[](memory://boundary/empty-anchor@artifact-version-one#sha256={ROOT_A})"),
    ],
)
def test_malformed_or_non_markdown_boundary_attempts_fail_explicitly(
    text: str,
) -> None:
    with pytest.raises(
        MalformedContinuityMemoryReference,
        match="malformed memory boundary reference",
    ):
        _parse(text)


def test_diagnostics_keep_valid_links_while_exposing_content_free_repair_evidence() -> (
    None
):
    valid = _link("仍然有效", entry_id="valid-entry")
    malformed = "[待修复](memory://boundary/broken@artifact-version-one#sha256=short)"
    exact = f"{valid}\n{malformed}\n".encode()

    diagnostics = diagnose_continuity_memory_index(
        exact,
        subject_document_version_id="subject-version",
        unified_subject_revision=SUBJECT_REVISION_A,
    )

    assert [item.entry_id for item in diagnostics.index.entries] == ["valid-entry"]
    assert len(diagnostics.issues) == 1
    assert diagnostics.issues[0].error_type == "invalid_boundary_uri"
    assert diagnostics.issues[0].attempted_reference_sha256
    assert "待修复" not in repr(diagnostics.issues[0])
    assert diagnostics.issues_sha256


def test_duplicate_entry_identity_fails_even_when_target_differs() -> None:
    text = "\n".join(
        (
            _link(
                "第一次出现",
                entry_id="same-entry",
                artifact_version_id="artifact-version-one",
                root_sha256=ROOT_A,
            ),
            _link(
                "第二次出现",
                entry_id="same-entry",
                artifact_version_id="artifact-version-two",
                root_sha256=ROOT_B,
            ),
        )
    )

    with pytest.raises(
        DuplicateContinuityMemoryEntry,
        match="same-entry",
    ):
        _parse(text)


def test_exact_bytes_and_source_identities_fail_closed() -> None:
    with pytest.raises(TypeError, match="immutable bytes"):
        parse_continuity_memory_index(  # type: ignore[arg-type]
            "not exact bytes",
            subject_document_version_id="subject-version",
            unified_subject_revision=SUBJECT_REVISION_A,
        )
    with pytest.raises(
        MalformedContinuityMemoryReference,
        match="not valid UTF-8",
    ):
        parse_continuity_memory_index(
            b"\xff",
            subject_document_version_id="subject-version",
            unified_subject_revision=SUBJECT_REVISION_A,
        )
    with pytest.raises(ContinuityMemoryIndexError, match="canonical identity"):
        parse_continuity_memory_index(
            b"",
            subject_document_version_id=" subject-version ",
            unified_subject_revision=SUBJECT_REVISION_A,
        )
    with pytest.raises(ContinuityMemoryIndexError, match="SHA-256"):
        parse_continuity_memory_index(
            b"",
            subject_document_version_id="subject-version",
            unified_subject_revision="A" * 64,
        )


def _sized_document(size: int) -> str:
    link = _link("预算边界")
    padding = size - len(link.encode("utf-8"))
    assert padding >= 0
    return link + ("x" * padding)


def test_health_is_content_free_and_size_is_only_review_pressure() -> None:
    at_soft_target = build_continuity_memory_index_health(
        _parse(_sized_document(CONTINUITY_MEMORY_SOFT_TARGET_BYTES))
    )
    above_soft_target = build_continuity_memory_index_health(
        _parse(_sized_document(CONTINUITY_MEMORY_SOFT_TARGET_BYTES + 1))
    )
    at_review_pressure = build_continuity_memory_index_health(
        _parse(_sized_document(CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES))
    )

    assert at_soft_target.source_bytes == CONTINUITY_MEMORY_SOFT_TARGET_BYTES
    assert at_soft_target.entry_count == 1
    assert at_soft_target.broken_reference_count == 0
    assert at_soft_target.soft_target_exceeded is False
    assert at_soft_target.review_pressure_reached is False
    assert above_soft_target.soft_target_exceeded is True
    assert above_soft_target.review_pressure_reached is False
    assert at_review_pressure.review_pressure_reached is True
    assert at_review_pressure.authority == CONTINUITY_MEMORY_INDEX_AUTHORITY
    assert at_review_pressure.pressure_semantics == "engineering_review_only"
    assert at_review_pressure.automatic_deletion_recommended is False

    snapshot = at_review_pressure.as_dict()
    assert snapshot["bytes"] == CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES
    assert snapshot["count"] == 1
    assert snapshot["broken"] == 0
    assert "预算边界" not in repr(snapshot)
    assert "memory-entry-one" not in repr(snapshot)
    assert ROOT_A not in repr(snapshot)


def test_lifecycle_diff_is_technical_and_never_deletes_a_bundle() -> None:
    previous_text = "\n".join(
        (
            _link(
                "稍后离开当前索引",
                entry_id="deactivated-entry",
                artifact_version_id="artifact-old",
                root_sha256=ROOT_A,
            ),
            _link(
                "锚点旧文字",
                entry_id="stable-target",
                artifact_version_id="artifact-stable",
                root_sha256=ROOT_B,
            ),
            _link(
                "将更新边界",
                entry_id="retargeted-entry",
                artifact_version_id="artifact-v1",
                root_sha256=ROOT_A,
            ),
        )
    )
    current_text = "\n".join(
        (
            _link(
                "锚点的新文字不会被误报为换目标",
                entry_id="stable-target",
                artifact_version_id="artifact-stable",
                root_sha256=ROOT_B,
            ),
            _link(
                "更新后的边界",
                entry_id="retargeted-entry",
                artifact_version_id="artifact-v2",
                root_sha256=ROOT_C,
            ),
            _link(
                "刚进入当前索引",
                entry_id="activated-entry",
                artifact_version_id="artifact-new",
                root_sha256=ROOT_C,
            ),
        )
    )
    previous = _parse(
        previous_text,
        version_id="subject-memory-v1",
        revision=SUBJECT_REVISION_A,
    )
    current = _parse(
        current_text,
        version_id="subject-memory-v2",
        revision=SUBJECT_REVISION_B,
    )

    lifecycle = diff_continuity_memory_indexes(previous, current)

    assert lifecycle.activated == ("activated-entry",)
    assert lifecycle.deactivated == ("deactivated-entry",)
    assert lifecycle.rewritten == ("stable-target",)
    assert len(lifecycle.retargeted) == 1
    retargeting = lifecycle.retargeted[0]
    assert retargeting.entry_id == "retargeted-entry"
    assert retargeting.previous_artifact_version_id == "artifact-v1"
    assert retargeting.current_artifact_version_id == "artifact-v2"
    assert retargeting.previous_root_sha256 == ROOT_A
    assert retargeting.current_root_sha256 == ROOT_C
    assert lifecycle.previous_subject_document_version_id == "subject-memory-v1"
    assert lifecycle.current_subject_document_version_id == "subject-memory-v2"
    assert lifecycle.previous_unified_subject_revision == SUBJECT_REVISION_A
    assert lifecycle.current_unified_subject_revision == SUBJECT_REVISION_B

    # A deactivation is only absence in the new projection.  The earlier exact
    # projection and its immutable target reference remain untouched.
    previous_entry = previous.entries[0]
    assert previous_entry.entry_id == "deactivated-entry"
    assert previous_entry.artifact_version_id == "artifact-old"
    assert previous_entry.boundary_root_sha256 == ROOT_A


def test_valid_root_change_is_traceable_and_invalid_hash_tamper_fails() -> None:
    first = _parse(
        _link(
            "同一个索引",
            entry_id="traceable-entry",
            artifact_version_id="artifact-v1",
            root_sha256=ROOT_A,
        ),
        version_id="subject-memory-v1",
        revision=SUBJECT_REVISION_A,
    )
    second = _parse(
        _link(
            "同一个索引",
            entry_id="traceable-entry",
            artifact_version_id="artifact-v1",
            root_sha256=ROOT_B,
        ),
        version_id="subject-memory-v2",
        revision=SUBJECT_REVISION_B,
    )

    lifecycle = diff_continuity_memory_indexes(first, second)
    assert len(lifecycle.retargeted) == 1
    assert first.entries[0].entry_sha256 != second.entries[0].entry_sha256
    assert first.entries[0].boundary_root_sha256 == ROOT_A
    assert second.entries[0].boundary_root_sha256 == ROOT_B

    malformed = _link(
        "同一个索引",
        entry_id="traceable-entry",
        artifact_version_id="artifact-v1",
        root_sha256=("b" * 63) + "!",
    )
    with pytest.raises(MalformedContinuityMemoryReference):
        _parse(malformed)
