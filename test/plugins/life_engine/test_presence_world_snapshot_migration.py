"""Characterization tests for Presence/World snapshot migration."""

from __future__ import annotations

import json
import sqlite3
from typing import Self

import pytest

import plugins.life_engine.storage.domain_schema as domain_schema_module
from plugins.life_engine.storage.contracts import (
    StorageBackendRuntime,
    StorageRuntimeDisabled,
    StorageWriterRole,
)
from plugins.life_engine.storage.migration.domain_copy import (
    TABLE_SPECS,
    PresenceWorldCopyError,
    _insert_missing_rows,
    _validate_cross_table,
    aggregate_domain_root,
    domain_reports,
    normalize_domain_row,
)
from plugins.life_engine.storage.models import BackendKind


def test_presence_row_normalizes_json_and_timestamp_without_losing_semantics() -> None:
    spec = TABLE_SPECS["consciousness_presence"]
    row = {
        "instance_id": "voice:one",
        "kind": "voice_live",
        "display_name": "爱莉",
        "status": "suspended",
        "created_at": "2026-08-04T14:00:00.123456+08:00",
        "last_active_at": "",
        "suspended_at": "2026-08-04T14:01:00+08:00",
        "stream_ids_json": '["stream:voice", "stream:qq"]',
        "perception_filter_json": '{"b":2, "a":1}',
        "metadata_json": '{"自由":true}',
        "session_id": "episode:one",
        "process_epoch": "epoch:one",
        "lease_expires_at": "",
        "lease_duration_seconds": None,
        "revision": "7",
        "updated_at": "2026-08-04T14:01:00+08:00",
    }

    normalized = normalize_domain_row(spec, row, source=True)

    assert normalized["created_at"] == "2026-08-04T06:00:00.123456+00:00"
    assert normalized["perception_filter_json"] == '{"a":1,"b":2}'
    assert json.loads(normalized["metadata_json"]) == {"自由": True}
    assert normalized["revision"] == 7


def _valid_rows() -> dict[str, list[dict[str, object]]]:
    return {
        "consciousness_presence": [
            {
                "instance_id": "chat",
                "status": "active",
                "stream_ids_json": '["stream:chat"]',
            }
        ],
        "consciousness_stream_owners": [
            {
                "stream_id": "stream:chat",
                "instance_id": "chat",
            }
        ],
        "consciousness_presence_outbox": [],
        "world_projection_meta": [
            {"meta_key": "as_of_ingest_position", "meta_value": "9"}
        ],
        "world_assertions": [],
        "world_projection_changes": [{"ingest_position": 9}],
        "world_perception_cursors": [
            {"instance_id": "chat", "ingest_position": 8}
        ],
    }


def test_cross_table_validation_rejects_owner_and_cursor_regression() -> None:
    rows = _valid_rows()
    _validate_cross_table(rows)  # type: ignore[arg-type]

    rows["consciousness_stream_owners"][0]["instance_id"] = "missing"
    with pytest.raises(PresenceWorldCopyError, match="missing Presence"):
        _validate_cross_table(rows)  # type: ignore[arg-type]

    rows = _valid_rows()
    rows["world_perception_cursors"][0]["ingest_position"] = 10
    with pytest.raises(PresenceWorldCopyError, match="exceeds projector frontier"):
        _validate_cross_table(rows)  # type: ignore[arg-type]


def test_domain_root_is_independent_of_input_report_order() -> None:
    rows = {
        name: []
        for name in TABLE_SPECS
    }
    reports = domain_reports(rows)

    assert aggregate_domain_root(reports) == aggregate_domain_root(list(reversed(reports)))


def test_local_schema_accepts_adapter_normalized_optional_timestamps() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in domain_schema_module._LOCAL_STATEMENTS:
            connection.execute(statement)
        optional_columns = {
            "consciousness_presence": {
                "created_at",
                "last_active_at",
                "suspended_at",
                "lease_expires_at",
            },
            "world_assertions": {
                "observed_at",
                "valid_from",
                "valid_to",
                "recorded_at",
                "retracted_at",
            },
            "world_projection_changes": {"occurred_at", "recorded_at"},
        }
        for table_name, column_names in optional_columns.items():
            columns = {
                str(row[1]): row
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            assert column_names <= columns.keys()
            assert all(int(columns[name][3]) == 0 for name in column_names)
    finally:
        connection.close()


async def test_candidate_copy_can_create_schema_only_while_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations = 0
    applications: list[tuple[object, ...]] = []

    async def validate() -> None:
        nonlocal validations
        validations += 1

    class _Runner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def apply(self, migrations: tuple[object, ...]) -> None:
            applications.append(migrations)

    monkeypatch.setattr(domain_schema_module, "MySQLMigrationRunner", _Runner)
    runtime = StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.MYSQL,
        backend_identity="mysql://candidate",
        generation=None,
        authority_registry=None,
        authority_token=None,
        engine=object(),  # type: ignore[arg-type]
        session_factory=None,
        _writer_validator=validate,
        writer_role=StorageWriterRole.CANDIDATE_COPY,
        writer_epoch=3,
    )

    await domain_schema_module.ensure_presence_world_schema(runtime)

    assert validations == 2
    assert len(applications) == 1


