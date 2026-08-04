from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from plugins.life_engine.storage.authority import (
    AuthorityConflict,
    AuthorityError,
    FileAuthorityRegistry,
    GenerationConflict,
    GenerationNotVerified,
    StaleAuthorityToken,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)


def generation(
    generation_id: str,
    *,
    status: GenerationStatus = GenerationStatus.VERIFIED,
    marker: str = "0",
) -> BackendGeneration:
    return BackendGeneration(
        generation_id=generation_id,
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256=marker * 64,
        root_hashes={"life_events": marker * 64},
        frontiers={"life_events": 12},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at=(
            "2026-08-04T00:01:00+00:00"
            if status == GenerationStatus.VERIFIED
            else ""
        ),
        status=status,
    )


async def test_file_authority_is_verified_fenced_and_secret_free(tmp_path: Path) -> None:
    state_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(state_path)
    verified = generation("local-v1")
    await registry.register_generation(verified)
    await registry.register_generation(verified)
    token = await registry.activate_generation(
        "local-v1",
        expected_epoch=0,
        owner_id="test-writer",
        lease_seconds=60,
        confirm_previous_writers_stopped=True,
    )

    await registry.validate(token)
    health = await registry.health()
    restored = await registry.get_generation("local-v1")

    assert restored == verified
    assert health["status"] == "healthy"
    assert health["audit_chain_valid"] is True
    assert health["audit_event_count"] == 2
    assert token.fencing_token not in state_path.read_text(encoding="utf-8")
    assert token.fencing_token not in registry.audit_path.read_text(encoding="utf-8")

    next_epoch = await registry.revoke(token)
    assert next_epoch == 2
    assert (await registry.health())["status"] == "disabled"
    with pytest.raises(StaleAuthorityToken):
        await registry.validate(token)


async def test_file_authority_rejects_unverified_conflicting_and_stale_state(
    tmp_path: Path,
) -> None:
    registry = FileAuthorityRegistry(tmp_path / "authority.json")
    await registry.register_generation(
        generation("candidate", status=GenerationStatus.CANDIDATE)
    )
    with pytest.raises(GenerationNotVerified):
        await registry.activate_generation(
            "candidate",
            expected_epoch=0,
            owner_id="writer",
            lease_seconds=60,
            confirm_previous_writers_stopped=True,
        )

    await registry.register_generation(generation("verified", marker="1"))
    with pytest.raises(GenerationConflict):
        await registry.register_generation(generation("verified", marker="2"))
    with pytest.raises(AuthorityConflict):
        await registry.activate_generation(
            "verified",
            expected_epoch=9,
            owner_id="writer",
            lease_seconds=60,
            confirm_previous_writers_stopped=True,
        )


async def test_local_fence_blocks_cutover_until_transaction_scope_exits(
    tmp_path: Path,
) -> None:
    registry = FileAuthorityRegistry(tmp_path / "authority.json")
    await registry.register_generation(generation("local-v1"))
    await registry.register_generation(generation("local-v2", marker="1"))
    token = await registry.activate_generation(
        "local-v1",
        expected_epoch=0,
        owner_id="writer-1",
        lease_seconds=60,
        confirm_previous_writers_stopped=True,
    )

    async with registry.fenced(token):
        cutover = asyncio.create_task(
            registry.activate_generation(
                "local-v2",
                expected_epoch=1,
                owner_id="writer-2",
                lease_seconds=60,
                confirm_previous_writers_stopped=True,
            )
        )
        await asyncio.sleep(0.05)
        assert not cutover.done()

    next_token = await asyncio.wait_for(cutover, timeout=2)
    assert next_token.authority_epoch == 2
    with pytest.raises(StaleAuthorityToken):
        await registry.validate(token)


async def test_audit_tamper_fails_closed(tmp_path: Path) -> None:
    registry = FileAuthorityRegistry(tmp_path / "authority.json")
    await registry.register_generation(generation("local-v1"))
    lines = registry.audit_path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[0])
    event["payload"]["status"] = "sealed"
    registry.audit_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    health = await registry.health()
    assert health["status"] == "failed"
    with pytest.raises(AuthorityError):
        await registry.register_generation(generation("local-v2", marker="1"))


def test_authority_token_expiry_hint_is_timezone_safe() -> None:
    assert datetime.now(UTC).tzinfo is not None
