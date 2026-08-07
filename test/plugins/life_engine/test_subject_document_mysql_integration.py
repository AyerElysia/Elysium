"""Opt-in real-MySQL contract for exact-byte subject document history."""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest

from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.factory import (
    MySQLBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentConflict,
)
from plugins.life_engine.storage.subject_factory import open_subject_document_store
from src.kernel.storage.engine import (
    MySQLStorageConfig,
    create_mysql_storage_engine,
)


def _config() -> MySQLStorageConfig:
    host = os.environ.get("ELYSIUM_TEST_MYSQL_HOST", "")
    database = os.environ.get("ELYSIUM_TEST_MYSQL_DATABASE", "")
    user = os.environ.get("ELYSIUM_TEST_MYSQL_USER", "")
    if not host or not database or not user:
        pytest.skip("isolated MySQL integration database is not configured")
    if os.environ.get("ELYSIUM_TEST_MYSQL_SUBJECT_ISOLATED") != "1":
        pytest.skip("Subject Document MySQL contract requires an isolated database")
    return MySQLStorageConfig(
        host=host,
        port=int(os.environ.get("ELYSIUM_TEST_MYSQL_PORT", "3306")),
        database=database,
        user=user,
        password=os.environ.get("ELYSIUM_TEST_MYSQL_PASSWORD", ""),
        ssl_mode=os.environ.get("ELYSIUM_TEST_MYSQL_SSL_MODE", "disabled"),  # type: ignore[arg-type]
    )


@pytest.mark.timeout(120)
async def test_mysql_subject_document_adapter_contract() -> None:
    """Exercise exact bytes, idempotency and head CAS in an isolated schema."""

    config = _config()
    identity = uuid4().hex
    generation = BackendGeneration(
        generation_id=f"mysql-subject-{identity}",
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256="5" * 64,
        root_hashes={"subject": "6" * 64},
        frontiers={"subject": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )
    registry_id = f"subject-integration-{identity}"
    engine = create_mysql_storage_engine(config)
    registry = MySQLAuthorityRegistry(engine, registry_id=registry_id)
    runtime = None
    try:
        await registry.register_generation(generation)
        runtime = await open_storage_backend(
            StorageFactorySettings(
                enabled=True,
                authoritative_backend=BackendKind.MYSQL,
                backend_generation=generation.generation_id,
                schema_version=1,
                registry_id=registry_id,
                authority_provider="mysql",
                authority_owner_id=f"writer-{identity}",
                mysql=MySQLBackendSettings(
                    host=config.host,
                    port=config.port,
                    database=config.database,
                    user=config.user,
                    password_env="TEST_SUBJECT_MYSQL_PASSWORD",
                    ssl_mode=config.ssl_mode,
                ),
            ),
            environment={"TEST_SUBJECT_MYSQL_PASSWORD": config.password},
        )
        store = await open_subject_document_store(runtime, initialize_schema=True)
        command = AppendSubjectDocumentVersion(
            logical_path=f"contract/{identity}.md",
            expected_revision=0,
            expected_head_version_id="",
            content_bytes=b"\xef\xbb\xbfexact\r\nbytes\r\n",
            occurrence_id=f"contract:{identity}",
            recorded_by="mysql-contract",
            recorded_source=f"test:{identity}",
            declared_owner="elysia",
            provenance_status="semantic_source_missing",
            encoding="utf-8-sig",
            newline_style="crlf",
        )
        committed = await store.append_version(command)
        assert await store.append_version(command) == committed
        assert (
            await store.get_version(committed.version.version_id)
        ).content_bytes == command.content_bytes
        with pytest.raises(SubjectDocumentConflict, match="identity"):
            await store.append_version(
                replace(command, content_bytes=b"conflicting bytes")
            )
    finally:
        if runtime is not None:
            await runtime.revoke_authority()
            await runtime.close()
        await engine.dispose()
