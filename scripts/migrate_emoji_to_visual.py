"""存量表情包视觉化迁移脚本。

把旧的 data/emoji_sender/memes/ 里已收藏的表情包，逐张做视觉 embed，
写入新的视觉向量集合（emoji_sender_visual），一图一条，按 hash 去重。
旧的文本向量库保留作回退，不删除。

用法（项目 .venv，且视觉嵌入服务已启动）：
    .venv/bin/python scripts/migrate_emoji_to_visual.py
    .venv/bin/python scripts/migrate_emoji_to_visual.py --endpoint http://127.0.0.1:8848/v1/embeddings
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 确保能 import 项目模块
sys.path.insert(0, str(Path(__file__).parent.parent))

from plugins.emoji.sender.meme_store import MemeStore  # noqa: E402
from plugins.emoji.sender.visual_embedder import VisualEmbedder, VisualEmbedError  # noqa: E402
from src.kernel.vector_db import get_vector_db_service  # noqa: E402

_ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


async def migrate(
    *,
    old_meme_dir: str,
    old_vdb_path: str,
    old_collection: str,
    new_vdb_path: str,
    new_collection: str,
    new_meme_dir: str,
    db_path: str,
    endpoint: str,
) -> None:
    old_dir = Path(old_meme_dir)
    if not old_dir.exists():
        print(f"旧表情包目录不存在: {old_dir}，无需迁移")
        return

    files = [p for p in old_dir.iterdir() if p.is_file() and p.suffix.lower() in _ALLOWED_SUFFIXES]
    print(f"发现 {len(files)} 张已存表情包，开始视觉化迁移 ...")
    if not files:
        return

    store = MemeStore(
        db_path=db_path,
        image_dir=new_meme_dir,
        vector_db=get_vector_db_service(new_vdb_path),
        collection_name=new_collection,
    )
    await store.initialize()
    embedder = VisualEmbedder(endpoint, timeout=60)

    # 尝试从旧向量库读取描述，迁移为 note（可选）
    old_notes = await _load_old_notes(old_vdb_path, old_collection)

    migrated = 0
    skipped = 0
    failed = 0
    for path in files:
        meme_id = path.stem  # 旧库以 hash 命名文件
        try:
            image_bytes = path.read_bytes()
            embedding = await embedder.embed_image_bytes(image_bytes)
            # 新库图片就指向旧文件路径（不重复复制），也可改为复制到 new_meme_dir
            await store.store_visual(
                meme_id=meme_id,
                embedding=embedding,
                source_hash=meme_id,
                image_path=str(path),
                note=old_notes.get(meme_id, ""),
            )
            migrated += 1
            if migrated % 10 == 0:
                print(f"  已迁移 {migrated} 张 ...")
        except VisualEmbedError as exc:
            print(f"  [失败] {path.name}: 视觉嵌入失败 - {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] {path.name}: {exc}")
            failed += 1

    print(f"\n迁移完成: 成功 {migrated} | 失败 {failed} | 跳过 {skipped}")
    print(f"新视觉集合 '{new_collection}' 现有 {await store.count_collected()} 条")


async def _load_old_notes(old_vdb_path: str, old_collection: str) -> dict[str, str]:
    """从旧文本向量库读取 meme_id -> description，作为迁移后的 note。"""
    notes: dict[str, str] = {}
    try:
        vdb = get_vector_db_service(old_vdb_path)
        await vdb.get_or_create_collection(old_collection)
        data = await vdb.get(
            collection_name=old_collection,
            include=["metadatas"],
            limit=100000,
        )
        ids = list(data.get("ids") or [])
        metadatas = list(data.get("metadatas") or [])
        for mid, meta in zip(ids, metadatas):
            # 旧库一张图可能多条（按 tag），取第一个非空 description
            meme_id = str((meta or {}).get("meme_id") or mid).split(":")[0]
            desc = str((meta or {}).get("description") or "").strip()
            if meme_id and desc and meme_id not in notes:
                notes[meme_id] = desc[:120]
    except Exception as exc:  # noqa: BLE001
        print(f"读取旧描述失败（note 将为空）: {exc}")
    return notes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-meme-dir", default="data/emoji_sender/memes")
    parser.add_argument("--old-vdb-path", default="data/emoji_sender/vector_db")
    parser.add_argument("--old-collection", default="emoji_sender")
    parser.add_argument("--new-vdb-path", default="data/emoji_sender/vector_db")
    parser.add_argument("--new-collection", default="emoji_sender_visual")
    parser.add_argument("--new-meme-dir", default="data/emoji/memes")
    parser.add_argument("--db-path", default="data/emoji/meme_candidates.db")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8848/v1/embeddings")
    args = parser.parse_args()

    asyncio.run(
        migrate(
            old_meme_dir=args.old_meme_dir,
            old_vdb_path=args.old_vdb_path,
            old_collection=args.old_collection,
            new_vdb_path=args.new_vdb_path,
            new_collection=args.new_collection,
            new_meme_dir=args.new_meme_dir,
            db_path=args.db_path,
            endpoint=args.endpoint,
        )
    )


if __name__ == "__main__":
    main()
