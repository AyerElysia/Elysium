"""旧 ThoughtStream 快照的只读、content-neutral 检查器。"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias, cast

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

SUPPORTED_LEGACY_STREAM_SCHEMA_VERSIONS = frozenset({2})
LEGACY_STREAM_STATUSES = frozenset({"active", "dormant", "completed"})


class LegacySnapshotError(Exception):
    """旧快照无法被无损、可信地消费。"""

    def __init__(
        self,
        source_path: Path,
        reason: str,
        *,
        row_ordinal: int | None = None,
        field: str | None = None,
    ) -> None:
        self.source_path = source_path
        self.reason = reason
        self.row_ordinal = row_ordinal
        self.field = field
        location: list[str] = [str(source_path)]
        if row_ordinal is not None:
            location.append(f"row={row_ordinal}")
        if field is not None:
            location.append(f"field={field}")
        super().__init__(f"{' '.join(location)}: {reason}")


class LegacySnapshotNotFoundError(LegacySnapshotError):
    """旧快照路径不存在。"""


class LegacySnapshotReadError(LegacySnapshotError):
    """旧快照无法完成只读访问。"""


class LegacySnapshotChangedError(LegacySnapshotError):
    """旧快照在读取期间发生变化。"""


class LegacySnapshotDecodeError(LegacySnapshotError):
    """旧快照不是严格 UTF-8。"""


class LegacySnapshotFormatError(LegacySnapshotError):
    """旧快照不是受支持的 JSON 文档结构。"""


class LegacySnapshotSchemaError(LegacySnapshotError):
    """旧快照违反已登记的 ThoughtStream schema。"""


@dataclass(frozen=True, slots=True)
class LegacyStreamRow:
    """源 JSON 中一行未经语义映射的记录。"""

    source_ordinal: int
    original_fields: MappingProxyType[str, JSONValue]
    row_sha256: str


@dataclass(frozen=True, slots=True)
class LegacyStreamsSnapshot:
    """可供归档/迁移层核验的逐字节旧快照。"""

    source_path: Path
    raw_bytes: bytes
    byte_length: int
    sha256: str
    schema_version: int
    global_revision: int | None
    rows: tuple[LegacyStreamRow, ...]
    status_counts: MappingProxyType[str, int]


class _DuplicateJSONKeyError(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError
        result[key] = value
    return result


def _reject_non_json_number(_value: str) -> None:
    raise ValueError


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError
    return parsed


def _stat_identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def _read_stable_bytes(source_path: Path) -> bytes:
    try:
        path_stat_before = source_path.stat()
    except FileNotFoundError as exc:
        raise LegacySnapshotNotFoundError(
            source_path, "legacy snapshot does not exist"
        ) from exc
    except OSError as exc:
        raise LegacySnapshotReadError(
            source_path, "legacy snapshot cannot be inspected"
        ) from exc

    try:
        with source_path.open("rb") as source:
            fd_stat_before = os.fstat(source.fileno())
            raw_bytes = source.read()
            fd_stat_after = os.fstat(source.fileno())
            path_stat_after = source_path.stat()
    except FileNotFoundError as exc:
        raise LegacySnapshotChangedError(
            source_path, "legacy snapshot disappeared during read"
        ) from exc
    except OSError as exc:
        raise LegacySnapshotReadError(
            source_path, "legacy snapshot cannot be read"
        ) from exc

    identities = {
        _stat_identity(path_stat_before),
        _stat_identity(fd_stat_before),
        _stat_identity(fd_stat_after),
        _stat_identity(path_stat_after),
    }
    if len(identities) != 1 or len(raw_bytes) != fd_stat_after.st_size:
        raise LegacySnapshotChangedError(
            source_path, "legacy snapshot changed during read"
        )
    return raw_bytes


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_known_field(
    source_path: Path,
    row: dict[str, JSONValue],
    *,
    source_ordinal: int,
    field: str,
) -> None:
    value = row[field]
    valid = True
    if field in {
        "id",
        "title",
        "created_at",
        "last_advanced_at",
        "last_thought",
        "status",
        "last_focused_at",
        "last_decay_at",
    }:
        valid = isinstance(value, str)
    elif field in {"advance_count", "revision"}:
        valid = _is_int(value) and value >= 0
    elif field == "curiosity_score":
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    elif field == "related_memories":
        valid = isinstance(value, list) and all(
            isinstance(item, str) for item in value
        )
    if not valid:
        raise LegacySnapshotSchemaError(
            source_path,
            "field has an invalid legacy type",
            row_ordinal=source_ordinal,
            field=field,
        )


def _validate_row(
    source_path: Path,
    row: dict[str, JSONValue],
    *,
    source_ordinal: int,
) -> None:
    required_fields = ("id", "title", "created_at", "last_advanced_at")
    for field in required_fields:
        if field not in row:
            raise LegacySnapshotSchemaError(
                source_path,
                "required field is missing",
                row_ordinal=source_ordinal,
                field=field,
            )

    known_fields = {
        "id",
        "title",
        "created_at",
        "last_advanced_at",
        "advance_count",
        "curiosity_score",
        "last_thought",
        "related_memories",
        "status",
        "last_focused_at",
        "last_decay_at",
        "revision",
    }
    for field in known_fields.intersection(row):
        _validate_known_field(
            source_path,
            row,
            source_ordinal=source_ordinal,
            field=field,
        )
    if "status" in row and row["status"] not in LEGACY_STREAM_STATUSES:
        raise LegacySnapshotSchemaError(
            source_path,
            "status is not part of the legacy protocol",
            row_ordinal=source_ordinal,
            field="status",
        )


def read_legacy_streams_snapshot(source_path: str | Path) -> LegacyStreamsSnapshot:
    """严格读取旧 ``streams.json``，不修改、补全或解释其主体语义。"""
    path = Path(source_path)
    raw_bytes = _read_stable_bytes(path)
    decode_failed = False
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        decode_failed = True
        text = ""
    if decode_failed:
        raise LegacySnapshotDecodeError(path, "legacy snapshot is not strict UTF-8")

    format_error = ""
    try:
        document = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_non_json_number,
            parse_float=_parse_json_float,
        )
    except _DuplicateJSONKeyError:
        format_error = "JSON object contains a duplicate key"
    except json.JSONDecodeError as exc:
        format_error = f"invalid JSON at line {exc.lineno} column {exc.colno}"
    except ValueError:
        format_error = "JSON contains a non-standard number"
    if format_error:
        raise LegacySnapshotFormatError(path, format_error)

    if not isinstance(document, dict):
        raise LegacySnapshotFormatError(path, "top-level JSON value must be an object")

    schema_version = document.get("schema_version")
    if not _is_int(schema_version):
        raise LegacySnapshotSchemaError(path, "schema_version must be an integer")
    if schema_version not in SUPPORTED_LEGACY_STREAM_SCHEMA_VERSIONS:
        raise LegacySnapshotSchemaError(path, "schema_version is not supported")

    global_revision = document.get("global_revision")
    if global_revision is not None and (
        not _is_int(global_revision) or global_revision < 0
    ):
        raise LegacySnapshotSchemaError(
            path, "global_revision must be a non-negative integer"
        )

    raw_rows = document.get("streams")
    if not isinstance(raw_rows, list):
        raise LegacySnapshotSchemaError(path, "streams must be a list")

    rows: list[LegacyStreamRow] = []
    seen_stream_ids: set[str] = set()
    status_counts: dict[str, int] = {}
    max_row_revision = 0
    for source_ordinal, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, dict):
            raise LegacySnapshotSchemaError(
                path,
                "stream row must be an object",
                row_ordinal=source_ordinal,
            )
        _validate_row(path, raw_row, source_ordinal=source_ordinal)
        stream_id = cast(str, raw_row["id"])
        if stream_id in seen_stream_ids:
            raise LegacySnapshotSchemaError(
                path,
                "stream id is duplicated",
                row_ordinal=source_ordinal,
                field="id",
            )
        seen_stream_ids.add(stream_id)

        revision = raw_row.get("revision")
        if _is_int(revision):
            max_row_revision = max(max_row_revision, revision)
        status = raw_row.get("status")
        if isinstance(status, str):
            status_counts[status] = status_counts.get(status, 0) + 1

        canonical_row = json.dumps(
            raw_row,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        rows.append(
            LegacyStreamRow(
                source_ordinal=source_ordinal,
                original_fields=MappingProxyType(dict(raw_row)),
                row_sha256=hashlib.sha256(canonical_row).hexdigest(),
            )
        )

    if global_revision is not None and max_row_revision > global_revision:
        raise LegacySnapshotSchemaError(
            path, "row revision exceeds global_revision"
        )

    return LegacyStreamsSnapshot(
        source_path=path,
        raw_bytes=raw_bytes,
        byte_length=len(raw_bytes),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        schema_version=cast(int, schema_version),
        global_revision=global_revision,
        rows=tuple(rows),
        status_counts=MappingProxyType(dict(sorted(status_counts.items()))),
    )
