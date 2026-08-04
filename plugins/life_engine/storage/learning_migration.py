"""Explicit loss-aware import/export for the legacy ``.life_learning`` domain."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.kernel.storage import canonical_json

from ..learning.models import Insight, KnowledgeVersion, ValidationExperiment
from ..learning.reflection_queue import (
    REFLECTION_QUEUE_STATE_KEY,
    load_reflection_jobs,
)
from ..learning.skill_store import SkillCandidate, SkillPattern
from .learning_contracts import (
    LearningEventDraft,
    LearningProjection,
    LearningProjectionConflict,
    LearningProjectionWrite,
    LearningStorePort,
)

_INSIGHT_PROJECTION = "learning_insights"
_SKILL_PROJECTION = "learning_skills"
_PROJECTOR_VERSION = "learning-state-compat-v1"
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
_SNAPSHOT_CHUNK_BYTES = 1024 * 1024
_MAX_AUDIT_EVENT_BYTES = 1024 * 1024
_IMPORT_EVENT_BATCH_SIZE = 8


@dataclass(frozen=True, slots=True)
class LearningLegacyMigrationReport:
    """Content-free evidence for one domain import/export."""

    snapshot_sha256: str
    file_count: int
    total_bytes: int
    event_count: int
    projection_revisions: dict[str, int]
    file_hashes: dict[str, str]


def _json_file(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"legacy learning JSON must be an object: {path.name}")
    return dict(value)


def _collect_files(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    if not root.is_dir():
        raise NotADirectoryError(root)
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"legacy learning snapshot contains symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise ValueError(f"legacy learning file exceeds limit: {relative}")
        total += size
        if total > _MAX_SNAPSHOT_BYTES:
            raise ValueError("legacy learning snapshot exceeds bounded import size")
        files[relative] = path.read_bytes()
    return files


def _snapshot_identity(files: dict[str, bytes]) -> tuple[str, dict[str, str]]:
    hashes = {
        path: hashlib.sha256(content).hexdigest()
        for path, content in sorted(files.items())
    }
    material = [
        {"path": path, "size": len(files[path]), "sha256": digest}
        for path, digest in sorted(hashes.items())
    ]
    return hashlib.sha256(canonical_json(material).encode()).hexdigest(), hashes


def _legacy_projection_payloads(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    insights = _json_file(
        root / "insights.json",
        default={"version": 1, "insights": []},
    )
    insight_rows = insights.get("insights", [])
    if not isinstance(insight_rows, list) or any(
        not isinstance(row, dict) for row in insight_rows
    ):
        raise TypeError("legacy insights must be an object list")
    parsed_insights: list[dict[str, Any]] = []
    unreadable_insights: list[dict[str, Any]] = []
    for row in insight_rows:
        try:
            parsed_insights.append(Insight.from_dict(row).to_dict())
        except Exception:  # noqa: BLE001 - preserve unknown rows outside runtime state
            unreadable_insights.append(dict(row))
    experiments = _json_file(
        root / "validation_experiments.json",
        default={"pending": [], "completed": []},
    )
    pending = experiments.get("pending", [])
    completed = experiments.get("completed", [])
    if (
        not isinstance(pending, list)
        or not isinstance(completed, list)
        or any(not isinstance(row, dict) for row in [*pending, *completed])
    ):
        raise TypeError("legacy validation experiments must be object lists")
    parsed_pending = [ValidationExperiment.from_dict(row).to_dict() for row in pending]
    parsed_completed = [
        ValidationExperiment.from_dict(row).to_dict() for row in completed
    ]
    manifest = _json_file(
        root / "knowledge" / "manifest.json",
        default={"versions": [], "current_version": 0},
    )
    manifest_versions = manifest.get("versions", [])
    if not isinstance(manifest_versions, list) or any(
        not isinstance(row, dict) for row in manifest_versions
    ):
        raise TypeError("legacy knowledge manifest versions must be object lists")
    normalized_versions = [
        KnowledgeVersion.from_dict(row).to_dict() for row in manifest_versions
    ]
    if any(int(row["version"]) <= 0 for row in normalized_versions):
        raise ValueError("legacy knowledge versions must be positive")
    manifest = {**manifest, "versions": normalized_versions}
    state = _json_file(root / "state.json", default={})
    version_content: dict[str, str] = {}
    knowledge_dir = root / "knowledge"
    if knowledge_dir.exists():
        for path in sorted(knowledge_dir.glob("v*.md")):
            suffix = path.stem[1:]
            if suffix.isdigit():
                version_content[str(int(suffix))] = path.read_text(encoding="utf-8")
    current_version = int(manifest.get("current_version", 0) or 0)
    current_path = knowledge_dir / "self_knowledge.md"
    if current_version > 0 and current_path.exists():
        current_content = current_path.read_text(encoding="utf-8")
        existing = version_content.get(str(current_version))
        if existing is not None and existing != current_content:
            raise ValueError("legacy current knowledge differs from its version file")
        version_content[str(current_version)] = current_content
    if current_version > 0 and str(current_version) not in version_content:
        raise ValueError("legacy current knowledge version content is missing")

    skills = _json_file(
        root / "skills.json",
        default={"version": 1, "skills": []},
    )
    skill_rows = skills.get("skills", [])
    candidate_rows = skills.get("candidates", [])
    if not isinstance(skill_rows, list) or any(
        not isinstance(row, dict) for row in skill_rows
    ):
        raise TypeError("legacy skills must be an object list")
    if not isinstance(candidate_rows, list) or any(
        not isinstance(row, dict) for row in candidate_rows
    ):
        raise TypeError("legacy skill candidates must be an object list")
    parsed_skills: list[dict[str, Any]] = []
    unreadable_skills: list[dict[str, Any]] = []
    for row in skill_rows:
        try:
            parsed_skills.append(SkillPattern.from_dict(row).to_dict())
        except Exception:  # noqa: BLE001 - preserve unknown rows outside runtime state
            unreadable_skills.append(dict(row))
    parsed_candidates: list[dict[str, Any]] = []
    unreadable_candidates: list[dict[str, Any]] = []
    for row in candidate_rows:
        try:
            parsed_candidates.append(SkillCandidate.from_dict(row).to_dict())
        except Exception:  # noqa: BLE001 - preserve unknown rows outside runtime state
            unreadable_candidates.append(dict(row))
    if REFLECTION_QUEUE_STATE_KEY in state:
        load_reflection_jobs(state)
    return (
        {
            "version": int(insights.get("version", 1) or 1),
            "insights": parsed_insights,
            "unreadable_rows": unreadable_insights,
            "experiments": {
                "pending": parsed_pending,
                "completed": parsed_completed,
            },
            "knowledge_manifest": manifest,
            "knowledge_versions_content": version_content,
            "state": state,
        },
        {
            "version": int(skills.get("version", 1) or 1),
            "skills": parsed_skills,
            "candidates": parsed_candidates,
            "unreadable_rows": unreadable_skills,
            "unreadable_candidate_rows": unreadable_candidates,
        },
    )


def _audit_events(
    root: Path,
    *,
    subject_revision: str,
) -> list[LearningEventDraft]:
    events: list[LearningEventDraft] = []
    for relative, domain in (
        ("insights_audit.jsonl", _INSIGHT_PROJECTION),
        ("skills_audit.jsonl", _SKILL_PROJECTION),
        ("maintenance_runs.jsonl", "maintenance"),
    ):
        path = root / relative
        if not path.exists():
            continue
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            encoded_line = line.encode("utf-8")
            line_hash = hashlib.sha256(encoded_line).hexdigest()
            if len(encoded_line) > _MAX_AUDIT_EVENT_BYTES:
                value = {
                    "legacy_line_sha256": line_hash,
                    "legacy_line_bytes": len(encoded_line),
                    "semantic_payload_omitted": True,
                    "exact_bytes_location": "legacy.snapshot.file_chunk",
                }
            else:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    value = {
                        "raw_line_base64": base64.b64encode(encoded_line).decode()
                    }
                if not isinstance(value, dict):
                    value = {"legacy_value": value}
            timestamp = str(value.get("timestamp") or value.get("started_at") or "")
            timestamp_status = "source"
            try:
                datetime.fromisoformat(timestamp)
            except ValueError:
                timestamp = "1970-01-01T00:00:00+00:00"
                timestamp_status = "missing_or_invalid_source"
            action = str(value.get("action") or value.get("outcome") or "legacy_event")
            event_kind = f"{domain}.{action}"
            if len(event_kind) > 128:
                event_kind = (
                    f"{domain}.oversized_action."
                    f"{hashlib.sha256(action.encode('utf-8')).hexdigest()[:16]}"
                )
            events.append(
                LearningEventDraft(
                    occurrence_id=(f"legacy_learning:{relative}:{index}:{line_hash}"),
                    event_kind=event_kind,
                    occurred_at=timestamp,
                    source="legacy.learning.import",
                    actor_consciousness_instance_id="",
                    subject_revision=subject_revision,
                    provenance={
                        "legacy_path": relative,
                        "legacy_line": index + 1,
                        "occurred_at_status": timestamp_status,
                    },
                    payload=dict(value),
                )
            )
    return events


def _exact_snapshot_events(
    files: dict[str, bytes],
    *,
    snapshot_sha256: str,
    file_hashes: dict[str, str],
    subject_revision: str,
) -> list[LearningEventDraft]:
    """Split exact legacy bytes into bounded immutable migration events."""

    manifest = LearningEventDraft(
        occurrence_id=f"legacy_learning_snapshot:{snapshot_sha256}",
        event_kind="legacy.snapshot.manifested",
        occurred_at="1970-01-01T00:00:00+00:00",
        source="legacy.learning.import",
        actor_consciousness_instance_id="",
        subject_revision=subject_revision,
        provenance={
            "snapshot_sha256": snapshot_sha256,
            "file_count": len(files),
            "total_bytes": sum(len(content) for content in files.values()),
            "occurred_at_status": "deterministic_migration_identity",
        },
        payload={
            "files": {
                path: {
                    "sha256": file_hashes[path],
                    "size": len(content),
                    "chunk_count": max(
                        1,
                        (len(content) + _SNAPSHOT_CHUNK_BYTES - 1)
                        // _SNAPSHOT_CHUNK_BYTES,
                    ),
                }
                for path, content in sorted(files.items())
            }
        },
    )
    events = [manifest]
    for path, content in sorted(files.items()):
        path_digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
        chunks = [
            content[offset : offset + _SNAPSHOT_CHUNK_BYTES]
            for offset in range(0, len(content), _SNAPSHOT_CHUNK_BYTES)
        ] or [b""]
        for index, chunk in enumerate(chunks):
            chunk_sha256 = hashlib.sha256(chunk).hexdigest()
            events.append(
                LearningEventDraft(
                    occurrence_id=(
                        f"legacy_learning_chunk:{snapshot_sha256}:"
                        f"{path_digest}:{index}:{chunk_sha256}"
                    ),
                    event_kind="legacy.snapshot.file_chunk",
                    occurred_at="1970-01-01T00:00:00+00:00",
                    source="legacy.learning.import",
                    actor_consciousness_instance_id="",
                    subject_revision=subject_revision,
                    provenance={
                        "snapshot_sha256": snapshot_sha256,
                        "path": path,
                        "file_sha256": file_hashes[path],
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                        "occurred_at_status": "deterministic_migration_identity",
                    },
                    payload={
                        "chunk_sha256": chunk_sha256,
                        "size": len(chunk),
                        "content_base64": base64.b64encode(chunk).decode("ascii"),
                    },
                )
            )
    return events


async def import_legacy_learning_snapshot(
    workspace_path: str | Path,
    store: LearningStorePort,
    *,
    subject_revision: str = "",
) -> LearningLegacyMigrationReport:
    """Import one immutable legacy snapshot without activating or dual-writing it."""

    workspace = Path(workspace_path).resolve()
    root = workspace / ".life_learning"
    files = _collect_files(root)
    snapshot_sha256, file_hashes = _snapshot_identity(files)
    insight_payload, skill_payload = _legacy_projection_payloads(root)
    events = [
        *_exact_snapshot_events(
            files,
            snapshot_sha256=snapshot_sha256,
            file_hashes=file_hashes,
            subject_revision=subject_revision,
        ),
        *_audit_events(root, subject_revision=subject_revision),
    ]
    existing_insights, existing_skills = await asyncio_gather_projections(store)
    for projection, payload, name in (
        (existing_insights, insight_payload, _INSIGHT_PROJECTION),
        (existing_skills, skill_payload, _SKILL_PROJECTION),
    ):
        if projection is not None and projection.payload != payload:
            raise LearningProjectionConflict(
                f"legacy import refuses to overwrite selected projection: {name}"
            )
    writes = [
        LearningProjectionWrite(
            projection_name=name,
            expected_revision=projection.revision if projection else 0,
            expected_source_frontier=(projection.source_frontier if projection else 0),
            schema_version=1,
            projector_version=_PROJECTOR_VERSION,
            rebuild_state="ready",
            payload=payload,
        )
        for projection, payload, name in (
            (existing_insights, insight_payload, _INSIGHT_PROJECTION),
            (existing_skills, skill_payload, _SKILL_PROJECTION),
        )
        if projection is None
    ]
    event_count = 0
    for offset in range(0, len(events), _IMPORT_EVENT_BATCH_SIZE):
        batch = events[offset : offset + _IMPORT_EVENT_BATCH_SIZE]
        result = await store.commit(events=batch, projections=[])
        event_count += len(result.events)
    completed = LearningEventDraft(
        occurrence_id=f"legacy_learning_snapshot:{snapshot_sha256}:complete",
        event_kind="legacy.snapshot.imported",
        occurred_at="1970-01-01T00:00:00+00:00",
        source="legacy.learning.import",
        actor_consciousness_instance_id="",
        subject_revision=subject_revision,
        provenance={
            "snapshot_sha256": snapshot_sha256,
            "occurred_at_status": "deterministic_migration_identity",
        },
        payload={
            "file_count": len(files),
            "total_bytes": sum(len(content) for content in files.values()),
        },
    )
    result = await store.commit(events=[completed], projections=writes)
    event_count += len(result.events)
    projection_revisions = {
        projection.projection_name: projection.revision
        for projection in (existing_insights, existing_skills)
        if projection is not None
    }
    projection_revisions.update(
        {
            projection.projection_name: projection.revision
            for projection in result.projections
        }
    )
    return LearningLegacyMigrationReport(
        snapshot_sha256=snapshot_sha256,
        file_count=len(files),
        total_bytes=sum(len(content) for content in files.values()),
        event_count=event_count,
        projection_revisions=projection_revisions,
        file_hashes=file_hashes,
    )


async def asyncio_gather_projections(
    store: LearningStorePort,
) -> tuple[LearningProjection | None, LearningProjection | None]:
    """Keep projection reads together without exposing a mutable SQL session."""

    insights, skills = await asyncio.gather(
        store.get_projection(_INSIGHT_PROJECTION),
        store.get_projection(_SKILL_PROJECTION),
    )
    return insights, skills


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def export_learning_legacy_snapshot(
    store: LearningStorePort,
    target_directory: str | Path,
) -> LearningLegacyMigrationReport:
    """Export current projections to a new, non-authoritative legacy directory."""

    target = Path(target_directory).resolve()
    if target.exists():
        raise FileExistsError(target)
    insight_projection, skill_projection = await asyncio_gather_projections(store)
    if insight_projection is None or skill_projection is None:
        raise RuntimeError("both learning projections are required for export")
    for projection in (insight_projection, skill_projection):
        if projection.rebuild_state != "ready":
            raise RuntimeError(
                f"cannot export {projection.projection_name}: "
                f"{projection.rebuild_state}"
            )
    target.mkdir(parents=True)
    insight_payload = insight_projection.payload
    skill_payload = skill_projection.payload
    _write_json(
        target / "insights.json",
        {
            "version": int(insight_payload.get("version", 1) or 1),
            "updated_at": insight_projection.updated_at,
            "insights": [
                *list(insight_payload.get("insights", [])),
                *list(insight_payload.get("unreadable_rows", [])),
            ],
        },
    )
    _write_json(target / "state.json", dict(insight_payload.get("state", {})))
    _write_json(
        target / "validation_experiments.json",
        dict(insight_payload.get("experiments", {})),
    )
    manifest = dict(insight_payload.get("knowledge_manifest", {}))
    _write_json(target / "knowledge" / "manifest.json", manifest)
    version_content = insight_payload.get("knowledge_versions_content", {})
    if not isinstance(version_content, dict):
        raise TypeError("selected knowledge versions are malformed")
    for version, content in sorted(
        version_content.items(), key=lambda item: int(item[0])
    ):
        (target / "knowledge" / f"v{int(version)}.md").write_text(
            str(content),
            encoding="utf-8",
        )
    current_version = int(manifest.get("current_version", 0) or 0)
    if current_version > 0 and str(current_version) in version_content:
        (target / "knowledge" / "self_knowledge.md").write_text(
            str(version_content[str(current_version)]),
            encoding="utf-8",
        )
    _write_json(
        target / "skills.json",
        {
            "version": int(skill_payload.get("version", 1) or 1),
            "updated_at": skill_projection.updated_at,
            "skills": [
                *list(skill_payload.get("skills", [])),
                *list(skill_payload.get("unreadable_rows", [])),
            ],
            "candidates": [
                *list(skill_payload.get("candidates", [])),
                *list(skill_payload.get("unreadable_candidate_rows", [])),
            ],
        },
    )

    audit_lines: dict[str, list[str]] = {
        "insights_audit.jsonl": [],
        "skills_audit.jsonl": [],
        "maintenance_runs.jsonl": [],
    }
    position = 0
    event_count = 0
    while True:
        page = await store.read_events(position, limit=1000)
        if not page:
            break
        for event in page:
            event_count += 1
            if event.event_kind.startswith(
                _INSIGHT_PROJECTION + "."
            ) and not event.event_kind.endswith(".snapshot"):
                audit_lines["insights_audit.jsonl"].append(
                    json.dumps(event.payload, ensure_ascii=False)
                )
            elif event.event_kind.startswith(
                _SKILL_PROJECTION + "."
            ) and not event.event_kind.endswith(".snapshot"):
                audit_lines["skills_audit.jsonl"].append(
                    json.dumps(event.payload, ensure_ascii=False)
                )
            elif event.event_kind.startswith("maintenance."):
                audit_lines["maintenance_runs.jsonl"].append(
                    json.dumps(event.payload, ensure_ascii=False)
                )
        position = page[-1].position
    for relative, lines in audit_lines.items():
        if lines:
            (target / relative).write_text("\n".join(lines) + "\n", encoding="utf-8")

    files = _collect_files(target)
    snapshot_sha256, file_hashes = _snapshot_identity(files)
    return LearningLegacyMigrationReport(
        snapshot_sha256=snapshot_sha256,
        file_count=len(files),
        total_bytes=sum(len(content) for content in files.values()),
        event_count=event_count,
        projection_revisions={
            insight_projection.projection_name: insight_projection.revision,
            skill_projection.projection_name: skill_projection.revision,
        },
        file_hashes=file_hashes,
    )


async def verify_learning_legacy_export(
    store: LearningStorePort,
    exported_directory: str | Path,
) -> dict[str, Any]:
    """Verify a reverse export reconstructs both current semantic projections."""

    insight_projection, skill_projection = await asyncio_gather_projections(store)
    if insight_projection is None or skill_projection is None:
        raise RuntimeError("both learning projections are required for verification")
    exported_insights, exported_skills = _legacy_projection_payloads(
        Path(exported_directory).resolve()
    )
    source_hashes = {
        _INSIGHT_PROJECTION: hashlib.sha256(
            canonical_json(insight_projection.payload).encode("utf-8")
        ).hexdigest(),
        _SKILL_PROJECTION: hashlib.sha256(
            canonical_json(skill_projection.payload).encode("utf-8")
        ).hexdigest(),
    }
    reverse_hashes = {
        _INSIGHT_PROJECTION: hashlib.sha256(
            canonical_json(exported_insights).encode("utf-8")
        ).hexdigest(),
        _SKILL_PROJECTION: hashlib.sha256(
            canonical_json(exported_skills).encode("utf-8")
        ).hexdigest(),
    }
    return {
        "verified": source_hashes == reverse_hashes,
        "source_projection_sha256": source_hashes,
        "reverse_projection_sha256": reverse_hashes,
    }


async def verify_legacy_learning_import(
    workspace_path: str | Path,
    store: LearningStorePort,
) -> dict[str, Any]:
    """Prove selected projections and immutable chunks equal one legacy source."""

    root = Path(workspace_path).resolve() / ".life_learning"
    files = _collect_files(root)
    snapshot_sha256, file_hashes = _snapshot_identity(files)
    expected_insights, expected_skills = _legacy_projection_payloads(root)
    insight_projection, skill_projection = await asyncio_gather_projections(store)
    if insight_projection is None or skill_projection is None:
        raise RuntimeError("both learning projections are required for verification")
    projection_matches = all(
        (
            insight_projection.payload == expected_insights,
            skill_projection.payload == expected_skills,
        )
    )
    manifest_occurrence = f"legacy_learning_snapshot:{snapshot_sha256}"
    complete_occurrence = f"{manifest_occurrence}:complete"
    manifest_event, complete_event = await asyncio.gather(
        store.event_by_occurrence(manifest_occurrence),
        store.event_by_occurrence(complete_occurrence),
    )
    if (
        manifest_event is None
        or manifest_event.event_kind != "legacy.snapshot.manifested"
        or complete_event is None
        or complete_event.event_kind != "legacy.snapshot.imported"
    ):
        raise RuntimeError("legacy learning import completion evidence is missing")
    expected_manifest_files = {
        path: {
            "sha256": file_hashes[path],
            "size": len(content),
            "chunk_count": max(
                1,
                (len(content) + _SNAPSHOT_CHUNK_BYTES - 1)
                // _SNAPSHOT_CHUNK_BYTES,
            ),
        }
        for path, content in sorted(files.items())
    }
    if not all(
        (
            manifest_event.provenance.get("snapshot_sha256") == snapshot_sha256,
            manifest_event.payload.get("files") == expected_manifest_files,
            complete_event.provenance.get("snapshot_sha256") == snapshot_sha256,
            int(complete_event.payload.get("file_count", -1)) == len(files),
            int(complete_event.payload.get("total_bytes", -1))
            == sum(len(content) for content in files.values()),
        )
    ):
        raise RuntimeError("legacy learning import manifest evidence differs")

    chunks: dict[str, dict[int, bytes]] = {}
    chunk_counts: dict[str, int] = {}
    position = 0
    while True:
        page = await store.read_events(
            position,
            limit=1000,
            event_kinds=("legacy.snapshot.file_chunk",),
        )
        if not page:
            break
        for event in page:
            position = event.position
            if event.provenance.get("snapshot_sha256") != snapshot_sha256:
                continue
            path = str(event.provenance.get("path", ""))
            index = int(event.provenance.get("chunk_index", -1))
            count = int(event.provenance.get("chunk_count", 0))
            content = base64.b64decode(
                str(event.payload.get("content_base64", "")),
                validate=True,
            )
            if (
                not path
                or index < 0
                or count <= 0
                or event.provenance.get("file_sha256") != file_hashes.get(path)
                or len(content) != int(event.payload.get("size", -1))
                or hashlib.sha256(content).hexdigest()
                != str(event.payload.get("chunk_sha256", ""))
            ):
                raise RuntimeError("legacy learning snapshot chunk is corrupt")
            previous_count = chunk_counts.setdefault(path, count)
            if previous_count != count or index in chunks.setdefault(path, {}):
                raise RuntimeError("legacy learning snapshot chunk identity conflicts")
            chunks[path][index] = content
        position = page[-1].position

    reconstructed_hashes: dict[str, str] = {}
    for path, expected in sorted(files.items()):
        count = chunk_counts.get(path, 0)
        indexed = chunks.get(path, {})
        if set(indexed) != set(range(count)):
            raise RuntimeError(f"legacy learning snapshot chunks are incomplete: {path}")
        reconstructed = b"".join(indexed[index] for index in range(count))
        reconstructed_hashes[path] = hashlib.sha256(reconstructed).hexdigest()
        if reconstructed != expected:
            raise RuntimeError(f"legacy learning snapshot bytes differ: {path}")
    if set(chunks) != set(files):
        raise RuntimeError("legacy learning snapshot contains unexpected files")
    exact_matches = reconstructed_hashes == file_hashes
    return {
        "verified": projection_matches and exact_matches,
        "snapshot_sha256": snapshot_sha256,
        "file_count": len(files),
        "total_bytes": sum(len(content) for content in files.values()),
        "projection_matches": projection_matches,
        "exact_bytes_match": exact_matches,
        "projection_sha256": {
            _INSIGHT_PROJECTION: insight_projection.projection_sha256,
            _SKILL_PROJECTION: skill_projection.projection_sha256,
        },
    }


__all__ = [
    "LearningLegacyMigrationReport",
    "export_learning_legacy_snapshot",
    "import_legacy_learning_snapshot",
    "verify_legacy_learning_import",
    "verify_learning_legacy_export",
]
