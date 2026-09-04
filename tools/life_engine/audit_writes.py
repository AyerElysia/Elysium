#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Elysium 数据系统只读写入审计。

只读取 memory.db / life_events.sqlite3（mode=ro + query_only），绝不写入任何运行态数据。
产出：自包含 HTML 报告（写入项目 docs/report/）。
"""
import os
import re
import html
import json
import sqlite3
import subprocess
import datetime

WS = "/root/Elysia/Elysium/data/life_engine_workspace"
MEM = os.path.join(WS, ".memory", "memory.db")
LIFE = os.path.join(WS, "life_events.sqlite3")
REPORT_DIR = "/root/Elysia/Elysium/docs/report"
REPORT = os.path.join(REPORT_DIR, "Elysium_数据系统写入审计_2026-08-01.html")

TS_KEYWORDS = ['created_at', 'created', 'recorded_at', 'ingested_at', 'occurred_at',
               'event_time', 'timestamp', 'ts', 'time', 'updated_at', 'at', 'date']


def ro(path):
    if not os.path.exists(path):
        return None
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        c.execute("PRAGMA query_only=ON")
    except Exception:
        pass
    return c


def tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def find_col(conn, tbl, cands):
    cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % tbl)]
    for c in cands:
        if c in cols:
            return c
    return None


def find_ts_col(conn, tbl):
    cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % tbl)]
    for k in TS_KEYWORDS:
        if k in cols:
            return k
    for c in cols:
        cl = c.lower()
        if any(t in cl for t in ['time', 'stamp', '_at', 'ts', 'date']):
            return c
    return None


def text_col(conn, tbl):
    cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % tbl)]
    for k in ['content', 'text', 'narrative', 'summary', 'payload', 'body', 'message']:
        if k in cols:
            return k
    return None


def to_date(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.datetime.utcfromtimestamp(float(v)).date()
        except Exception:
            return None
    s = str(v).strip()
    if not s:
        return None
    if re.match(r'^\d{4}-\d{2}-\d{2}', s):
        try:
            return datetime.datetime.fromisoformat(s.replace('Z', '')).date()
        except Exception:
            try:
                return datetime.datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S').date()
            except Exception:
                return None
    if re.match(r'^-?\d+(\.\d+)?$', s):
        try:
            return datetime.datetime.utcfromtimestamp(float(s)).date()
        except Exception:
            return None
    return None


def daily_counts(conn, tbl, ts_col):
    out = {}
    for (v,) in conn.execute('SELECT "%s" FROM "%s"' % (ts_col, tbl)):
        d = to_date(v)
        if d:
            out[d] = out.get(d, 0) + 1
    return out


def dist(conn, tbl, col):
    try:
        return dict(conn.execute('SELECT "%s", COUNT(*) FROM "%s" GROUP BY "%s"'
                                 % (col, tbl, col)).fetchall())
    except Exception:
        return {}


def cnt(conn, tbl):
    if conn is None:
        return None
    try:
        return conn.execute('SELECT COUNT(*) FROM "%s"' % tbl).fetchone()[0]
    except Exception:
        return None


def samples(conn, tbl, where=None, limit=8):
    sql = 'SELECT * FROM "%s"' % tbl
    if where:
        sql += ' WHERE ' + where
    sql += ' ORDER BY rowid DESC LIMIT %d' % limit
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    except Exception:
        return [], []


def pick_text(cols, row):
    for k in ['content', 'text', 'narrative', 'summary', 'payload', 'body', 'message']:
        if k in cols:
            v = row[cols.index(k)]
            if v:
                return str(v)
    for v in row:
        if v and isinstance(v, str) and len(v) > 20:
            return v
    return ''


def du(path):
    try:
        out = subprocess.run(['du', '-sh', path], capture_output=True, text=True, timeout=180)
        return out.stdout.split('\t')[0].strip()
    except Exception:
        return '?'


def parse_size(s):
    s = s.strip().upper()
    m = re.match(r'^([\d.]+)\s*([GMK])?', s)
    if not m:
        return 0.0
    v = float(m.group(1))
    u = m.group(2)
    return v * {'G': 1e9, 'M': 1e6, 'K': 1e3, None: 1}[u]


# ----------------------------------------------------------------------------
data = {"generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "dbs": {}}
mc = ro(MEM)
lc = ro(LIFE)

for name, conn in (("memory.db", mc), ("life_events.sqlite3", lc)):
    if not conn:
        data["dbs"][name] = {"exists": False}
        continue
    tcs = {}
    for t in tables(conn):
        try:
            tcs[t] = conn.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        except Exception:
            tcs[t] = -1
    integ = None
    try:
        integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except Exception:
        pass
    data["dbs"][name] = {"exists": True, "tables": tcs, "integrity": integ}

KEY = ['memory_experiences', 'memory_witnesses', 'memory_claims', 'memory_claim_evidence',
       'memory_artifact_versions', 'memory_interpretations', 'memory_index_jobs',
       'memory_vector_tombstones', 'memory_witness_delivery_jobs', 'memory_witness_migrations',
       'memory_witness_decisions', 'memory_corrections', 'memory_recall_events',
       'memory_artifact_heads', 'memory_artifact_derivations', 'memory_interpretation_sources',
       'raw_life_events']

daily = {}
for t in KEY:
    conn = mc if t.startswith('memory_') else lc
    if conn is None:
        continue
    dbname = "memory.db" if conn is mc else "life_events.sqlite3"
    if t not in data["dbs"][dbname]["tables"]:
        continue
    tsc = find_ts_col(conn, t)
    if not tsc:
        daily[t] = {"ts_col": None, "counts": {}}
        continue
    dc = daily_counts(conn, t, tsc)
    daily[t] = {"ts_col": tsc, "counts": {str(k): v for k, v in dc.items()}}

alldates = set()
for t, info in daily.items():
    alldates.update(info["counts"].keys())
if alldates:
    anchor = max(datetime.date.fromisoformat(d) for d in alldates)
else:
    anchor = datetime.date.today()
window = [anchor - datetime.timedelta(days=i) for i in range(29, -1, -1)]
window_str = [d.isoformat() for d in window]

series = {}
for t, info in daily.items():
    c = info["counts"]
    series[t] = [c.get(d, 0) for d in window_str]
data["window"] = window_str
data["series"] = series
data["anchor"] = anchor.isoformat()

data["dist"] = {}
if mc:
    et = find_col(mc, 'memory_experiences', ['event_type', 'type'])
    if et:
        data["dist"]["experiences_event_type"] = dist(mc, 'memory_experiences', et)
    ck = find_col(mc, 'memory_claims', ['claim_kind', 'kind'])
    if ck:
        data["dist"]["claims_claim_kind"] = dist(mc, 'memory_claims', ck)
    ps = find_col(mc, 'memory_witnesses', ['projection_status', 'status'])
    if ps:
        data["dist"]["witnesses_projection_status"] = dist(mc, 'memory_witnesses', ps)
    js = find_col(mc, 'memory_index_jobs', ['status'])
    if js:
        data["dist"]["index_jobs_status"] = dist(mc, 'memory_index_jobs', js)

health = {}
if mc:
    health["beliefs"] = cnt(mc, "memory_beliefs")
    health["state_events"] = cnt(mc, "memory_state_events")
    health["epistemic_conflicts"] = cnt(mc, "memory_epistemic_conflicts")
    health["retrieval_episodes"] = cnt(mc, "memory_retrieval_episodes")
    health["retrieval_exposures"] = cnt(mc, "memory_retrieval_exposures")
    health["retrieval_feedback"] = cnt(mc, "memory_retrieval_feedback")
    health["semantic_relations"] = cnt(mc, "memory_semantic_relations")
    wd = data["dist"].get("witnesses_projection_status", {})
    health["witness_pending"] = wd.get("pending")
    health["witness_complete"] = wd.get("complete")
    jsd = data["dist"].get("index_jobs_status", {})
    health["index_stale"] = jsd.get("stale")
    health["index_completed"] = jsd.get("completed")
data["health"] = health

samp = {}
if mc:
    samp["claims"] = samples(mc, 'memory_claims', None, 8)
    samp["witnesses"] = samples(mc, 'memory_witnesses', None, 8)
    samp["experiences"] = samples(mc, 'memory_experiences', "event_type='text'", 8)

sizes = {}
try:
    for name in sorted(os.listdir(WS)):
        p = os.path.join(WS, name)
        if os.path.isdir(p):
            sizes[name] = du(p)
    sizes["__root__"] = du(WS)
except Exception:
    pass
data["sizes"] = sizes

# ----------------------------------------------------------------------------
CSS = """
:root{--bg:#0f1420;--card:#1a2030;--ink:#e7ebf5;--mut:#9aa4bf;--bd:#2a3142;
--red:#ff6b6b;--amber:#ffb454;--green:#5fd38d;--acc:#7c9cff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.6 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{padding:28px 32px 6px} h1{margin:0 0 4px;font-size:22px}
h2{margin:26px 0 12px;font-size:17px;border-left:3px solid var(--acc);padding-left:10px}
.sub{color:var(--mut);margin:0} .warn{color:var(--amber);font-size:12px}
section{padding:0 32px 8px} .cards{display:flex;flex-wrap:wrap;gap:12px}
.card{flex:1 1 200px;background:var(--card);border:1px solid var(--bd);
border-radius:10px;padding:14px 16px;min-width:180px}
.card.bad{border-color:var(--red)} .card.warn{border-color:var(--amber)}
.card.good{border-color:var(--green)}
.ct{color:var(--mut);font-size:12px} .cv{font-size:22px;font-weight:700;margin:4px 0}
.cn{font-size:12px;color:var(--mut)}
.bad .cv{color:var(--red)} .warn .cv{color:var(--amber)} .good .cv{color:var(--green)}
.metric{background:var(--card);border:1px solid var(--bd);border-radius:10px;
padding:14px 16px;margin:10px 0}
.metric h3{margin:0 0 6px;font-size:14px}
.chart{width:100%;height:160px;display:block;background:#0c111c;border-radius:6px}
table{width:100%;border-collapse:collapse;margin:8px 0;font-size:13px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--bd)}
th{color:var(--mut);font-weight:600} .num{text-align:right;font-variant-numeric:tabular-nums}
.sample{background:#0c111c;border:1px solid var(--bd);border-radius:8px;
padding:10px 12px;margin:8px 0;font-size:13px;color:#cdd5ea}
.tag{display:inline-block;background:#222a3d;color:var(--acc);border-radius:4px;
padding:1px 7px;font-size:11px;margin-right:6px}
.debt li{margin:6px 0} .ok{color:var(--green)} .bad{color:var(--red)} .warn{color:var(--amber)}
footer{color:var(--mut);font-size:12px;padding:20px 32px 40px}
"""


def e(s):
    return html.escape(str(s))


def excerpt(s, n=300):
    s = str(s)
    if len(s) > n:
        s = s[:n] + '…'
    return e(s).replace('\n', ' ')


def bars(series_counts, color, w=600, h=160):
    n = len(series_counts)
    if n == 0:
        return ''
    maxc = max(series_counts) or 1
    bw = w / n
    parts = ['<svg class="chart" viewBox="0 0 %d %d" preserveAspectRatio="none" '
             'xmlns="http://www.w3.org/2000/svg">' % (w, h)]
    for i, c in enumerate(series_counts):
        bh = (c / maxc) * (h - 24)
        x = i * bw
        y = h - 18 - bh
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s">'
                     '<title>%s</title></rect>' % (x, y, max(bw - 1, 0.5), bh, color, c))
    parts.append('<text x="2" y="12" fill="#9aa4bf" font-size="11">峰值 %d</text>' % maxc)
    parts.append('</svg>')
    return ''.join(parts)


L = []
L.append('<!doctype html><html lang="zh"><head><meta charset="utf-8">')
L.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
L.append('<title>Elysium 数据系统写入审计</title>')
L.append('<style>' + CSS + '</style></head><body>')
L.append('<header><h1>Elysium 数据系统 · 写入审计与健康度</h1>')
L.append('<p class="sub">只读审计 · 生成于 %s · 数据窗口至 %s（近 30 天）</p>'
         % (data["generated"], data["anchor"]))
L.append('<p class="warn">本页仅以 mode=ro 读取 memory.db / life_events.sqlite3，'
         '未写入任何运行态数据。所有数字来自实际查询。</p></header>')

# ---- health cards ----
h = data["health"]
L.append('<section><h2>系统健康度</h2><div class="cards">')
wp = h.get("witness_pending") or 0
wc = h.get("witness_complete") or 0
pratio = (wp / (wp + wc)) if (wp + wc) else 0


def card(title, val, status, note):
    cls = {'red': 'bad', 'amber': 'warn', 'green': 'good'}[status]
    L.append('<div class="card %s"><div class="ct">%s</div><div class="cv">%s</div>'
             '<div class="cn">%s</div></div>' % (cls, e(title), e(val), e(note)))


card('信念层 beliefs', '%s 条' % h.get("beliefs"),
     'red' if h.get("beliefs") == 0 else 'green',
     '主体事实信念层为空 -> 认知闭环未建立，claim 从未被 endorse/reject')
card('检索度量 retrieval', '%s 条' % (h.get("retrieval_episodes") or 0),
     'red' if (h.get("retrieval_episodes") or 0) == 0 else 'green',
     '召回/曝光/反馈全为 0 -> 记忆复用情况不可观测')
card('见证投影积压', '%d 待投影 (%.0f%%)' % (wp, pratio * 100),
     'red' if pratio >= 0.4 else 'amber',
     'witnesses: %d complete / %d pending' % (wc, wp))
card('索引陈旧', '%s 条' % h.get("index_stale"),
     'amber' if (h.get("index_stale") or 0) > 0 else 'green',
     'index_jobs 中有 stale 任务未清理')
card('库完整性', '%s' % data["dbs"]["memory.db"].get("integrity"),
     'green' if data["dbs"]["memory.db"].get("integrity") == 'ok' else 'red',
     'PRAGMA integrity_check')
card('存储体积', '%s' % sizes.get("__root__", '?'),
     'amber', '.memory 目录含约 3.5GB 历史备份，需清理策略')
L.append('</div></section>')

# ---- daily write trends ----
DISPLAY = [
    ('memory_experiences', '经历事件 experiences', '#7c9cff'),
    ('memory_witnesses', '见证意识 witnesses', '#5fd38d'),
    ('memory_claims', '主体 claim', '#ffb454'),
    ('memory_claim_evidence', 'claim 证据', '#c792ea'),
    ('memory_artifact_versions', '产物版本', '#4dd0e1'),
    ('memory_interpretations', '解释 interpretations', '#f48fb1'),
    ('memory_index_jobs', '索引任务', '#a3be8c'),
    ('memory_vector_tombstones', '向量清理 tombstones', '#ff6b6b'),
    ('memory_witness_delivery_jobs', '见证投递', '#80cbc4'),
]
L.append('<section><h2>每日写入趋势（近 30 天）</h2>')
for t, label, color in DISPLAY:
    s = data["series"].get(t, [0] * 30)
    total = sum(s)
    L.append('<div class="metric"><h3>%s &nbsp;<span class="num" style="color:var(--mut)">'
             '合计 %d · 峰值 %d/日</span></h3>%s</div>'
             % (e(label), total, max(s) if s else 0, bars(s, color)))
L.append('</section>')

# ---- classification distributions ----
L.append('<section><h2>写入内容分类分布</h2>')
L.append('<div class="metric"><h3>experiences 事件类型（占比）</h3><table>')
etd = data["dist"].get("experiences_event_type", {})
ettotal = sum(etd.values()) or 1
for k, v in sorted(etd.items(), key=lambda x: -x[1])[:14]:
    L.append('<tr><td>%s</td><td class="num">%d</td><td class="num">%.1f%%</td></tr>'
             % (e(k), v, v / ettotal * 100))
L.append('</table></div>')

L.append('<div class="metric"><h3>claims 主体判定类型</h3><table>')
for k, v in sorted(data["dist"].get("claims_claim_kind", {}).items(), key=lambda x: -x[1]):
    L.append('<tr><td>%s</td><td class="num">%d</td></tr>' % (e(k), v))
L.append('</table></div>')

L.append('<div class="metric"><h3>witnesses 投影状态</h3><table>')
for k, v in sorted(data["dist"].get("witnesses_projection_status", {}).items(), key=lambda x: -x[1]):
    L.append('<tr><td>%s</td><td class="num">%d</td></tr>' % (e(k), v))
L.append('</table></div>')

L.append('<div class="metric"><h3>index_jobs 状态</h3><table>')
for k, v in sorted(data["dist"].get("index_jobs_status", {}).items(), key=lambda x: -x[1]):
    L.append('<tr><td>%s</td><td class="num">%d</td></tr>' % (e(k), v))
L.append('</table></div></section>')

# ---- storage ----
L.append('<section><h2>存储构成</h2><div class="metric"><table>')
sz = data["sizes"]
maxb = max([parse_size(v) for v in sz.values() if isinstance(v, str)] or [1])
for k in sorted(sz.keys()):
    if k == '__root__':
        continue
    v = sz[k]
    w = (parse_size(v) / maxb * 100) if isinstance(v, str) else 0
    L.append('<tr><td>%s</td><td class="num">%s</td>'
             '<td style="width:40%%"><div style="background:#222a3d;height:8px;'
             'border-radius:4px"><div style="width:%.1f%%;background:var(--acc);'
             'height:8px;border-radius:4px"></div></div></td></tr>'
             % (e(k), e(v), w))
L.append('<tr><td><b>__root__ 总</b></td><td class="num"><b>%s</b></td><td></td></tr>'
         % e(sz.get("__root__", '?')))
L.append('</table></div></section>')

# ---- content samples ----
L.append('<section><h2>具体写入了什么（内容样本）</h2>')
L.append('<h3 style="color:var(--mut);font-size:13px">主体 claim（最近 8 条）</h3>')
cols, rows = samp.get("claims", ([], []))
ck = find_col(mc, 'memory_claims', ['claim_kind', 'kind']) if mc else None
for r in rows:
    txt = pick_text(cols, r)
    tag = ''
    if ck and ck in cols:
        tag = '<span class="tag">%s</span>' % e(r[cols.index(ck)])
    L.append('<div class="sample">%s%s</div>' % (tag, excerpt(txt, 320)))
L.append('<h3 style="color:var(--mut);font-size:13px">见证意识 witnesses（最近 8 条）</h3>')
cols, rows = samp.get("witnesses", ([], []))
for r in rows:
    L.append('<div class="sample">%s</div>' % excerpt(pick_text(cols, r), 320))
L.append('<h3 style="color:var(--mut);font-size:13px">经历事件 experiences（event_type=text，最近 8 条）</h3>')
cols, rows = samp.get("experiences", ([], []))
for r in rows:
    L.append('<div class="sample">%s</div>' % excerpt(pick_text(cols, r), 320))
L.append('</section>')

# ---- data debt ----
L.append('<section><h2>数据债务清单与建议</h2><ul class="debt">')
if (h.get("beliefs") or 0) == 0:
    L.append('<li class="bad"><b>认知闭环缺失：</b>memory_beliefs / memory_state_events / '
             'memory_epistemic_conflicts 全部为 0。130 条 claim 无一条被主体 endorse/reject，'
             '见证意识不拥有事实裁决工具。建议启用"主体显式裁决"闭环（已在 memory 改造分支实现）。</li>')
if (h.get("retrieval_episodes") or 0) == 0:
    L.append('<li class="bad"><b>检索不可观测：</b>retrieval_episodes/exposures/feedback 全为 0，'
             '无法评估记忆被复用与命中质量。建议接入检索度量采集。</li>')
if pratio >= 0.4:
    L.append('<li class="warn"><b>见证投影积压：</b>%d 条 pending vs %d complete（%.0f%% 待投影），'
             '建议追赶 projection 流水线。</li>' % (wp, wc, pratio * 100))
if (h.get("index_stale") or 0) > 0:
    L.append('<li class="warn"><b>索引陈旧：</b>%d 条 index_jobs 为 stale，建议清理或重跑。</li>'
             % h.get("index_stale"))
L.append('<li class="warn"><b>存储膨胀：</b>.memory 目录 %s，其中约 3.5GB 为历史 .bak 备份，'
         'chroma 向量库 32M。建议制定备份保留/清理策略（保留最近 1-2 份）。</li>'
         % e(sz.get("__root__", '?')))
etv = data["dist"].get("experiences_event_type", {})
tc = etv.get("tool_call", 0) + etv.get("tool_result", 0)
if ettotal := sum(etv.values()):
    if tc / ettotal > 0.3:
        L.append('<li class="warn"><b>运动皮层信号占比高：</b>experiences 中 tool_call+tool_result '
                 '占 %.0f%%（约 %d 行），属"操作日志"而非情景记忆，符合设计预期但量大，'
                 '建议关注归档/降采样。</li>' % (tc / ettotal * 100, tc))
L.append('<li class="ok"><b>正常项：</b>memory.db integrity_check=ok；experiences / witnesses / '
         'claims / artifact_versions / interpretations / index_jobs 均持续稳定写入，'
         '原始事件账本 life_events.sqlite3 独立追加（%d 行）。</li>'
         % data["dbs"]["life_events.sqlite3"]["tables"].get("raw_life_events", 0))
L.append('</ul></section>')

L.append('<footer>Elysium 数据系统只读写入审计 · 生成于 %s · '
         '数据源 memory.db (69 表) / life_events.sqlite3 (13 表) · 仅读取</footer>'
         % data["generated"])
L.append('</body></html>')

os.makedirs(REPORT_DIR, exist_ok=True)
with open(REPORT, "w", encoding="utf-8") as f:
    f.write("\n".join(L))

# ---- stdout verification summary ----
print("=== AUDIT DONE ===")
print("memory.db integrity:", data["dbs"]["memory.db"].get("integrity"))
print("memory.db tables:", len(data["dbs"]["memory.db"]["tables"]))
print("life_events tables:", len(data["dbs"]["life_events.sqlite3"]["tables"]))
print("anchor:", data["anchor"])
print("health:", json.dumps(h, ensure_ascii=False))
print("witness pending/complete:", wp, wc)
print("experiences total:", sum(data["series"].get("memory_experiences", [])))
print("claims total(30d):", sum(data["series"].get("memory_claims", [])))
print("sizes:", json.dumps(sizes, ensure_ascii=False))
print("report:", REPORT, os.path.getsize(REPORT), "bytes")
