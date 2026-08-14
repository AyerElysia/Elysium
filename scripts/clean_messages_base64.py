#!/usr/bin/env python3
"""清理 messages 表中的 base64 数据。

该脚本用于清理历史消息中的 base64 数据，回收数据库空间。
新消息已通过 stream_manager._sanitize_content() 自动清理。

用法:
    # 仅统计，不修改
    python scripts/clean_messages_base64.py --dry-run

    # 执行清理
    python scripts/clean_messages_base64.py

    # 清理后执行 VACUUM 回收空间（会锁库较长时间）
    python scripts/clean_messages_base64.py --vacuum
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def sanitize_content(content: str) -> str:
    """清理 content 中的 base64 数据。"""
    import ast
    import json

    # 先尝试标准 JSON（双引号）
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        # 尝试解析单引号格式的 Python dict 字符串
        try:
            data = ast.literal_eval(content)
        except (ValueError, SyntaxError):
            return content

    if not isinstance(data, dict):
        return content

    changed = False

    # 处理 media 字段
    if "media" in data and isinstance(data["media"], list):
        for media in data["media"]:
            if isinstance(media, dict) and "data" in media:
                media_data = media["data"]
                if isinstance(media_data, str) and len(media_data) > 500:
                    media["data"] = "[removed]"
                    changed = True

    # 处理 base64 字段（视频消息格式）
    if "base64" in data and isinstance(data["base64"], str):
        if len(data["base64"]) > 500:
            data["base64"] = "[removed]"
            changed = True

    if not changed:
        return content

    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return content


def main():
    parser = argparse.ArgumentParser(description="清理 messages 表中的 base64 数据")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅统计，不修改数据库",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="清理后执行 VACUUM 回收空间（会锁库较长时间）",
    )
    parser.add_argument(
        "--db-path",
        default="data/Elysium.db",
        help="数据库路径（默认: data/Elysium.db）",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"错误: 数据库文件不存在: {db_path}")
        sys.exit(1)

    print(f"数据库路径: {db_path}")
    print(f"数据库大小: {db_path.stat().st_size / 1024 / 1024 / 1024:.2f} GB")
    print()

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # 统计总消息数
    cursor.execute("SELECT COUNT(*) FROM messages")
    total_count = cursor.fetchone()[0]
    print(f"总消息数: {total_count:,}")

    # 统计包含 base64 的消息数
    cursor.execute("SELECT COUNT(*) FROM messages WHERE content LIKE '%base64%'")
    base64_count = cursor.fetchone()[0]
    print(f"包含 base64 的消息: {base64_count:,}")

    # 统计各类型消息
    cursor.execute("""
        SELECT message_type, COUNT(*), SUM(LENGTH(content))
        FROM messages
        GROUP BY message_type
        ORDER BY SUM(LENGTH(content)) DESC
    """)
    print("\n各类型消息统计:")
    total_size = 0
    for msg_type, count, size in cursor.fetchall():
        size_mb = size / 1024 / 1024
        total_size += size
        print(f"  {msg_type:10s}: {count:6,} 条, {size_mb:8.2f} MB")
    print(f"  {'总计':10s}: {total_count:6,} 条, {total_size / 1024 / 1024:8.2f} MB")

    if args.dry_run:
        print("\n[DRY-RUN] 仅统计，未修改数据库")
        conn.close()
        return

    print("\n开始清理...")

    # 分批处理，避免内存溢出
    batch_size = 1000
    offset = 0
    cleaned_count = 0
    saved_bytes = 0

    while True:
        cursor.execute(
            "SELECT id, content FROM messages WHERE content LIKE '%base64%' LIMIT ? OFFSET ?",
            (batch_size, offset),
        )
        rows = cursor.fetchall()

        if not rows:
            break

        for msg_id, content in rows:
            original_len = len(content)
            sanitized = sanitize_content(content)

            if len(sanitized) < original_len:
                cursor.execute(
                    "UPDATE messages SET content = ? WHERE id = ?",
                    (sanitized, msg_id),
                )
                cleaned_count += 1
                saved_bytes += original_len - len(sanitized)

        conn.commit()
        offset += batch_size
        print(f"  已处理: {min(offset, base64_count):,}/{base64_count:,}", end="\r")

    print(f"\n\n清理完成:")
    print(f"  清理消息数: {cleaned_count:,}")
    print(f"  节省空间: {saved_bytes / 1024 / 1024:.2f} MB")

    if args.vacuum:
        print("\n执行 VACUUM 回收空间（可能需要几分钟）...")
        conn.execute("VACUUM")
        print("VACUUM 完成")

    conn.close()

    # 显示最终大小
    print(f"\n数据库最终大小: {db_path.stat().st_size / 1024 / 1024 / 1024:.2f} GB")


if __name__ == "__main__":
    main()
