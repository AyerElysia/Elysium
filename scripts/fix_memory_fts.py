"""Repair memory_fts coverage after MySQL->local migration (FTS5 content was not migrated).

Rebuilds document-level FTS rows from memory_chunks content for every live node
missing a memory_fts row, and marks permanently unclaimable index jobs as stale.
Run only while Elysium is stopped.
"""
from __future__ import annotations

import sqlite3
import sys
import time

DB_PATH = "data/life_engine_workspace/.memory/memory.db"


def main() -> int:
    db = sqlite3.connect(DB_PATH, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 60000")

    missing = db.execute(
        """
        SELECT n.node_id, n.title
        FROM memory_nodes n
        LEFT JOIN memory_fts f ON f.node_id = n.node_id
        WHERE n.is_deleted = 0 AND f.node_id IS NULL
        ORDER BY n.node_id
        """
    ).fetchall()
    print(f"nodes missing fts: {len(missing)}")

    rebuilt = 0
    empty = 0
    started = time.time()
    with db:
        for i, row in enumerate(missing):
            node_id = row["node_id"]
            chunks = db.execute(
                "SELECT content FROM memory_chunks WHERE node_id = ? ORDER BY chunk_index",
                (node_id,),
            ).fetchall()
            content = "\n".join(str(c["content"] or "") for c in chunks).strip()
            if not content:
                empty += 1
                continue
            db.execute(
                "INSERT INTO memory_fts(node_id, title, content) VALUES (?, ?, ?)",
                (node_id, str(row["title"] or ""), content),
            )
            rebuilt += 1
            if (i + 1) % 500 == 0:
                print(f"  progress {i + 1}/{len(missing)} elapsed={time.time()-started:.0f}s")

    # orphaned pending jobs whose revision/hash no longer matches the node
    with db:
        orphaned = db.execute(
            """
            UPDATE memory_index_jobs
            SET status = 'stale', updated_at = ?, error = 'OrphanedPendingAfterMigration'
            WHERE status = 'pending'
              AND NOT EXISTS (
                  SELECT 1 FROM memory_nodes n
                  WHERE n.node_id = memory_index_jobs.node_id
                    AND n.index_revision = memory_index_jobs.index_revision
                    AND n.content_hash = memory_index_jobs.content_hash
              )
            """,
            (time.time(),),
        ).rowcount
    print(f"orphaned pending jobs marked stale: {orphaned}")

    final = db.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
    coverage = db.execute(
        "SELECT COUNT(*) FROM memory_nodes n WHERE n.is_deleted=0 AND EXISTS (SELECT 1 FROM memory_fts f WHERE f.node_id=n.node_id)"
    ).fetchone()[0]
    live = db.execute("SELECT COUNT(*) FROM memory_nodes WHERE is_deleted=0").fetchone()[0]
    print(f"rebuilt={rebuilt} empty_chunks_skipped={empty} fts_rows={final} coverage={coverage}/{live}")

    # smoke test
    probe = db.execute(
        "SELECT node_id FROM memory_fts WHERE memory_fts MATCH ? LIMIT 3", ("记忆",)
    ).fetchall()
    print(f"match probe rows: {len(probe)}")
    db.execute("PRAGMA quick_check")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
