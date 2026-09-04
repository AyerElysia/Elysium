import sqlite3
import os

base = "/root/Elysia/Elysium/data/life_engine_workspace/.memory"
print("=== .memory 目录构成 (bytes) ===")
for name in sorted(os.listdir(base)):
    p = os.path.join(base, name)
    try:
        sz = os.path.getsize(p)
    except Exception:
        sz = -1
    print("%14d  %s" % (sz, name))

db = os.path.join(base, "memory.db")
c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
c.execute("PRAGMA query_only=ON")
tabs = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("=== memory.db 表数:", len(tabs), "===")
for t in tabs:
    try:
        n = c.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
    except Exception:
        n = -1
    print("%9d  %s" % (n, t))
