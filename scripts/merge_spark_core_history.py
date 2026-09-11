"""Lossless Core message union for the explicitly frozen Spark cutover.

Only local integer message row IDs may be reassigned. Stable message IDs and
all payload fields remain exact. Other tables must be source supersets, with
only the observed operational clocks allowed to take the later exact value.
The command creates a new candidate and never overwrites either input.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

try:
    from scripts.audit_spark_cutover import digest, quote
except ModuleNotFoundError:
    from audit_spark_cutover import digest, quote


class CoreMergeConflict(RuntimeError):
    """A conflict that cannot be resolved without changing original content."""


def _schema(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()


def _later(a: Any, b: Any) -> Any:
    if a == b:
        return a
    if a is None or b is None:
        raise CoreMergeConflict("operational clock null conflict")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return max(a, b)
    if isinstance(a, str) and isinstance(b, str):
        return a if datetime.fromisoformat(a) >= datetime.fromisoformat(b) else b
    raise CoreMergeConflict("operational clock type conflict")


def merge_core(target: sqlite3.Connection, preserved: sqlite3.Connection) -> dict[str, Any]:
    """Append preserved messages transactionally; fail on stable-ID conflict."""
    if _schema(target) != _schema(preserved):
        raise CoreMergeConflict("Core schemas differ")
    tables = [
        row[0] for row in target.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    clocks = {"chat_streams": {"last_active_time"}, "person_info": {"updated_at"}}
    report: dict[str, Any] = {"inserted_messages": [], "clock_updates": {}, "already_present": 0}
    with target:
        target.execute("BEGIN IMMEDIATE")
        for table in tables:
            foreign_keys = target.execute(f"PRAGMA foreign_key_list({quote(table)})").fetchall()
            if any(row[2] == "messages" for row in foreign_keys):
                raise CoreMergeConflict("message primary key has incoming foreign keys")
            columns = target.execute(f"PRAGMA table_info({quote(table)})").fetchall()
            names = [row[1] for row in columns]
            if table == "messages":
                keys = ["message_id"]
            else:
                keys = [row[1] for row in columns if row[5]]
            if not keys:
                raise CoreMergeConflict(f"table lacks stable lookup: {table}")
            condition = " AND ".join(f"{quote(key)} IS ?" for key in keys)
            for original in preserved.execute(f"SELECT * FROM {quote(table)}"):
                data = dict(zip(names, original))
                if table == "messages" and not data["message_id"]:
                    raise CoreMergeConflict("message lacks stable identity")
                matches = target.execute(
                    f"SELECT * FROM {quote(table)} WHERE {condition}",
                    [data[key] for key in keys],
                ).fetchall()
                if len(matches) > 1:
                    raise CoreMergeConflict("ambiguous stable identity")
                if not matches:
                    if table != "messages":
                        raise CoreMergeConflict(f"preserved row absent in {table}")
                    payload_names = [name for name in names if name != "id"]
                    cursor = target.execute(
                        f"INSERT INTO messages ({','.join(quote(n) for n in payload_names)}) "
                        f"VALUES ({','.join('?' for _ in payload_names)})",
                        [data[name] for name in payload_names],
                    )
                    report["inserted_messages"].append({
                        "preserved_local_id": data["id"],
                        "candidate_local_id": cursor.lastrowid,
                        "message_identity_sha256": digest(data["message_id"]),
                        "payload_sha256": digest([data[name] for name in payload_names]),
                    })
                    continue
                current = dict(zip(names, matches[0]))
                permitted = clocks.get(table, set())
                changed = {
                    name for name in names if data[name] != current[name]
                    and not (table == "messages" and name == "id")
                }
                if changed - permitted:
                    raise CoreMergeConflict(f"non-clock stable identity conflict in {table}")
                for name in changed:
                    latest = _later(current[name], data[name])
                    if latest != current[name]:
                        target.execute(
                            f"UPDATE {quote(table)} SET {quote(name)}=? WHERE {condition}",
                            [latest, *[data[key] for key in keys]],
                        )
                        report["clock_updates"][table] = report["clock_updates"].get(table, 0) + 1
                if table == "messages":
                    report["already_present"] += 1
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--preserved", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--writer-frozen", action="store_true", required=True)
    args = parser.parse_args()
    source, preserved = [path.resolve(strict=True) for path in (args.source, args.preserved)]
    candidate = args.candidate.resolve()
    if source == preserved or candidate in (source, preserved):
        parser.error("all database paths must differ")
    with candidate.open("xb"):
        pass
    connections = []
    try:
        for path in (source, preserved):
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            connections.append(connection)
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise CoreMergeConflict("input database integrity failure")
        output = sqlite3.connect(candidate)
        connections.append(output)
        connections[0].backup(output)
        report = merge_core(output, connections[1])
        if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise CoreMergeConflict("candidate integrity failure")
        print(json.dumps({"candidate": str(candidate), "verified": True, **report}))
    finally:
        for connection in connections:
            connection.close()


if __name__ == "__main__":
    main()
