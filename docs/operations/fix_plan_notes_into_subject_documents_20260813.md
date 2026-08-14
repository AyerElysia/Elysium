# notes/ 纳入主体文档体系：协作者独立执行手册

> 日期：2026-08-13
> 目标读者：Linux 协作者（`elysium-linux-primary`）——按本手册即可完成 notes/ 数据对接，无需本机协助
> 状态：**本机（Windows）已完成代码改动 + notes/ 迁移，协作者按 §3 执行即可**

## 0. 必读前置条件（不满足会卡住）

1. **代码必须先提交并推送**。本次改动（§4 清单）当前**尚未 commit/push**（2026-08-13 16:28，HEAD=`298179c2`）。
   你 `git pull` 拉不到任何东西之前，以下步骤全部无效。→ **先等本机把代码提交推送，你 pull 到后再继续。**
   （pull 后确认工作区含 `plugins/life_engine/storage/subject_workspace.py` 里出现 `"notes/"` 前缀。）

2. **MySQL 密码**：迁移脚本需要环境变量 `ELYSIUM_MYSQL_PASSWORD`（连接信息在 `config/core.toml [database]`，
   自动读取）。密码向汐汐索取，**不要写进任何文件或 commit**，命令内联传入即可。

3. **本机已完成的动作**（你不需要重复做）：
   - 代码改动：notes/ 已纳入 subject 文档声明范围（6 文件 + 测试，§4）；
   - 本机数据迁移：`notes/relationships/` 下 2 个文件已进入 MySQL（run `notes-backfill-20260813-1625`，
     revision=1、`byte_fidelity=exact_bytes`，本机 sha256 与库内一致，verified=true）。

## 1. 背景

`notes/`（含 `relationships/` 关系档案）是爱莉的第一人称主体语义，与日记同级（AGENTS.md §4.1）。
此前 subject 文档声明只覆盖 SOUL/USER/MEMORY + `diaries/`，`notes/` 从未进入 MySQL：

- 双节点各自持有本地副本，**从不一致、从不互相同步**（`data/` 在 .gitignore 内，也不走 git）；
- 心跳预算内爱莉检索不到 notes/ 内容。

本次修复把 notes/ 纳入主体文档账本（MySQL 版本链 + 本地投影），两边都靠 MySQL 同步。

## 2. 本机迁移结果（你的对比基准）

| 逻辑路径 | revision | sha256 | 字节 |
|---|---|---|---|
| `life_engine_workspace/notes/relationships/xiaoxi_relationship.md` | 1 | `000f95d8fa8171eda064bbb57d1a5fa8b826f5ea092a51fad1d97335854d65a3` | 2348 |
| `life_engine_workspace/notes/relationships/xixi_relationship.md` | 1 | `c9905078f99b57dfda1480b355e305c6885a7f1e73b187cbc5bea5b9a38e08a2` | 14918 |

## 3. 协作者执行步骤（三种情况，按需选）

### 3.0 准备（必做）

```bash
# 在仓库根目录（Elysium）
git pull   # 必须先确认 §0.1 已满足
git status # 应干净（或仅你自己的并行改动）

# 确认你的 notes/ 本地文件
ls -la data/life_engine_workspace/notes/relationships/
sha256sum data/life_engine_workspace/notes/relationships/*.md
```

### 3.1 情况 A：你的 notes/ 内容与本机完全一致（推荐目标）

你的 `sha256sum` 输出与 §2 表完全相同 → **你什么都不用做**。数据已在 MySQL：
- 重启 Elysium 后，本地 `data/life_engine_workspace/notes/` 自动成为 MySQL 投影物化副本；
- 爱莉心跳即可看到 notes/ 内容。

### 3.2 情况 B：你的 notes/ 有本机没有的独有内容（文件不同/更多）

你的 sha256 与 §2 不同，或你有额外的 notes 文件，且确认**你的版本应作为新版本保留**：

