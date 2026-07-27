"""表情包存储层：候选池（SQLite）+ 图片文件 + 视觉向量库。

设计：图片归图片（文件系统）、向量归向量（ChromaDB）、候选状态归 SQLite，
三者用 meme_id / source_hash 关联。这是处理"图片+向量"多模态存储的标准分离方案。

- 候选池（SQLite）：聊天收到的表情包候选 + 浏览/收藏状态 + 来源溯源
- 图片文件：收藏的表情包按 meme_id 存于 image_dir
- 视觉向量库（ChromaDB）：视觉 embedding + 元数据，供纯视觉检索
"""

from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from src.kernel.logger import get_logger

logger = get_logger("emoji.meme_store")


@dataclass
class MemeCandidate:
    """候选池中的一条表情包候选。"""

    candidate_id: str
    source_hash: str
    source_path: str = ""
    source_stream: str = ""
    source_message_id: str = ""
    mime: str = ""
    status: str = "unreviewed"  # unreviewed / collected / dismissed
    brief: str = ""  # 感知层生成的简短描述（供她浏览时判断）
    note: str = ""  # 她收藏时写的“为什么喜欢”
    collected_meme_id: str = ""
    created_at: float = 0.0
    reviewed_at: float = 0.0


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MemeStore:
    """表情包统一存储：候选池 + 图片库 + 视觉向量库。"""

    def __init__(
        self,
        db_path: str,
        image_dir: str,
        vector_db: Any,
        collection_name: str,
    ) -> None:
        self._db_path = db_path
        self._image_dir = Path(image_dir)
        self._vdb = vector_db
        self._collection = collection_name
        self._initialized = False

    # ── 初始化 ──────────────────────────────────────────────────

    async def initialize(self) -> None:
        if self._initialized:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._image_dir.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS meme_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    source_hash TEXT NOT NULL,
                    source_path TEXT DEFAULT '',
                    source_stream TEXT DEFAULT '',
                    source_message_id TEXT DEFAULT '',
                    mime TEXT DEFAULT '',
                    status TEXT DEFAULT 'unreviewed',
                    brief TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    collected_meme_id TEXT DEFAULT '',
                    created_at REAL DEFAULT 0,
                    reviewed_at REAL DEFAULT 0
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_status ON meme_candidates(status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_hash ON meme_candidates(source_hash)"
            )
            await db.commit()
        await self._vdb.get_or_create_collection(self._collection)
        self._initialized = True

    # ── 候选池（SQLite）──────────────────────────────────────────

    async def has_hash(self, source_hash: str) -> bool:
        """该 hash 是否已在候选池（任意状态），用于来源去重。"""
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT 1 FROM meme_candidates WHERE source_hash = ? LIMIT 1",
                (source_hash,),
            ) as cur:
                return await cur.fetchone() is not None

    async def add_candidate(
        self,
        *,
        source_hash: str,
        source_path: str = "",
        source_stream: str = "",
        source_message_id: str = "",
        mime: str = "",
        brief: str = "",
    ) -> bool:
        """登记一条候选（感知筛选后调用）。hash 已存在则跳过，返回是否新增。"""
        if await self.has_hash(source_hash):
            return False
        now = time.time()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO meme_candidates
                    (candidate_id, source_hash, source_path, source_stream,
                     source_message_id, mime, status, brief, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'unreviewed', ?, ?)
                """,
                (source_hash, source_hash, source_path, source_stream,
                 source_message_id, mime, brief, now),
            )
            await db.commit()
        return True

    async def list_unreviewed(self, limit: int = 8) -> list[MemeCandidate]:
        """列出未浏览的候选（按时间正序）。"""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM meme_candidates WHERE status = 'unreviewed' "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return [self._row_to_candidate(r) for r in rows]

    async def count_unreviewed(self) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM meme_candidates WHERE status = 'unreviewed'"
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_candidate(self, candidate_id: str) -> MemeCandidate | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM meme_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ) as cur:
                row = await cur.fetchone()
        return self._row_to_candidate(row) if row else None

    async def mark_collected(self, candidate_id: str, meme_id: str, note: str = "") -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE meme_candidates SET status='collected', collected_meme_id=?, "
                "note=?, reviewed_at=? WHERE candidate_id=?",
                (meme_id, note, time.time(), candidate_id),
            )
            await db.commit()

    async def mark_dismissed(self, candidate_id: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE meme_candidates SET status='dismissed', reviewed_at=? "
                "WHERE candidate_id=?",
                (time.time(), candidate_id),
            )
            await db.commit()

    @staticmethod
    def _row_to_candidate(row: Any) -> MemeCandidate:
        return MemeCandidate(
            candidate_id=row["candidate_id"],
            source_hash=row["source_hash"],
            source_path=row["source_path"] or "",
            source_stream=row["source_stream"] or "",
            source_message_id=row["source_message_id"] or "",
            mime=row["mime"] or "",
            status=row["status"] or "unreviewed",
            brief=row["brief"] or "",
            note=row["note"] or "",
            collected_meme_id=row["collected_meme_id"] or "",
            created_at=float(row["created_at"] or 0.0),
            reviewed_at=float(row["reviewed_at"] or 0.0),
        )

    # ── 图片文件 ────────────────────────────────────────────────

    def save_image(self, meme_id: str, source_path: str, mime: str = "") -> str:
        """把候选图片复制到图片库，返回存储路径。"""
        self._image_dir.mkdir(parents=True, exist_ok=True)
        ext = self._ext_from_mime(mime) or Path(source_path).suffix or ".png"
        target = self._image_dir / f"{meme_id}{ext}"
        shutil.copy2(source_path, target)
        return str(target)

    @staticmethod
    def _ext_from_mime(mime: str) -> str:
        return {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }.get((mime or "").lower(), "")

    # ── 视觉向量库（ChromaDB）────────────────────────────────────

    async def store_visual(
        self,
        *,
        meme_id: str,
        embedding: list[float],
        source_hash: str,
        image_path: str,
        source_stream: str = "",
        source_message_id: str = "",
        note: str = "",
    ) -> None:
        """把收藏的表情包视觉向量写入向量库。"""
        await self._vdb.get_or_create_collection(self._collection)
        await self._vdb.add(
            collection_name=self._collection,
            ids=[meme_id],
            embeddings=[list(embedding)],
            documents=[note or ""],
            metadatas=[
                {
                    "meme_id": meme_id,
                    "source_hash": source_hash,
                    "path": image_path,
                    "source_stream": source_stream,
                    "source_message_id": source_message_id,
                    "note": note,
                    "collected_at": float(time.time()),
                }
            ],
        )

    async def search_visual(
        self,
        query_embedding: list[float],
        top_n: int = 8,
    ) -> dict[str, list[Any]]:
        """纯视觉检索：用文本意图向量检索表情包图像向量。"""
        await self._vdb.get_or_create_collection(self._collection)
        return await self._vdb.query(
            collection_name=self._collection,
            query_embeddings=[list(query_embedding)],
            n_results=top_n,
        )

    async def is_visual_duplicate(
        self,
        embedding: list[float],
        threshold: float = 0.95,
    ) -> bool:
        """视觉去重：若已存在 cosine >= threshold 的近似图，视为重复。

        ChromaDB 默认返回 L2 距离；对归一化向量，cosine = 1 - dist^2/2。
        """
        try:
            results = await self.search_visual(embedding, top_n=1)
            distances = list(results.get("distances") or [])
            if not distances or not distances[0]:
                return False
            dist = float(distances[0][0])
            cosine = 1.0 - (dist * dist) / 2.0
            return cosine >= threshold
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"视觉去重检查失败（按不重复处理）: {exc}")
            return False

    async def count_collected(self) -> int:
        try:
            return int(await self._vdb.count(self._collection))
        except Exception:  # noqa: BLE001
            return 0
