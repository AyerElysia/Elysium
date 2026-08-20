#!/usr/bin/env python3
"""修复 memory_witnesses 反向迁移缺口：projection_path 唯一索引导致的冲突行。

对本地缺失的 witness_id，以「projection_path 置空」方式插入（投影路径可重建，
见证内容不可再生），保证见证内容完整。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.util import greenlet_spawn


async def main() -> int:
    password = os.environ["ELYSIUM_MYSQL_PASSWORD"]
    url = f"mysql+asyncmy://elysia:{password}@frp-one.com:65429/elysium"
    db_path = _ROOT / "data/life_engine_workspace/.memory/memory.db"

    sqlite_conn = sqlite3.connect(db_path)
    sqlite_conn.execute("PRAGMA journal_mode = WAL")
    local_cols = [r[1] for r in sqlite_conn.execute("PRAGMA table_info(memory_witnesses)")]
    local_ids = {
        r[0] for r in sqlite_conn.execute("SELECT witness_id FROM memory_witnesses")
    }
    print(f"本地现有 witness: {len(local_ids)}")

    engine = create_async_engine(url, pool_pre_ping=True)
    async with engine.connect() as conn:
        col_result = await conn.execute(text("SHOW COLUMNS FROM memory_witnesses"))
        mysql_cols = [dict(r._mapping)["Field"] for r in col_result]
        columns = [c for c in mysql_cols if c in local_cols]
        col_list = ", ".join(f"`{c}`" for c in columns)
        result = await conn.execute(
            text(f"SELECT {col_list} FROM memory_witnesses")
        )
        placeholders = ", ".join("?" for _ in columns)
        insert_sql = (
            f"INSERT OR IGNORE INTO memory_witnesses ({', '.join(columns)}) "
            f"VALUES ({placeholders})"
        )
        pp_idx = columns.index("projection_path") if "projection_path" in columns else None
        inserted = 0
        batch = []
        while True:
            rows = await greenlet_spawn(result.fetchmany, 2000)
            if not rows:
                break
            for row in rows:
                values = list(row)
                witness_id = values[columns.index("witness_id")]
                if witness_id in local_ids:
                    continue
                if pp_idx is not None:
                    values[pp_idx] = ""  # 规避唯一索引冲突；投影路径可重建
                batch.append(tuple(values))
            if batch:
                before = sqlite_conn.total_changes
                sqlite_conn.executemany(insert_sql, batch)
                inserted += sqlite_conn.total_changes - before
                batch = []
        sqlite_conn.commit()
    await engine.dispose()

    final = sqlite_conn.execute("SELECT COUNT(*) FROM memory_witnesses").fetchone()[0]
    sqlite_conn.close()
    print(f"新插入: {inserted}，本地最终: {final}（MySQL 源: 5303）")
    return 0 if final >= 5303 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
