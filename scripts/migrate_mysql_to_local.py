#!/usr/bin/env python3
"""MySQL -> 本地各域 SQLite 反向迁移（本地化切换的数据装载工具）。

用法:
    source .env.restart
    .venv/bin/python scripts/migrate_mysql_to_local.py \
        --mysql-url "mysql+asyncmy://elysia:$ELYSIUM_MYSQL_PASSWORD@frp-one.com:65429/elysium"

本地模式（backend=local）下存储运行时禁用，各域使用工作区内自有库：
  - event    -> data/life_engine_workspace/life_events.sqlite3
  - presence -> data/life_engine_workspace/runtime/consciousness_presence.sqlite3
  - world    -> data/life_engine_workspace/runtime/world_projection.sqlite3
  - memory   -> data/life_engine_workspace/.memory/memory.db
learning（.life_learning/*.json）与 subject（.life_narrative）为文件形态，
无法表级迁移；仅做 MySQL 新鲜度对比供人工决策。

策略:
1. 逐表 MySQL -> 对应本地库流式拷贝（INSERT OR IGNORE，幂等且保护本地更新数据）
2. 列以「MySQL 与本地表交集」为准，结构漂移不阻断
3. 报告每张表 源行数/拷贝行数/跳过行数/目标行数；目标低于源则退出码 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.util import greenlet_spawn

DOMAIN_TARGETS: dict[str, tuple[str, list[str]]] = {
    "event": (
        "data/life_engine_workspace/life_events.sqlite3",
        [
            "raw_life_events",
            "raw_event_consumer_offsets",
            "raw_event_ledger_meta",
            "raw_event_export_outbox",
        ],
    ),
    "presence": (
        "data/life_engine_workspace/runtime/consciousness_presence.sqlite3",
        [
            "consciousness_presence",
            "consciousness_stream_owners",
            "consciousness_presence_outbox",
        ],
    ),
    "world": (
        "data/life_engine_workspace/runtime/world_projection.sqlite3",
        [
            "world_projection_meta",
            "world_assertions",
            "world_projection_changes",
            "world_perception_cursors",
        ],
    ),
    "memory": (
        "data/life_engine_workspace/.memory/memory.db",
        [],
    ),
}

SKIP_TABLES = {"runtime_singleton_writer_claims"}

FILE_DOMAIN_FRESHNESS = [
    ("learning", "learning_events", "occurred_at"),
    ("subject", "subject_document_versions", "recorded_at"),
]


def _adapt(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return value


def local_columns(sqlite_conn, table: str) -> list[str] | None:
    rows = sqlite_conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        return None
    return [r[1] for r in rows]


async def copy_table(mysql_conn, sqlite_conn, table: str, batch_size: int = 2000) -> dict:
    count_result = await mysql_conn.execute(text(f"SELECT COUNT(*) FROM `{table}`"))
    source_count = count_result.scalar()
    col_result = await mysql_conn.execute(text(f"SHOW COLUMNS FROM `{table}`"))
    mysql_columns = [dict(row._mapping)["Field"] for row in col_result]
    target_columns = local_columns(sqlite_conn, table)
    if target_columns is None:
        return {"table": table, "error": "本地库无此表"}
    columns = [c for c in mysql_columns if c in target_columns]
    dropped = [c for c in mysql_columns if c not in target_columns]
    if not columns:
        return {"table": table, "error": "MySQL 与本地无公共列"}

    col_list = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    insert_sql = f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"

    copied = 0
    skipped = 0
    result = await mysql_conn.execute(text(f"SELECT {col_list} FROM `{table}`"))
    batch: list[tuple] = []

    def flush():
        nonlocal copied, skipped, batch
        if not batch:
            return
        before = sqlite_conn.total_changes
        sqlite_conn.executemany(insert_sql, batch)
        delta = sqlite_conn.total_changes - before
        copied += delta
        skipped += len(batch) - delta
        batch = []

    while True:
        rows = await greenlet_spawn(result.fetchmany, batch_size)
        if not rows:
            break
        for row in rows:
            batch.append(tuple(_adapt(v) for v in row))
        flush()
    flush()
    sqlite_conn.commit()
    final_count = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    info = {
        "table": table,
        "source_rows": int(source_count),
        "copied_rows": copied,
        "skipped_duplicate": skipped,
        "target_rows": final_count,
    }
    if dropped:
        info["dropped_columns"] = dropped
    return info


async def freshness(mysql_conn, table: str, time_col: str) -> dict:
    out = {"table": table}
    try:
        result = await mysql_conn.execute(
            text(f"SELECT MAX(`{time_col}`), COUNT(*) FROM `{table}`")
        )
        out["mysql_max"] = str(result.one())
    except Exception as exc:
        out["mysql_error"] = str(exc)[:120]
    return out


async def run(args) -> int:
    mysql_engine = create_async_engine(args.mysql_url, pool_pre_ping=True)
    report: dict = {"tables": [], "freshness": [], "skipped": []}
    failures: list[str] = []

    async with mysql_engine.connect() as mysql_conn:
        tables_result = await mysql_conn.execute(text("SHOW TABLES"))
        all_tables = {row[0] for row in tables_result}
        print(f"MySQL 共 {len(all_tables)} 张表")

        for domain, (rel_path, tables) in DOMAIN_TARGETS.items():
            db_path = (_REPOSITORY_ROOT / rel_path).resolve()
            if not db_path.exists():
                print(f"!! [{domain}] 本地库不存在: {db_path}")
                failures.append(f"{domain}:missing_db")
                continue
            sqlite_conn = sqlite3.connect(db_path)
            sqlite_conn.execute("PRAGMA journal_mode = WAL")
            sqlite_conn.execute("PRAGMA synchronous = NORMAL")

            if domain == "memory":
                tables = sorted(t for t in all_tables if t.startswith("memory_"))
            print(f"\n=== [{domain}] -> {rel_path} ===")
            for table in tables:
                if table in SKIP_TABLES:
                    continue
                if table not in all_tables:
                    print(f"  - {table}: MySQL 中不存在，跳过")
                    report["skipped"].append(table)
                    continue
                info = await copy_table(mysql_conn, sqlite_conn, table)
                info["domain"] = domain
                report["tables"].append(info)
                if "error" in info:
                    print(f"  - {table}: 跳过（{info['error']}）")
                    continue
                ok = info["target_rows"] >= info["source_rows"]
                if not ok:
                    failures.append(table)
                dropped = info.get("dropped_columns")
                print(
                    f"  - {table}: 源={info['source_rows']} 拷贝={info['copied_rows']} "
                    f"跳过={info['skipped_duplicate']} 目标={info['target_rows']} "
                    f"{'OK' if ok else 'CHECK'}"
                    + (f" (丢弃列: {dropped})" if dropped else "")
                )
            sqlite_conn.close()

        print("\n=== 文件形态域新鲜度（learning/subject） ===")
        for domain, table, time_col in FILE_DOMAIN_FRESHNESS:
            if table not in all_tables:
                print(f"  - [{domain}] MySQL 无 {table}")
                continue
            info = await freshness(mysql_conn, table, time_col)
            info["domain"] = domain
            report["freshness"].append(info)
            print(f"  - [{domain}] MySQL MAX/COUNT: {info.get('mysql_max')}")

    await mysql_engine.dispose()

    report_path = (_REPOSITORY_ROOT / args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {report_path}")

    if failures:
        print(f"!! 需人工核对: {failures}")
        return 1
    print("全部表行数校验通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MySQL -> 本地各域 SQLite 反向迁移")
    parser.add_argument("--mysql-url", required=True)
    parser.add_argument("--report", default="data/life_storage/migration_report.json")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
