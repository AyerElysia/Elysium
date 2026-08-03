from __future__ import annotations

import asyncio
import hashlib
import os
from uuid import uuid4

import pytest

from src.kernel.memory_archive.models import ArchiveMode, ArchiveRecord
from src.kernel.memory_archive.mysql_store import (
    MySQLArchiveConfig,
    RemoteMemoryArchive,
)


def _config_from_environment() -> MySQLArchiveConfig:
    host = os.environ.get("ELYSIUM_TEST_MYSQL_HOST", "")
    database = os.environ.get("ELYSIUM_TEST_MYSQL_DATABASE", "")
    user = os.environ.get("ELYSIUM_TEST_MYSQL_USER", "")
    if not host or not database or not user:
        pytest.skip("isolated MySQL integration database is not configured")
    return MySQLArchiveConfig(
        host=host,
        port=int(os.environ.get("ELYSIUM_TEST_MYSQL_PORT", "3306")),
        database=database,
        user=user,
        password=os.environ.get("ELYSIUM_TEST_MYSQL_PASSWORD", ""),
        ssl_mode=os.environ.get("ELYSIUM_TEST_MYSQL_SSL_MODE", "disabled"),
    )


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_mysql_archive_concurrent_replay_has_one_exact_manifest_link() -> None:
    store = RemoteMemoryArchive(_config_from_environment())
    suffix = uuid4().hex
    node_id = f"memory-archive-integration-{suffix}"
    manifest_id = str(uuid4())
    record = ArchiveRecord.build(
        source_node_id=node_id,
        source_domain="contract_probe",
        record_kind="concurrent_replay",
        logical_key="one",
        mode=ArchiveMode.VERSIONED,
        source_sequence=1,
        recorded_at="2026-08-03T00:00:00+08:00",
        visibility="owner_private",
        authority="integration_test",
        payload={"suffix": suffix},
    )
    try:
        await store.initialize()
        await store.start_run(manifest_id, node_id, run_mode="integration")
        results = await asyncio.gather(
            *(
                store.publish_batch(
                    [record],
                    manifest_id=manifest_id,
                    starting_ordinal=0,
                )
                for _ in range(8)
            )
        )
        digest = hashlib.sha256(
            f"{record.record_id}:{record.payload_hash}\n".encode("ascii")
        ).hexdigest()
        await store.finish_run(
            manifest_id,
            status="complete",
            scanned_count=1,
            accepted_count=sum(result[0].status == "accepted" for result in results),
            duplicate_count=sum(result[0].status == "duplicate" for result in results),
            conflict_count=0,
            source_counts={"selected:contract_probe": 1},
            root_hash=digest,
        )
        verification = await store.verify_run(manifest_id)
    finally:
        await store.close()

    statuses = [result[0].status for result in results]
    assert statuses.count("accepted") == 1
    assert statuses.count("duplicate") == 7
    assert verification["verified"] is True
    assert verification["linked_records"] == 1
