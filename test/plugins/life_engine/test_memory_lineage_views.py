"""``get_lineage_node_views`` 与逐节点读取的等价性契约。

组装记忆包时，每条血缘边原先要打两次数据库：一次 :func:`get_node_by_id`
取节点、一次 :func:`get_snippet` 取摘要。一次召回返回 10 条结果、每条牵出
若干条前后向边，往返次数就是三位数——而这些往返是串行的，每一次都要排队等
同一条连接的语句锁。

批量化只有在**语义完全不变**时才是等价改写。本文件把两条路径跑在同一份数据
上逐节点比对：可见性判定（已删除、路径不再合规）与摘要来源（chunk 优先、
其次 FTS、都没有则空串）必须一字不差。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator

import pytest

from plugins.life_engine.memory.search import (
    get_lineage_node_views,
    get_node_by_id,
    get_snippet,
)

_SCHEMA = """
CREATE TABLE memory_nodes (
    node_id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL,
    file_path TEXT,
    content_hash TEXT,
    title TEXT,
    activation_strength REAL NOT NULL DEFAULT 1.0,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at REAL,
    emotional_valence REAL NOT NULL DEFAULT 0.0,
    emotional_arousal REAL NOT NULL DEFAULT 0.0,
    importance REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    embedding_synced INTEGER NOT NULL DEFAULT 0,
    is_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE memory_chunks (
    node_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (node_id, chunk_index)
);
CREATE TABLE memory_fts (
    node_id TEXT NOT NULL,
    content TEXT NOT NULL
);
"""

# 覆盖 get_lineage_node_views 需要与 get_node_by_id 保持一致的每一条分支：
# 正常可见、摘要回落到 FTS、无任何内容、已删除、路径不再合规、非文件节点。
_NODES: tuple[dict[str, object], ...] = (
    {
        "node_id": "n-visible-chunk",
        "node_type": "file",
        "file_path": "notes/2026/alpha.md",
        "title": "Alpha",
        "is_deleted": 0,
        "chunks": ["第一块内容，摘要应当取它。", "第二块内容，不应被取到。"],
        "fts": "FTS 内容，chunk 存在时不应被取到。",
    },
    {
        "node_id": "n-visible-fts-only",
        "node_type": "file",
        "file_path": "notes/2026/beta.md",
        "title": "Beta",
        "is_deleted": 0,
        "chunks": [],
        "fts": "只有 FTS 有内容。",
    },
    {
        "node_id": "n-visible-empty",
        "node_type": "file",
        "file_path": "notes/2026/gamma.md",
        "title": "Gamma",
        "is_deleted": 0,
        "chunks": [],
        "fts": None,
    },
    {
        "node_id": "n-deleted",
        "node_type": "file",
        "file_path": "notes/2026/deleted.md",
        "title": "Deleted",
        "is_deleted": 1,
        "chunks": ["已删除节点的内容"],
        "fts": None,
    },
    {
        "node_id": "n-noncanonical",
        "node_type": "file",
        "file_path": "./notes/2026/../2026/noncanonical.md",
        "title": "Noncanonical",
        "is_deleted": 0,
        "chunks": ["路径不再合规"],
        "fts": None,
    },
    {
        "node_id": "n-absolute",
        "node_type": "file",
        "file_path": "/etc/passwd",
        "title": "Absolute",
        "is_deleted": 0,
        "chunks": ["工作区外"],
        "fts": None,
    },
    {
        "node_id": "n-concept",
        "node_type": "concept",
        "file_path": "",
        "title": "某个概念",
        "is_deleted": 0,
        "chunks": ["概念节点的内容"],
        "fts": None,
    },
)

_ALL_IDS: tuple[str, ...] = tuple(str(node["node_id"]) for node in _NODES)


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """构造一份覆盖各条可见性分支的内存记忆库。

    Yields:
        sqlite3.Connection: 已建表并写入测试数据的连接。
    """
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)

    now = time.time()
    for node in _NODES:
        connection.execute(
            """
            INSERT INTO memory_nodes (
                node_id, node_type, file_path, content_hash, title,
                activation_strength, access_count, last_accessed_at,
                emotional_valence, emotional_arousal, importance,
                created_at, updated_at, embedding_synced, is_deleted
            ) VALUES (?, ?, ?, '', ?, 1.0, 0, NULL, 0.0, 0.0, 0.5, ?, ?, 0, ?)
            """,
            (
                node["node_id"],
                node["node_type"],
                node["file_path"],
                node["title"],
                now,
                now,
                node["is_deleted"],
            ),
        )
        for index, content in enumerate(node["chunks"]):  # type: ignore[arg-type]
            connection.execute(
                "INSERT INTO memory_chunks (node_id, chunk_index, content) "
                "VALUES (?, ?, ?)",
                (node["node_id"], index, content),
            )
        if node["fts"] is not None:
            connection.execute(
                "INSERT INTO memory_fts (node_id, content) VALUES (?, ?)",
                (node["node_id"], node["fts"]),
            )
    connection.commit()

    yield connection
    connection.close()


async def test_views_match_per_node_reads_exactly(db: sqlite3.Connection) -> None:
    """批量视图的可见集合与每个字段都必须与逐节点读取一致。"""
    views = await get_lineage_node_views(db, _ALL_IDS)

    expected_visible: set[str] = set()
    for node_id in _ALL_IDS:
        if await get_node_by_id(db, node_id) is not None:
            expected_visible.add(node_id)

    assert set(views) == expected_visible
    # 数据本身必须真的覆盖了两侧分支，否则这条断言是空的
    assert expected_visible, "测试数据没有任何可见节点"
    assert set(_ALL_IDS) - expected_visible, "测试数据没有任何不可见节点"

    for node_id in expected_visible:
        node = await get_node_by_id(db, node_id)
        assert node is not None
        view = views[node_id]
        assert view.node_id == node_id
        assert view.file_path == node.file_path
        assert view.title == node.title
        assert view.snippet == await get_snippet(db, node_id)


async def test_snippet_source_precedence_matches_get_snippet(
    db: sqlite3.Connection,
) -> None:
    """摘要来源的优先级：chunk 优先、其次 FTS、都没有则空串。"""
    views = await get_lineage_node_views(db, _ALL_IDS)

    assert views["n-visible-chunk"].snippet.startswith("第一块内容")
    assert views["n-visible-fts-only"].snippet == "只有 FTS 有内容。"
    assert views["n-visible-empty"].snippet == ""


async def test_unknown_and_duplicate_ids_are_handled(
    db: sqlite3.Connection,
) -> None:
    """未知 ID 不出现在结果里，重复 ID 只返回一份。"""
    views = await get_lineage_node_views(
        db,
        ["n-visible-chunk", "n-visible-chunk", "n-does-not-exist", ""],
    )

    assert set(views) == {"n-visible-chunk"}


async def test_empty_input_does_not_touch_the_database(
    db: sqlite3.Connection,
) -> None:
    """空输入必须直接返回，而不是打一次空查询。"""
    assert await get_lineage_node_views(db, []) == {}
