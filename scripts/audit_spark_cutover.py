"""Content-free, read-only comparison of frozen deployment data copies.

This does not select a history winner or mutate either database. Local primary
key collisions are reported separately from complete row identity. Reports
contain schema and hashes, never user content or primary-key values.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


def digest(value: Any) -> str:
    def encode(item: Any) -> Any:
        if isinstance(item, bytes):
            return {"bytes_hex": item.hex()}
        raise TypeError(type(item).__name__)

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=encode)
        .encode("utf-8")
    ).hexdigest()


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def inspect_database(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    try:
        checks = [row[0] for row in connection.execute("PRAGMA quick_check")]
        if checks != ["ok"]:
            raise RuntimeError(f"integrity check failed: {path.name}")
        tables = {}
        for name, sql in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall():
            columns = connection.execute(f"PRAGMA table_info({quote(name)})").fetchall()
            pk_columns = [
                row[0] for row in sorted(columns, key=lambda c: c[5]) if row[5]
            ]
            rows: Counter[str] = Counter()
            keyed: dict[str, Counter[str]] = {}
            for row in connection.execute(f"SELECT * FROM {quote(name)}"):
                row_hash = digest(row)
                rows[row_hash] += 1
                if pk_columns:
                    key_hash = digest([row[index] for index in pk_columns])
                    keyed.setdefault(key_hash, Counter())[row_hash] += 1
            tables[name] = {
                "columns": columns,
                "sql": sql,
                "foreign_keys": connection.execute(
                    f"PRAGMA foreign_key_list({quote(name)})"
                ).fetchall(),
                "rows": rows,
                "keys": keyed,
                "row_count": sum(rows.values()),
                "root": digest(sorted(rows.items())),
            }
        return tables
    finally:
        connection.rollback()
        connection.close()


def compare_database(left: Path, right: Path) -> dict[str, Any]:
    sides = [inspect_database(path) if path.is_file() else {} for path in (left, right)]
    report = {}
    for table in sorted(set(sides[0]) | set(sides[1])):
        a, b = [side.get(table) for side in sides]
        if a is None or b is None:
            report[table] = {"present": [a is not None, b is not None]}
            continue
        left_keys, right_keys = set(a["keys"]), set(b["keys"])
        common_keys = left_keys & right_keys
        report[table] = {
            "counts": [a["row_count"], b["row_count"]],
            "roots": [a["root"], b["root"]],
            "schema_equal": a["sql"] == b["sql"] and a["columns"] == b["columns"],
            "left_only_rows": sum((a["rows"] - b["rows"]).values()),
            "right_only_rows": sum((b["rows"] - a["rows"]).values()),
            "pk_left_only": len(left_keys - right_keys),
            "pk_right_only": len(right_keys - left_keys),
            "pk_conflicts": sum(a["keys"][key] != b["keys"][key] for key in common_keys),
            "columns": a["columns"],
            "foreign_keys": a["foreign_keys"],
        }
        if report[table]["pk_conflicts"] and report[table]["schema_equal"]:
            connection = sqlite3.connect(left.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                connection.execute(
                    "ATTACH DATABASE ? AS other", (right.resolve().as_uri() + "?mode=ro",)
                )
                columns = [row[1] for row in a["columns"]]
                keys = [row[1] for row in a["columns"] if row[5]]
                expressions = ",".join(
                    f"SUM(a.{quote(column)} IS NOT b.{quote(column)})" for column in columns
                )
                condition = " AND ".join(
                    f"a.{quote(key)} IS b.{quote(key)}" for key in keys
                )
                counts = connection.execute(
                    f"SELECT {expressions} FROM main.{quote(table)} a "
                    f"JOIN other.{quote(table)} b ON {condition}"
                ).fetchone()
                report[table]["changed_columns"] = {
                    column: count for column, count in zip(columns, counts) if count
                }
            finally:
                connection.close()
    return report


def file_hash(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def compare_files(left: Path, right: Path, *, exclude_sqlite: bool = False) -> dict[str, Any]:
    excluded = {"repair_backups", "new_api_relay", "__pycache__"}

    def files(root: Path) -> dict[str, Path]:
        return {
            path.relative_to(root).as_posix(): path
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
            and not (set(path.relative_to(root).parts) & excluded)
            and not (exclude_sqlite and path.name.endswith((
                ".db", ".db-wal", ".db-shm", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm"
            )))
        }

    sides = [files(root) for root in (left, right)]
    report = {"left_only": [], "right_only": [], "changed": [], "identical": 0}
    for name in sorted(set(sides[0]) | set(sides[1])):
        a, b = [side.get(name) for side in sides]
        if a is None or b is None:
            report["right_only" if a is None else "left_only"].append(name)
        elif a.stat().st_size != b.stat().st_size or file_hash(a) != file_hash(b):
            report["changed"].append(name)
        else:
            report["identical"] += 1
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path, nargs="?")
    parser.add_argument("right", type=Path, nargs="?")
    parser.add_argument("--summary-file", type=Path)
    parser.add_argument("--database", action="append", default=[])
    parser.add_argument("--files", action="store_true")
    parser.add_argument("--files-exclude-sqlite", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.summary_file:
        report = json.loads(args.summary_file.read_text(encoding="utf-8"))
        for name, tables in report.get("databases", {}).items():
            print(json.dumps({"database": name, "total_tables": len(tables)}))
            for table, value in tables.items():
                if value.get("left_only_rows") or value.get("right_only_rows") or not value.get("schema_equal"):
                    print(json.dumps({"table": table, **{
                        key: item for key, item in value.items()
                        if key not in ("roots", "columns", "foreign_keys")
                    }}))
        if "files" in report:
            print(json.dumps(report["files"]))
        return
    if args.left is None or args.right is None:
        parser.error("left and right comparison roots are required")
    left, right = args.left.resolve(strict=True), args.right.resolve(strict=True)
    if left == right:
        parser.error("comparison roots must differ")
    result: dict[str, Any] = {"left": str(left), "right": str(right), "databases": {}}
    for relative in args.database:
        paths = [(root / relative).resolve() for root in (left, right)]
        if any(not path.is_relative_to(root) for path, root in zip(paths, (left, right))):
            parser.error("database escapes comparison root")
        result["databases"][relative] = compare_database(*paths)
    if args.files:
        result["files"] = compare_files(left, right, exclude_sqlite=args.files_exclude_sqlite)
        result["files"]["sqlite_excluded"] = args.files_exclude_sqlite
    encoded = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.report:
        with args.report.open("x", encoding="utf-8") as output:
            output.write(encoded)
        print(json.dumps({"report": str(args.report), "sha256": file_hash(args.report)}))
    else:
        print(encoded)


if __name__ == "__main__":
    main()
