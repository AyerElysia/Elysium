#!/usr/bin/env python3
"""
清理 Elysium.db 中 images 表的重复 image_id 记录
规则：当 image_id 重复时，保留 path 包含 'data\media_cache\images' 的记录，删除其他 
"""

import sqlite3
import sys
from pathlib import Path


def clean_duplicate_images(db_path: str, dry_run: bool = True) -> dict:
    """
    清理 images 表中的重复记录

    Args:
        db_path: 数据库文件路径
        dry_run: 如果为 True，只预览不实际删除

    Returns:
        统计信息字典
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    stats = {
        "total_rows_before": 0,
        "duplicate_groups": 0,
        "rows_to_delete": 0,
        "rows_deleted": 0,
        "errors": [],
    }

    try:
        # 获取总行数
        cursor.execute("SELECT COUNT(*) FROM images")
        stats["total_rows_before"] = cursor.fetchone()[0]
        print(f"[INFO] 当前 images 表总行数: {stats['total_rows_before']}")

        # 查找所有重复的 image_id
        cursor.execute(
            """
            SELECT image_id, COUNT(*) as cnt 
            FROM images 
            GROUP BY image_id 
            HAVING cnt > 1
        """
        )
        duplicates = cursor.fetchall()
        stats["duplicate_groups"] = len(duplicates)

        if not duplicates:
            print("[OK] 没有发现重复的 image_id")
            return stats

        print(f"\n[INFO] 发现 {len(duplicates)} 个重复的 image_id")
        print("=" * 60)

        rows_to_delete = []

        for image_id, count in duplicates:
            # 获取该 image_id 的所有记录
            cursor.execute(
                """
                SELECT id, image_id, path, type, description 
                FROM images 
                WHERE image_id = ?
            """,
                (image_id,),
            )
            rows = cursor.fetchall()

            print(f"\n[GROUP] image_id: {image_id} (共 {len(rows)} 条)")

            # 找出应该保留的记录（path 包含 media_cache\images）
            keep_row = None
            delete_candidates = []

            for row in rows:
                row_id, img_id, path, img_type, desc = row
                # 检查 path 是否包含 media_cache\images
                is_valid_path = (
                    r"data\media_cache\images" in path
                    or "data/media_cache/images" in path
                )

                status = "[KEEP] 保留" if is_valid_path else "[DEL] 删除"
                print(f"   ID={row_id}, type={img_type}")
                print(f"   path={path}")
                print(f"   -> {status}")

                if is_valid_path and keep_row is None:
                    keep_row = row
                else:
                    delete_candidates.append(row_id)

            # 如果没有找到有效路径的记录，保留第一个，删除其余
            if keep_row is None and rows:
                keep_row = rows[0]
                delete_candidates = [r[0] for r in rows[1:]]
                print(f"   [WARN] 未找到标准路径，保留 ID={keep_row[0]}")

            rows_to_delete.extend(delete_candidates)
            stats["rows_to_delete"] += len(delete_candidates)

        print("\n" + "=" * 60)
        print("\n[SUMMARY] 统计信息:")
        print(f"   重复组数: {stats['duplicate_groups']}")
        print(f"   待删除行数: {stats['rows_to_delete']}")
        print(
            f"   预计剩余行数: {stats['total_rows_before'] - stats['rows_to_delete']}"
        )

        if dry_run:
            print(
                "\n[DRY-RUN] 预览模式 - 以上记录将被删除，使用 --execute 参数执行实际删除"
            )
        else:
            print("\n[EXEC] 正在执行删除...")
            for row_id in rows_to_delete:
                try:
                    cursor.execute("DELETE FROM images WHERE id = ?", (row_id,))
                    stats["rows_deleted"] += 1
                except Exception as e:
                    stats["errors"].append(f"删除 ID={row_id} 失败: {e}")

            conn.commit()

            # 验证结果
            cursor.execute("SELECT COUNT(*) FROM images")
            final_count = cursor.fetchone()[0]
            print("[OK] 删除完成！")
            print(f"   实际删除: {stats['rows_deleted']} 行")
            print(f"   最终行数: {final_count}")

            if stats["errors"]:
                print(f"\n[ERR] 错误 ({len(stats['errors'])}):")
                for err in stats["errors"][:5]:
                    print(f"   {err}")

        return stats

    except Exception as e:
        print(f"[ERR] 错误: {e}")
        stats["errors"].append(str(e))
        return stats
    finally:
        conn.close()


def main():
    # 默认数据库路径
    db_path = Path("data/Elysium.db")

    # 检查命令行参数
    dry_run = True
    if len(sys.argv) > 1:
        if sys.argv[1] in ("--execute", "-e", "--yes", "-y"):
            dry_run = False
        elif sys.argv[1] in ("--help", "-h"):
            print(
                """
用法: python clean_duplicate_images.py [选项]

选项:
  --execute, -e    执行实际删除（默认只预览）
  --help, -h       显示帮助信息

说明:
  清理 images 表中 image_id 重复的记录。
  当 image_id 重复时，保留 path 包含 'data\\media_cache\\images' 的记录，
  删除其他重复记录。
  默认以预览模式运行，不会实际删除数据。
            """
            )
            return

    if not db_path.exists():
        print(f"[ERR] 数据库文件不存在: {db_path}")
        print("请确认路径正确，或修改脚本中的 db_path 变量")
        return

    print(f"[INFO] 数据库: {db_path.absolute()}")
    print(f"[INFO] 模式: {'预览' if dry_run else '执行'}")
    print("-" * 60)

    stats = clean_duplicate_images(str(db_path), dry_run=dry_run)

    if dry_run and stats["rows_to_delete"] > 0:
        print("\n[HINT] 提示: 运行以下命令执行实际删除:")
        print("   python clean_duplicate_images.py --execute")


if __name__ == "__main__":
    main()
