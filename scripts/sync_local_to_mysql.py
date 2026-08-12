#!/usr/bin/env python3
"""本地 SQLite → 远端 MySQL 增量同步（运维脚本）。

用法:
    python3 scripts/sync_local_to_mysql.py [--dry-run]

原则（遵守交接要求：对比后新增，不覆盖）:
- 以自然唯一键对比: messages.message_id / chat_streams.stream_id / person_info.person_id
- 仅 INSERT 远端缺失的行，绝不 UPDATE/DELETE 远端已有数据
- 幂等: 重复执行不会产生重复行
"""

from __future__ import annotations

import sqlite3
import subprocess
import os
import sys

SQLITE_PATH = "data/Elysium.db"
MYSQL_ARGS = ["mysql", "-h", "frp-one.com", "-P", "65429", "-u", "elysia", "-p1111"]
DB = "elysium"

# (表名, 自然唯一键)
TABLES = [
    ("messages", "message_id"),
    ("chat_streams", "stream_id"),
    ("person_info", "person_id"),
]


def mysql_escape(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    text = text.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def remote_existing_keys(table: str, key_col: str) -> set:
    sql = f"SELECT `{key_col}` FROM `{table}`;"
    result = subprocess.run(
        MYSQL_ARGS + ["-N", "-e", sql, DB],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    conn = sqlite3.connect(SQLITE_PATH)
    total_planned = 0

    for table, key_col in TABLES:
        cursor = conn.execute(f"PRAGMA table_info({table});")
        # 排除自增主键 id，让远端自行分配，避免主键冲突
        columns = [row[1] for row in cursor.fetchall() if row[1] != "id"]
        existing = remote_existing_keys(table, key_col)
        key_idx = columns.index(key_col)

        rows_to_insert = []
        select_cols = ", ".join(f'"{c}"' for c in columns)
        for row in conn.execute(f"SELECT {select_cols} FROM {table};"):
            if row[key_idx] not in existing:
                rows_to_insert.append(row)

        print(f"[{table}] 远端已有={len(existing)} 待插入={len(rows_to_insert)}")
        if not rows_to_insert or dry_run:
            total_planned += len(rows_to_insert)
            continue

        col_list = ", ".join(f"`{c}`" for c in columns)
        statements = []
        for row in rows_to_insert:
            values = ", ".join(mysql_escape(v) for v in row)
            statements.append(f"INSERT INTO `{table}` ({col_list}) VALUES ({values});")
        sql_batch = "\n".join(statements)

        result = subprocess.run(
            MYSQL_ARGS + [DB],
            input=sql_batch,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  ✗ 插入失败: {result.stderr.strip()[:300]}")
            return 1
        print(f"  ✓ 已插入 {len(rows_to_insert)} 行")

    conn.close()
    if dry_run:
        print(f"\n[dry-run] 共需插入 {total_planned} 行（未执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
