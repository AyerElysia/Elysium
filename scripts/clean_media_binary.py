#!/usr/bin/env python3
"""
全面清理 messages 表中的二进制/base64 数据，只保留文本。

覆盖三种形态：
  A. 裸二进制 content（emoji/voice/image 的纯 base64 字符串，无 JSON 包裹）
  B. dict content 中 media[].data 为 base64 字符串
  C. dict content 中 media[].data 为 dict，含 'base64' 键（视频格式）

清理策略：
  A → 整条 content 替换为 '[removed:binary_<type>]'
  B → media[].data 替换为 '[removed]'
  C → 删除 media[].data 的 'base64' 键，保留 filename/url/size_mb 元数据

安全设计：
  * 形态A 逐行做 base64 特征判定（只取前 512 字符，不加载整条 blob），
    避免误删长文本消息。
  * 使用 keyset 分页（id > last_id），避免行被改短后 OFFSET 漏行。
  * 形态B/C 逐行处理，限制峰值内存。

用法：
    python scripts/clean_media_binary.py --dry-run   # 仅统计
    python scripts/clean_media_binary.py             # 执行清理
    python scripts/clean_media_binary.py --vacuum    # 清理后 VACUUM 回收磁盘
"""
import argparse
import ast
import json
import sqlite3
import sys
from pathlib import Path

B64_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\r\n"
)
MIN_BINARY_LEN = 512


def parse_content(content: str):
    """解析 content，支持标准 JSON 与 Python repr（单引号）两种格式。"""
    try:
        return json.loads(content)
    except Exception:
        pass
    try:
        return ast.literal_eval(content)
    except Exception:
        return None


def looks_binary(prefix: str, tolerance: float = 0.02) -> bool:
    """基于前缀判断是否为 base64/二进制串。prefix 建议取 512 字符。"""
    if not prefix:
        return False
    non_b64 = sum(1 for c in prefix if c not in B64_CHARS)
    return (non_b64 / len(prefix)) <= tolerance


def clean_dict_content(data: dict) -> int:
    """就地清理 dict 中的 media base64，返回节省的字符数。"""
    saved = 0
    media_list = data.get("media")
    if not isinstance(media_list, list):
        return 0

    for m in media_list:
        if not isinstance(m, dict):
            continue
        dat = m.get("data")

        # 形态 B：data 是 base64 字符串
        if isinstance(dat, str) and len(dat) > MIN_BINARY_LEN and looks_binary(dat[:512]):
            saved += len(dat) - len("[removed]")
            m["data"] = "[removed]"

        # 形态 C：data 是 dict，含 base64 键
        elif isinstance(dat, dict) and "base64" in dat:
            b64val = dat.pop("base64")
            if isinstance(b64val, str):
                saved += len(b64val)

    return saved


def scan_form_a(cur, dry_run: bool):
    """扫描并（可选）清理形态A。返回 (命中行数, 节省字节)。"""
    # 先取候选 id + 类型 + 长度 + 前缀（不加载整条 blob）
    cur.execute(
        """
        SELECT id, message_type, LENGTH(content), SUBSTR(content, 1, 512)
        FROM messages
        WHERE SUBSTR(content, 1, 1) != '{' AND LENGTH(content) > ?
        ORDER BY id
        """,
        (MIN_BINARY_LEN,),
    )
    candidates = cur.fetchall()

    hits = []
    for msg_id, mt, length, prefix in candidates:
        if looks_binary(prefix):
            hits.append((msg_id, mt, length))

    saved = 0
    by_type = {}
    for msg_id, mt, length in hits:
        marker = f"[removed:binary_{mt}]"
        saved += length - len(marker)
        st = by_type.setdefault(mt, [0, 0])
        st[0] += 1
        st[1] += length

    print(f"[形态A] 裸二进制 content：命中 {len(hits):,} 行"
          f"（候选 {len(candidates):,} 行，已排除长文本）")
    for mt, (cnt, size) in sorted(by_type.items(), key=lambda x: -x[1][1]):
        print(f"  {mt:8s}: {cnt:6,} 行  {size/1024**2:8.1f} MB")

    if dry_run or not hits:
        return len(hits), saved

    for i, (msg_id, mt, _length) in enumerate(hits, 1):
        cur.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            (f"[removed:binary_{mt}]", msg_id),
        )
        if i % 200 == 0:
            cur.connection.commit()
            print(f"  已清理 {i:,}/{len(hits):,}", end="\r")
    cur.connection.commit()
    print(f"  形态A 完成：{len(hits):,} 行，节省 {saved/1024**2:.1f} MB")
    return len(hits), saved


