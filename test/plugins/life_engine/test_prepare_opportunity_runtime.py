"""Safety contract for the explicit Opportunity schema preparation command."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.storage.authority import FileAuthorityRegistry
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
from plugins.life_engine.storage.opportunity_contracts import OpportunityRuntimeMarker
from scripts import prepare_opportunity_runtime as prepare


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="opportunity-cli-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="1" * 64,
        root_hashes={"opportunity": "2" * 64},
        frontiers={"opportunity": 0},
        created_at="2026-09-05T00:00:00+00:00",
        verified_at="2026-09-05T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


def _settings() -> StorageFactorySettings:
    return StorageFactorySettings(
        enabled=True,
        authoritative_backend=BackendKind.LOCAL,
        backend_generation="opportunity-cli-v1",
    )


@dataclass
class _FakeRuntime:
    generation: BackendGeneration
    writer_role: Any
    calls: list[str]
    revoke_error: BaseException | None = None
    close_error: BaseException | None = None

    async def validate_writer(self) -> None:
        self.calls.append("validate")

    async def revoke_authority(self) -> int | None:
        self.calls.append("revoke")
        if self.revoke_error is not None:
            raise self.revoke_error
        return 2

    async def close(self) -> None:
        self.calls.append("close")
        if self.close_error is not None:
            raise self.close_error


def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prepare, "_load_settings", lambda *_args: _settings())


@pytest.mark.asyncio
async def test_default_is_connection_free_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)

    async def forbidden_open(_settings: object) -> object:
        raise AssertionError("dry run must not open a backend")

    monkeypatch.setattr(prepare, "open_storage_backend", forbidden_open)
    result = await prepare._run(prepare._arguments([]))

    assert result["status"] == "planned"
    assert result["mode"] == "dry_run"
    assert result["generation_id"] == "opportunity-cli-v1"
    assert result["installs_capabilities"] is False


@pytest.mark.asyncio
async def test_apply_requires_exact_generation_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)

    async def forbidden_open(_settings: object) -> object:
        raise AssertionError("mismatch must fail before opening a backend")

    monkeypatch.setattr(prepare, "open_storage_backend", forbidden_open)
    args = prepare._arguments(
        [
            "--apply",
            "--confirm-writer-runtime",
            "--confirm-generation",
            "wrong",
        ]
    )

    with pytest.raises(RuntimeError, match="GenerationConfirmationMismatch"):
        await prepare._run(args)


@pytest.mark.asyncio
async def test_apply_verifies_then_explicitly_marks_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)
    calls: list[str] = []
    runtime = _FakeRuntime(
        generation=_generation(),
        writer_role=prepare.StorageWriterRole.ACTIVE,
        calls=calls,
    )
    marker = OpportunityRuntimeMarker(
        generation_id=runtime.generation.generation_id,
        migration_occurrence_id="opportunity:migration:cli-v1",
        schema_version=2,
        activated_at="2026-09-05T00:02:00+00:00",
    )

    async def open_runtime(_settings: object) -> _FakeRuntime:
        calls.append("open")
        return runtime

    async def ensure_runtime(_runtime: object) -> None:
        calls.append("ensure_runtime")

    async def ensure_opportunity(_runtime: object, **_kwargs: object) -> None:
        calls.append("ensure_opportunity")

    async def verify(_runtime: object, **_kwargs: object) -> None:
        calls.append("verify")

    async def mark(
        _runtime: object,
        *,
        migration_occurrence_id: str,
    ) -> OpportunityRuntimeMarker:
        assert migration_occurrence_id == marker.migration_occurrence_id
        calls.append("mark")
        return marker

    monkeypatch.setattr(prepare, "open_storage_backend", open_runtime)
    monkeypatch.setattr(prepare, "ensure_runtime_state_schema", ensure_runtime)
    monkeypatch.setattr(prepare, "ensure_opportunity_schema", ensure_opportunity)
    monkeypatch.setattr(prepare, "verify_opportunity_schema", verify)
    monkeypatch.setattr(prepare, "mark_opportunity_runtime_managed", mark)
    args = prepare._arguments(
        [
            "--apply",
            "--confirm-writer-runtime",
            "--confirm-generation",
            "opportunity-cli-v1",
            "--mark-managed",
            "--migration-occurrence-id",
            marker.migration_occurrence_id,
        ]
    )

    result = await prepare._run(args)

    assert result["status"] == "applied"
    assert result["managed_marker"]["marker_sha256"] == marker.marker_sha256
    assert calls == [
        "open",
        "validate",
        "ensure_runtime",
        "ensure_opportunity",
        "verify",
        "mark",
        "validate",
        "revoke",
        "close",
    ]


@pytest.mark.asyncio
async def test_apply_without_mark_reports_an_existing_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)
    calls: list[str] = []
    runtime = _FakeRuntime(
        generation=_generation(),
        writer_role=prepare.StorageWriterRole.ACTIVE,
        calls=calls,
    )
    marker = OpportunityRuntimeMarker(
        generation_id="opportunity-original-generation",
        migration_occurrence_id="opportunity:migration:existing",
        schema_version=2,
        activated_at="2026-09-05T00:02:00+00:00",
    )

    async def open_runtime(_settings: object) -> _FakeRuntime:
        calls.append("open")
        return runtime

    async def ensure_runtime(_runtime: object) -> None:
        calls.append("ensure_runtime")

    async def ensure_opportunity(_runtime: object, **_kwargs: object) -> None:
        calls.append("ensure_opportunity")

    async def verify(_runtime: object, **_kwargs: object) -> None:
        calls.append("verify")

    async def read(_runtime: object) -> OpportunityRuntimeMarker:
        calls.append("read")
        return marker

    monkeypatch.setattr(prepare, "open_storage_backend", open_runtime)
    monkeypatch.setattr(prepare, "ensure_runtime_state_schema", ensure_runtime)
    monkeypatch.setattr(prepare, "ensure_opportunity_schema", ensure_opportunity)
    monkeypatch.setattr(prepare, "verify_opportunity_schema", verify)
    monkeypatch.setattr(prepare, "read_opportunity_runtime_marker", read)

    result = await prepare._run(
        prepare._arguments(
            [
                "--apply",
                "--confirm-writer-runtime",
                "--confirm-generation",
                "opportunity-cli-v1",
            ]
        )
    )

    assert result["managed_marker"]["marker_sha256"] == marker.marker_sha256
    assert calls == [
        "open",
        "validate",
        "ensure_runtime",
        "ensure_opportunity",
        "verify",
        "read",
        "validate",
        "revoke",
        "close",
    ]


@pytest.mark.asyncio
async def test_verify_reads_marker_and_failure_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)
    calls: list[str] = []
    runtime = _FakeRuntime(
        generation=_generation(),
        writer_role=prepare.StorageWriterRole.ACTIVE,
        calls=calls,
    )

    async def open_runtime(_settings: object) -> _FakeRuntime:
        calls.append("open")
        return runtime

    async def failed_verify(_runtime: object, **_kwargs: object) -> None:
        calls.append("verify")
        raise RuntimeError("OpportunitySchemaNotReady")

    async def forbidden_read(_runtime: object) -> None:
        raise AssertionError("failed schema verification must not read marker")

    monkeypatch.setattr(prepare, "open_storage_backend", open_runtime)
    monkeypatch.setattr(prepare, "verify_opportunity_schema", failed_verify)
    monkeypatch.setattr(prepare, "read_opportunity_runtime_marker", forbidden_read)

    with pytest.raises(RuntimeError, match="OpportunitySchemaNotReady"):
        await prepare._run(
            prepare._arguments(
                [
                    "--verify",
                    "--confirm-writer-runtime",
                    "--confirm-generation",
                    "opportunity-cli-v1",
                ]
            )
        )
    assert calls == ["open", "validate", "verify", "revoke", "close"]


@pytest.mark.asyncio
async def test_verify_accepts_managed_marker_preserved_from_source_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)
    calls: list[str] = []
    runtime = _FakeRuntime(
        generation=_generation(),
        writer_role=prepare.StorageWriterRole.ACTIVE,
        calls=calls,
    )
    marker = OpportunityRuntimeMarker(
        generation_id="opportunity-original-generation",
        migration_occurrence_id="opportunity:migration:original",
        schema_version=2,
        activated_at="2026-09-05T00:02:00+00:00",
    )

    async def open_runtime(_settings: object) -> _FakeRuntime:
        calls.append("open")
        return runtime

    async def verify(_runtime: object, **_kwargs: object) -> None:
        calls.append("verify")

    async def read(_runtime: object) -> OpportunityRuntimeMarker:
        calls.append("read")
        return marker

    monkeypatch.setattr(prepare, "open_storage_backend", open_runtime)
    monkeypatch.setattr(prepare, "verify_opportunity_schema", verify)
    monkeypatch.setattr(prepare, "read_opportunity_runtime_marker", read)

    result = await prepare._run(
        prepare._arguments(
            [
                "--verify",
                "--confirm-writer-runtime",
                "--confirm-generation",
                "opportunity-cli-v1",
            ]
        )
    )

    assert result["status"] == "verified"
    assert result["managed_marker"]["generation_id"] == marker.generation_id
    assert calls == [
        "open",
        "validate",
        "verify",
        "read",
        "validate",
        "revoke",
        "close",
    ]


@pytest.mark.asyncio
async def test_verify_reports_schema_ready_but_not_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)
    calls: list[str] = []
    runtime = _FakeRuntime(
        generation=_generation(),
        writer_role=prepare.StorageWriterRole.ACTIVE,
        calls=calls,
    )

    async def open_runtime(_settings: object) -> _FakeRuntime:
        calls.append("open")
        return runtime

    async def verify(_runtime: object, **_kwargs: object) -> None:
        calls.append("verify")

    async def read(_runtime: object) -> None:
        calls.append("read")

    monkeypatch.setattr(prepare, "open_storage_backend", open_runtime)
    monkeypatch.setattr(prepare, "verify_opportunity_schema", verify)
    monkeypatch.setattr(prepare, "read_opportunity_runtime_marker", read)

    result = await prepare._run(
        prepare._arguments(
            [
                "--verify",
                "--confirm-writer-runtime",
                "--confirm-generation",
                "opportunity-cli-v1",
            ]
        )
    )

    assert result["status"] == "verified"
    assert result["managed_marker"] == {
        "present": False,
        "reason": "managed_marker_not_written",
    }
    assert calls == [
        "open",
        "validate",
        "verify",
        "read",
        "validate",
        "revoke",
        "close",
    ]


@pytest.mark.asyncio
async def test_cancelled_verify_preserves_cancel_and_releases_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)
    calls: list[str] = []
    runtime = _FakeRuntime(
        generation=_generation(),
        writer_role=prepare.StorageWriterRole.ACTIVE,
        calls=calls,
        revoke_error=RuntimeError("authority revoke failed"),
    )
    cancellation = asyncio.CancelledError("operator cancelled")

    async def open_runtime(_settings: object) -> _FakeRuntime:
        calls.append("open")
        return runtime

    async def cancelled_verify(_runtime: object, **_kwargs: object) -> None:
        calls.append("verify")
        raise cancellation

    monkeypatch.setattr(prepare, "open_storage_backend", open_runtime)
    monkeypatch.setattr(prepare, "verify_opportunity_schema", cancelled_verify)

    with pytest.raises(asyncio.CancelledError) as captured:
        await prepare._run(
            prepare._arguments(
                [
                    "--verify",
                    "--confirm-writer-runtime",
                    "--confirm-generation",
                    "opportunity-cli-v1",
                ]
            )
        )

    assert captured.value is cancellation
    assert any(
        "cleanup also failed: RuntimeError" in note
        for note in getattr(cancellation, "__notes__", ())
    )
    assert calls == ["open", "validate", "verify", "revoke", "close"]


@pytest.mark.asyncio
async def test_revoke_failure_still_closes_and_fails_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)
    calls: list[str] = []
    runtime = _FakeRuntime(
        generation=_generation(),
        writer_role=prepare.StorageWriterRole.ACTIVE,
        calls=calls,
        revoke_error=RuntimeError("authority revoke failed"),
    )

    async def open_runtime(_settings: object) -> _FakeRuntime:
        calls.append("open")
        return runtime

    async def verify(_runtime: object, **_kwargs: object) -> None:
        calls.append("verify")

    async def read(_runtime: object) -> None:
        calls.append("read")

    monkeypatch.setattr(prepare, "open_storage_backend", open_runtime)
    monkeypatch.setattr(prepare, "verify_opportunity_schema", verify)
    monkeypatch.setattr(prepare, "read_opportunity_runtime_marker", read)

    with pytest.raises(RuntimeError, match="authority revoke failed"):
        await prepare._run(
            prepare._arguments(
                [
                    "--verify",
                    "--confirm-writer-runtime",
                    "--confirm-generation",
                    "opportunity-cli-v1",
                ]
            )
        )

    assert calls == [
        "open",
        "validate",
        "verify",
        "read",
        "validate",
        "revoke",
        "close",
    ]


@pytest.mark.asyncio
async def test_primary_failure_survives_revoke_and_close_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)
    calls: list[str] = []
    primary = ValueError("schema failure")
    runtime = _FakeRuntime(
        generation=_generation(),
        writer_role=prepare.StorageWriterRole.ACTIVE,
        calls=calls,
        revoke_error=RuntimeError("authority revoke failed"),
        close_error=OSError("engine close failed"),
    )

    async def open_runtime(_settings: object) -> _FakeRuntime:
        calls.append("open")
        return runtime

    async def failed_verify(_runtime: object, **_kwargs: object) -> None:
        calls.append("verify")
        raise primary

    monkeypatch.setattr(prepare, "open_storage_backend", open_runtime)
    monkeypatch.setattr(prepare, "verify_opportunity_schema", failed_verify)

    with pytest.raises(ValueError) as captured:
        await prepare._run(
            prepare._arguments(
                [
                    "--verify",
                    "--confirm-writer-runtime",
                    "--confirm-generation",
                    "opportunity-cli-v1",
                ]
            )
        )

    assert captured.value is primary
    assert any(
        "cleanup also failed: ExceptionGroup" in note
        for note in getattr(primary, "__notes__", ())
    )
    assert calls == ["open", "validate", "verify", "revoke", "close"]


@pytest.mark.asyncio
async def test_verify_releases_local_authority_for_immediate_reactivation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "opportunity.sqlite3"
    authority_path = tmp_path / "authority.json"
    generation = _generation()
    registry = FileAuthorityRegistry(authority_path)
    await registry.register_generation(generation)
    first_token = await registry.activate_generation(
        generation.generation_id,
        expected_epoch=0,
        owner_id="opportunity-cli",
        lease_seconds=60,
        confirm_previous_writers_stopped=True,
    )
    settings = StorageFactorySettings(
        enabled=True,
        authoritative_backend=BackendKind.LOCAL,
        backend_generation=generation.generation_id,
        schema_version=generation.schema_version,
        authority_epoch=first_token.authority_epoch,
        authority_owner_id=first_token.owner_id,
        fencing_token_env="TEST_OPPORTUNITY_CLI_FENCING_TOKEN",
        local=LocalBackendSettings(
            database_path=database_path,
            authority_state_path=authority_path,
        ),
    )
    monkeypatch.setattr(prepare, "_load_settings", lambda *_args: settings)
    monkeypatch.setenv(
        settings.fencing_token_env,
        first_token.fencing_token,
    )

    async def verify(_runtime: object, **_kwargs: object) -> None:
        return None

    async def read(_runtime: object) -> None:
        return None

    monkeypatch.setattr(prepare, "verify_opportunity_schema", verify)
    monkeypatch.setattr(prepare, "read_opportunity_runtime_marker", read)

    result = await prepare._run(
        prepare._arguments(
            [
                "--verify",
                "--confirm-writer-runtime",
                "--confirm-generation",
                generation.generation_id,
            ]
        )
    )

    released_health = await registry.health()
    assert result["status"] == "verified"
    assert released_health["status"] == "disabled"
    assert released_health["active_generation"] == ""

    next_token = await registry.activate_generation(
        generation.generation_id,
        expected_epoch=int(released_health["authority_epoch"]),
        owner_id="service-after-cli",
        lease_seconds=60,
        confirm_previous_writers_stopped=True,
    )
    next_settings = StorageFactorySettings(
        enabled=True,
        authoritative_backend=BackendKind.LOCAL,
        backend_generation=generation.generation_id,
        schema_version=generation.schema_version,
        authority_epoch=next_token.authority_epoch,
        authority_owner_id=next_token.owner_id,
        fencing_token_env="TEST_SERVICE_FENCING_TOKEN",
        local=settings.local,
    )
    reopened = await open_storage_backend(
        next_settings,
        environment={next_settings.fencing_token_env: next_token.fencing_token},
    )
    try:
        await reopened.validate_writer()
    finally:
        await reopened.revoke_authority()
        await reopened.close()


@pytest.mark.asyncio
async def test_verify_requires_writer_runtime_acknowledgement_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)

    async def forbidden_open(_settings: object) -> object:
        raise AssertionError("missing acknowledgement must fail before open")

    monkeypatch.setattr(prepare, "open_storage_backend", forbidden_open)
    with pytest.raises(RuntimeError, match="WriterRuntimeAcknowledgementRequired"):
        await prepare._run(
            prepare._arguments(
                [
                    "--verify",
                    "--confirm-generation",
                    "opportunity-cli-v1",
                ]
            )
        )


def test_marker_arguments_are_explicit() -> None:
    with pytest.raises(SystemExit):
        prepare._arguments(["--mark-managed"])
    with pytest.raises(SystemExit):
        prepare._arguments(
            [
                "--apply",
                "--confirm-generation",
                "opportunity-cli-v1",
                "--mark-managed",
            ]
        )
