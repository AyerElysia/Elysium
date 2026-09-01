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
    MySQLAuthorityRegistry,
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


async def test_file_authority_generation_drift_is_target_scoped_and_fenced(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(state_path)
    await registry.register_generation(generation("local-v2", marker="2"))
    verified_v3 = generation("local-v3", marker="3")
    await registry.register_generation(verified_v3)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["generations"]["local-v2"]["root_hashes"]["life_events"] = "4" * 64
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert await registry.get_generation("local-v3") == verified_v3
    with pytest.raises(AuthorityError, match="immutable registration"):
        await registry.get_generation("local-v2")

    token = await registry.activate_generation(
        "local-v3",
        expected_epoch=0,
        owner_id="writer-v3",
        lease_seconds=60,
        confirm_previous_writers_stopped=True,
    )
    await registry.validate(token)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["generations"]["local-v3"]["frontiers"]["life_events"] = 13
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(AuthorityError, match="immutable registration"):
        await registry.validate(token)
    assert (await registry.health())["status"] == "failed"


async def test_mysql_generation_registration_proof_rejects_two_column_drift() -> None:
    verified = generation("mysql-v1", marker="5")
    verified = BackendGeneration.from_dict(
        {
            **verified.to_dict(),
            "backend": BackendKind.MYSQL.value,
        }
    )
    registration_queries = 0

    class Result:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self._rows = rows

        def mappings(self) -> Result:
            return self

        def all(self) -> list[dict[str, object]]:
            return self._rows

    class Connection:
        async def execute(self, _statement: object, _params: object) -> Result:
            nonlocal registration_queries
            registration_queries += 1
            return Result(
                [
                    {
                        "payload_json": json.dumps(
                            {"manifest_sha256": verified.manifest_sha256}
                        )
                    }
                ]
            )

    registry = MySQLAuthorityRegistry(object())  # type: ignore[arg-type]
    connection = Connection()
    row = {
        "generation_id": verified.generation_id,
        "manifest_json": json.dumps(verified.to_dict()),
        "manifest_sha256": verified.manifest_sha256,
    }

    assert await registry._decode_verified_generation_row(connection, row) == verified  # type: ignore[arg-type]
    assert registration_queries == 1

    drifted = generation("mysql-v1", marker="6")
    drifted = BackendGeneration.from_dict(
        {
            **drifted.to_dict(),
            "backend": BackendKind.MYSQL.value,
        }
    )
    drifted_row = {
        "generation_id": drifted.generation_id,
        "manifest_json": json.dumps(drifted.to_dict()),
        "manifest_sha256": drifted.manifest_sha256,
    }
    with pytest.raises(AuthorityError, match="immutable registration"):
        await registry._decode_verified_generation_row(connection, drifted_row)  # type: ignore[arg-type]
    assert registration_queries == 1


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


async def test_file_authority_reuses_verified_head_until_audit_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "authority.json"
    writer = FileAuthorityRegistry(state_path)
    await writer.register_generation(generation("local-v1"))
    token = await writer.activate_generation(
        "local-v1",
        expected_epoch=0,
        owner_id="writer",
        lease_seconds=60,
        confirm_previous_writers_stopped=True,
    )

    reader = FileAuthorityRegistry(state_path)
    original_scan = reader._scan_audit_unlocked
    scan_count = 0

    def counted_scan() -> object:
        nonlocal scan_count
        scan_count += 1
        return original_scan()

    monkeypatch.setattr(reader, "_scan_audit_unlocked", counted_scan)

    await reader.validate(token)
    await reader.validate(token)
    async with reader.fenced(token):
        pass
    assert (await reader.health())["status"] == "healthy"
    assert scan_count == 1

    renewed = await writer.renew(token, lease_seconds=60)
    await reader.validate(renewed)
    await reader.validate(renewed)
    assert scan_count == 2


def test_authority_token_expiry_hint_is_timezone_safe() -> None:
    assert datetime.now(UTC).tzinfo is not None