def scan_form_bc(conn, dry_run: bool):
    """扫描并（可选）清理形态B/C。返回 (命中行数, 节省字节)。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id FROM messages
        WHERE SUBSTR(content, 1, 1) = '{' AND LENGTH(content) > ?
        ORDER BY id
        """,
        (MIN_BINARY_LEN,),
    )
    ids = [r[0] for r in cur.fetchall()]

    hits = 0
    saved = 0
    unparsable = 0
    write_cur = conn.cursor()

    for i, msg_id in enumerate(ids, 1):
        cur.execute("SELECT content FROM messages WHERE id = ?", (msg_id,))
        row = cur.fetchone()
        if not row:
            continue
        content = row[0]
        data = parse_content(content)
        if not isinstance(data, dict):
            unparsable += 1
            continue

        delta = clean_dict_content(data)
        if delta <= 0:
            continue

        try:
            new_content = json.dumps(data, ensure_ascii=False)
        except Exception:
            continue

        hits += 1
        saved += len(content) - len(new_content)

        if not dry_run:
            write_cur.execute(
                "UPDATE messages SET content = ? WHERE id = ?", (new_content, msg_id)
            )
            if hits % 100 == 0:
                conn.commit()

        if i % 200 == 0:
            print(f"  已扫描 {i:,}/{len(ids):,}（命中 {hits:,}）", end="\r")

    if not dry_run:
        conn.commit()

    print(f"\n[形态B/C] dict 内嵌 base64：扫描 {len(ids):,} 行，"
          f"命中 {hits:,} 行，节省 {saved/1024**2:.1f} MB"
          + (f"，无法解析 {unparsable:,} 行" if unparsable else ""))
    return hits, saved


def main():
    parser = argparse.ArgumentParser(description="清理 messages 表二进制/base64 数据")
    parser.add_argument("--dry-run", action="store_true", help="仅统计，不修改")
    parser.add_argument("--vacuum", action="store_true", help="清理后 VACUUM 回收磁盘")
    parser.add_argument("--db-path", default="data/MoFox.db")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"错误: {db_path} 不存在", file=sys.stderr)
        sys.exit(1)

    print(f"数据库: {db_path}  ({db_path.stat().st_size/1024**3:.2f} GB)")
    conn = sqlite3.connect(str(db_path), timeout=300)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*), SUM(LENGTH(content)) FROM messages")
    total, total_bytes = cur.fetchone()
    print(f"总消息: {total:,}  content 总量: {(total_bytes or 0)/1024**2:,.0f} MB\n")

    a_rows, a_saved = scan_form_a(cur, args.dry_run)
    print()
    bc_rows, bc_saved = scan_form_bc(conn, args.dry_run)

    print("\n========= 汇总 =========")
    print(f"  形态A: {a_rows:,} 行，{a_saved/1024**2:.1f} MB")
    print(f"  形态B/C: {bc_rows:,} 行，{bc_saved/1024**2:.1f} MB")
    print(f"  合计: {a_rows + bc_rows:,} 行，{(a_saved + bc_saved)/1024**2:.1f} MB")

    if args.dry_run:
        print("\n[DRY-RUN] 未修改数据库。")
        conn.close()
        return

    if args.vacuum:
        print("\n执行 VACUUM（10GB 库预计 5-15 分钟）...")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
        conn.execute("PRAGMA journal_mode=WAL")
        print("VACUUM 完成")

    conn.close()
    print(f"\n数据库最终大小: {db_path.stat().st_size/1024**3:.2f} GB")


if __name__ == "__main__":
    main()