async def test_unfenced_candidate_copy_cannot_create_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ForbiddenRunner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("unfenced schema migration reached the runner")

    monkeypatch.setattr(
        domain_schema_module,
        "MySQLMigrationRunner",
        _ForbiddenRunner,
    )
    runtime = StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.MYSQL,
        backend_identity="mysql://unfenced",
        generation=None,
        authority_registry=None,
        authority_token=None,
        engine=object(),  # type: ignore[arg-type]
        session_factory=None,
        writer_role=StorageWriterRole.CANDIDATE_COPY,
    )

    with pytest.raises(StorageRuntimeDisabled, match="no writer authority"):
        await domain_schema_module.ensure_presence_world_schema(runtime)


async def test_only_virgin_initialized_world_frontier_is_repairable() -> None:
    source = {name: [] for name in TABLE_SPECS}
    target = {name: [] for name in TABLE_SPECS}
    source["world_projection_meta"] = [
        {
            "meta_key": "as_of_ingest_position",
            "meta_value": "9",
            "updated_at": "2026-08-04T01:00:00+00:00",
        }
    ]
    target["world_projection_meta"] = [
        {
            "meta_key": "as_of_ingest_position",
            "meta_value": "0",
            "updated_at": "2026-08-04T00:00:00+00:00",
        }
    ]

    class _Result:
        rowcount = 1

    class _Session:
        async def execute(self, *_args: object, **_kwargs: object) -> _Result:
            return _Result()

    class _Uow:
        session = _Session()

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Runtime:
        def unit_of_work(self) -> _Uow:
            return _Uow()

    assert await _insert_missing_rows(_Runtime(), source, target) == 0  # type: ignore[arg-type]

    target["world_projection_changes"] = [{"ingest_position": 1}]
    with pytest.raises(PresenceWorldCopyError, match="different evidence"):
        await _insert_missing_rows(_Runtime(), source, target)  # type: ignore[arg-type]


async def test_virgin_contract_meta_timestamp_drift_is_repairable() -> None:
    """合同 meta（policy/version/rebuild_state）在 virgin 目标上只允许时间戳对齐。

    runbook：这些键由运行合同生成、不能冒充源记录。目标为 virgin 且值一致
    时，仅 updated_at 漂移不构成证据冲突，按源行对齐；值差异仍是硬冲突。
    """

    source = {name: [] for name in TABLE_SPECS}
    target = {name: [] for name in TABLE_SPECS}
    source["world_projection_meta"] = [
        {
            "meta_key": "projector_policy",
            "meta_value": "source-preserving-v1",
            "updated_at": "2026-08-04T10:27:07.735073+00:00",
        }
    ]
    target["world_projection_meta"] = [
        {
            "meta_key": "projector_policy",
            "meta_value": "source-preserving-v1",
            "updated_at": "2026-08-23T08:00:00+00:00",
        }
    ]

    class _Result:
        rowcount = 1

    class _Session:
        async def execute(self, *_args: object, **_kwargs: object) -> _Result:
            return _Result()

    class _Uow:
        session = _Session()

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Runtime:
        def unit_of_work(self) -> _Uow:
            return _Uow()

    assert (
        await _insert_missing_rows(_Runtime(), source, target) == 0  # type: ignore[arg-type]
    )

    # 值不同：仍是硬冲突，不允许修复。
    target["world_projection_meta"][0]["meta_value"] = "different-policy"
    with pytest.raises(PresenceWorldCopyError, match="different evidence"):
        await _insert_missing_rows(_Runtime(), source, target)  # type: ignore[arg-type]

    # 非 virgin（已有物化行）：时间戳漂移也是硬冲突。
    target["world_projection_meta"][0]["meta_value"] = "source-preserving-v1"
    target["world_projection_changes"] = [{"ingest_position": 1}]
    with pytest.raises(PresenceWorldCopyError, match="different evidence"):
        await _insert_missing_rows(_Runtime(), source, target)  # type: ignore[arg-type]