```bash
# 先确认差异（只读）
diff <(cat data/life_engine_workspace/notes/relationships/*.md) <(echo "对比 MySQL head 内容")

# 确认无误后，以你的本地内容追加新版本（revision 2+），不覆盖本机已有 head
ELYSIUM_MYSQL_PASSWORD=<密码> uv run python scripts/migrate_life_notes_incremental.py \
  --run-id notes-append-<你的日期> \
  --append
```

- `--append`：当目标 head 已存在且内容不同时，以当前 head 为父版本追加新 revision（CAS 保护）；
- 执行后检查输出：`appended` 列表应有你的文件、`conflicts` 应为空、`verified: true`；
- ⚠️ 不要用 `--append` 强行覆盖你已经确认不需要保留的差异——版本链只追加不覆盖，
  追加的版本会被保留，之后由爱莉/她者评估是否采纳。

### 3.3 情况 C：你本地没有 notes/ 或文件比本机少

说明你的节点本来就没有这些内容 → **什么都不用做**。重启后投影会把 MySQL 的 notes/ 落到你本地。

### 3.4 通用验证（任选，只读）

```bash
# 1) 直接查库（连接信息在 config/core.toml，密码用环境变量）
ELYSIUM_MYSQL_PASSWORD=<密码> uv run python - <<'PY'
import asyncio, asyncmy, os
async def main():
    conn = await asyncmy.connect(host='frp-one.com', port=65429, user='elysia',
                                 password=os.environ["ELYSIUM_MYSQL_PASSWORD"],
                                 database='elysium', charset='utf8mb4',
                                 autocommit=True, connect_timeout=10)
    async with conn.cursor() as cur:
        await cur.execute("SELECT logical_path, revision FROM subject_documents WHERE logical_path LIKE CONCAT('%', 'notes', '%')")
        for row in cur.fetchall():
            print(row)
    conn.close()
asyncio.run(main())
PY
# 期望：2 行（或含你 append 后的 revision 2+），无报错

# 2) 重启后观察：爱莉心跳/对话应能看到 notes/ 关系档案内容；本地文件与 MySQL 一致
```

## 4. 代码改动清单（本机已改，随提交推送）

| 文件 | 改动 |
|---|---|
| `plugins/life_engine/storage/subject_workspace.py` | 声明前缀 + 路径映射 + observer 扫描纳入 `notes/` |
| `plugins/life_engine/storage/migration/subject_copy.py` | 迁移选择范围纳入 `notes/` |
| `plugins/life_engine/memory/continuity_session.py` | 连续性审查辅助源纳入 `notes/`（新增 `note_document` 类型） |
| `scripts/audit_life_subject_shadow.py` | 独立审计脚本同步 `notes/` 范围 |
| `scripts/migrate_life_notes_incremental.py` | **新增**：notes/ 增量迁移脚本（`--append` 追加模式） |
| `docs/operations/life_storage_backend_runbook.md` | §6.8 声明范围描述更新 |
| 测试（3 文件） | subject contract +4、migration 更新、continuity +1 |

回归：subject/continuity 相关 144 passed。**未改 schema、不新增表，无需升级 generation。**

## 5. 常见问题

- **为什么不能跑全量 `migrate_life_subject_documents.py`？** 它要求快照文档数 == 库中 head 数，
  活账本已有 2,016 份 head，必然失败。增量脚本只处理 notes/。
- **append 后本机文件会怎样？** 版本链只追加。本机 projector 遇到你的新版本时，
  若本地文件等于父版本则原子替换为最新，否则保留本地并作为观察追加——不会丢数据。
- **如果我不小心 append 错了？** subject 文档不可覆盖、只追加。保留即可，之后由主体/她者评估，
  不删除历史（AGENTS.md §5.1 不可变历史）。

## 6. 完成后回报

- 执行了哪种情况（A/B/C）；
- 若执行 B：`appended` 的文件路径 + 新 revision + sha256；
- 重启后的观察结果（心跳可见 notes/、无新 ERROR）。
