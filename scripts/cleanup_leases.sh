#!/bin/bash
# 启动前清理残留租约（防启动慢）
# 仅清理"过期 + 本机死进程"的租约行，绝不碰活进程/远端实例的租约。
# 用法：在 main.py 启动前调用；或加入启动脚本。
set -e
cd /root/Elysia/Elysium

export ELYSIUM_MYSQL_PASSWORD="1111"

timeout 30 .venv/bin/python - <<'PY'
import asyncio, asyncmy, os, re
from pathlib import Path

PID_FILE = Path('/tmp/elysium_main_pid')

async def main():
    c = await asyncmy.connect(host='frp-one.com', port=65429, user='elysia',
                              password='1111', database='elysium', connect_timeout=8)
    cur = c.cursor()
    # 列出本机（elysium-linux-primary）未释放的租约
    await cur.execute(
        "SELECT namespace, state_key, owner_instance_id, lease_until "
        "FROM runtime_singleton_writer_claims "
        "WHERE released_at IS NULL "
        "AND owner_instance_id LIKE 'elysium-linux-primary%'"
    )
    rows = await cur.fetchall()
    cleaned = 0
    for ns, key, owner, lease_until in rows:
        m = re.search(r'pid-(\d+)', owner)
        if not m:
            continue
        pid = int(m.group(1))
        alive = os.path.exists(f'/proc/{pid}')
        if alive:
            print(f"SKIP (process alive): {ns}/{key} owner={owner}")
            continue
        # 本机进程已死 → 直接释放（不等待自然过期）
        await cur.execute(
            "UPDATE runtime_singleton_writer_claims SET released_at = UTC_TIMESTAMP(6) "
            "WHERE namespace=%s AND state_key=%s AND released_at IS NULL",
            (ns, key),
        )
        cleaned += cur.rowcount
        print(f"CLEANED (dead pid {pid}): {ns}/{key} lease_until={lease_until}")
    await c.commit()
    print(f"done: cleaned {cleaned} stale leases")
    c.close()

asyncio.run(main())
PY
