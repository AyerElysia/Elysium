"""Isolated restore and byte/row verification for archived memory records."""

from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .models import ArchiveRecord
from .mysql_store import RemoteMemoryArchive
from .sources import ArchiveSourceError, decode_value, iter_sqlite_records


class ArchiveRestoreError(RuntimeError):
    """A restore target is unsafe or archive content is incomplete."""


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


async def _all_heads(
    remote: RemoteMemoryArchive,
    *,
    source_node_id: str,
    source_domain: str,
    batch_size: int = 2000,
) -> list[ArchiveRecord]:
    records: list[ArchiveRecord] = []
    after = 0
    while True:
        batch = await remote.fetch_heads(
            source_node_id=source_node_id,
            source_domain=source_domain,
            after_position=after,
            limit=batch_size,
        )
        if not batch:
            return records
        records.extend(record for _, record in batch)
        after = batch[-1][0]


async def restore_sqlite_domain(
    remote: RemoteMemoryArchive,
    *,
    source_node_id: str,
    source_domain: str,
    output: Path,
) -> dict[str, Any]:
    """Restore one current SQLite domain into a new, isolated file."""

    target = output.resolve()
    if target.exists():
        raise ArchiveRestoreError(f"restore target already exists: {target}")
    records = await _all_heads(
        remote,
        source_node_id=source_node_id,
        source_domain=source_domain,
    )
    if not records:
        raise ArchiveRestoreError(f"archive has no heads for domain {source_domain!r}")
    schema_records = [
        record for record in records if record.record_kind.startswith("sqlite_schema:")
    ]
    row_records = [
        record for record in records if record.record_kind.startswith("sqlite_row:")
    ]
    table_sql: list[str] = []
    post_data_sql: list[str] = []
    for record in schema_records:
        payload = record.payload()
        sql = str(payload.get("sql", "")).strip()
        if not sql:
            continue
        object_type = str(payload.get("type", ""))
        if object_type == "table":
            table_sql.append(sql)
        else:
            post_data_sql.append(sql)
    if not table_sql:
        raise ArchiveRestoreError(
            f"archive domain {source_domain!r} has no table schema"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        for statement in table_sql:
            connection.execute(statement)
        table_counts: Counter[str] = Counter()
        for record in row_records:
            payload = record.payload()
            table = str(payload.get("table", ""))
            columns = payload.get("columns")
            if not table or not isinstance(columns, dict) or not columns:
                raise ArchiveRestoreError(
                    f"invalid archived SQLite row: {record.record_id}"
                )
            names = list(columns)
            placeholders = ",".join("?" for _ in names)
            connection.execute(
                f"INSERT INTO {_quote_identifier(table)} ("
                + ",".join(_quote_identifier(name) for name in names)
                + f") VALUES ({placeholders})",
                tuple(decode_value(columns[name]) for name in names),
            )
            table_counts[table] += 1
        for statement in post_data_sql:
            connection.execute(statement)
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise ArchiveRestoreError(
                f"restored SQLite integrity check failed: {integrity!r}"
            )
        foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_issues:
            raise ArchiveRestoreError(
                f"restored SQLite foreign-key check failed: {foreign_key_issues[:10]!r}"
            )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    archived_hashes = {record.record_id: record.payload_hash for record in records}
    try:
        restored_hashes = {
            record.record_id: record.payload_hash
            for record in iter_sqlite_records(
                target,
                source_node_id=source_node_id,
                domain=source_domain,
            )
        }
    except ArchiveSourceError as exc:
        raise ArchiveRestoreError(str(exc)) from exc
    missing = archived_hashes.keys() - restored_hashes.keys()
    unexpected = restored_hashes.keys() - archived_hashes.keys()
    mismatched = {
        record_id
        for record_id in archived_hashes.keys() & restored_hashes.keys()
        if archived_hashes[record_id] != restored_hashes[record_id]
    }
    if missing or unexpected or mismatched:
        raise ArchiveRestoreError(
            "restored SQLite records differ from archive: "
            f"missing={len(missing)}, unexpected={len(unexpected)}, "
            f"payload_mismatch={len(mismatched)}"
        )
    return {
        "source_domain": source_domain,
        "output": str(target),
        "records": len(records),
        "tables": dict(sorted(table_counts.items())),
        "integrity_check": "ok",
        "foreign_key_check": "ok",
        "record_identity_check": "ok",
        "payload_hash_check": "ok",
    }


async def restore_workspace(
    remote: RemoteMemoryArchive,
    *,
    source_node_id: str,
    output_root: Path,
) -> dict[str, Any]:
    """Restore subject files byte-for-byte into a new isolated directory."""

    target_root = output_root.resolve()
    if target_root.exists():
        raise ArchiveRestoreError(f"restore target already exists: {target_root}")
    records = await _all_heads(
        remote,
        source_node_id=source_node_id,
        source_domain="workspace",
    )
    chunks = {
        record.record_id: record
        for record in records
        if record.record_kind == "workspace_file_chunk"
    }
    files = [record for record in records if record.record_kind == "workspace_file"]
    if not files:
        raise ArchiveRestoreError("archive has no workspace file heads")
    target_root.mkdir(parents=True)
    total_bytes = 0
    for record in files:
        payload = record.payload()
        logical_path = str(payload.get("path", ""))
        if not logical_path:
            raise ArchiveRestoreError(
                f"workspace record has no path: {record.record_id}"
            )
        destination = (target_root / logical_path).resolve()
        try:
            destination.relative_to(target_root)
        except ValueError as exc:
            raise ArchiveRestoreError(
                f"workspace path escapes restore root: {logical_path}"
            ) from exc
        inline = str(payload.get("inline_base64", ""))
        if inline:
            content = base64.b64decode(inline, validate=True)
        else:
            parts: list[bytes] = []
            for chunk_id in payload.get("chunk_record_ids", []):
                chunk = chunks.get(str(chunk_id))
                if chunk is None:
                    raise ArchiveRestoreError(
                        f"workspace file is missing chunk {chunk_id}: {logical_path}"
                    )
                chunk_payload = chunk.payload()
                part = base64.b64decode(
                    str(chunk_payload.get("base64", "")), validate=True
                )
                if hashlib.sha256(part).hexdigest() != str(
                    chunk_payload.get("chunk_hash", "")
                ):
                    raise ArchiveRestoreError(
                        f"workspace chunk hash mismatch: {chunk_id}"
                    )
                parts.append(part)
            content = b"".join(parts)
        expected_bytes = int(payload.get("bytes", -1))
        expected_hash = str(payload.get("sha256", ""))
        if len(content) != expected_bytes:
            raise ArchiveRestoreError(f"workspace file size mismatch: {logical_path}")
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ArchiveRestoreError(f"workspace file hash mismatch: {logical_path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        mode = int(payload.get("mode", 0)) & 0o777
        if mode:
            destination.chmod(mode)
        mtime_ns = int(payload.get("mtime_ns", 0))
        if mtime_ns > 0:
            os.utime(destination, ns=(mtime_ns, mtime_ns))
        total_bytes += len(content)
    return {
        "source_domain": "workspace",
        "output": str(target_root),
        "files": len(files),
        "bytes": total_bytes,
        "byte_hash_check": "ok",
    }
