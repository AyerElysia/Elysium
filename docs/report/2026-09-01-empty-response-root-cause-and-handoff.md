# 2026-09-01 空响应根因定位与修复 —— 工作交接

> 本侧（WorkBuddy 助手）当日工作记录。供 Codex 线与用户同步认知。
> 重点：**本日三次推翻自己的中间结论**，凡引用旧结论处请以本文为准。

---

## 1. 结论速览

| 问题 | 结论 |
|---|---|
| 爱莉不回复的直接原因 | `life_chatter` 的 LLM 请求被判 `LLMEmptyResponseError`，降级链切换模型 |
| **真因** | `qwen3.8-flash` 的**流式分块密度过高**（单次响应 ~15 个片段，gemini 仅 1 个），逐块消费对 event loop 延迟极敏感 |
| qwen 失败率 | **70%（23/33）**，其余模型 1.1%–7.3% |
| 上游是否有问题 | **否**。qwen 的 23 次判空中仅 2 次上游真的 `completion_tokens=0` |
| 已修复 | `[models."qwen3.8-flash"] stream = false`，qwen **保留为首选** |
| 验证状态 | ⚠️ **未验证**（爱莉主动休息至 19:51，期间无消息） |

---

## 2. 三次自我推翻（重要，避免重复踩坑）

### 推翻一：「witness 落后 11.6 万条是写锁元凶」
- 依据来自 `raw_event_consumer_offsets`，但**该表两行都是冻结快照**：
  `memory_witness` 停在 **2026-08-12**、`memory_experience_ingest:v1` 停在 **2026-08-23**。
- `memory_witness` 行 `metadata={"witness_state_mirror": true}`，**无任何现行代码写入**，是 8-12 迁移遗留的孤儿镜像。
- **真实游标**在 `memory.db` 的 `memory_witness_state.last_sequence`：实测每轮恰好 +40，
  `last_error=''`，真实落后约 6.3 万条，吞吐 480/h vs 产生 74/h，**正在收敛**。
- ⇒ 据此提出的"停掉 memory_witness"方案**已撤销**，那会停掉一个自愈中的健康功能。

### 推翻二：「event_bus 超时导致空响应」
- 我把 `message_collector` 的 facts/context 后台化后，event_bus 超时
  **26 次/21 分钟 → 0**，但空响应比率**仍是 1.0 次/条，改动前后完全一致**。
- 此前"每次空响应前必有超时"是**相关性不是因果**——当时超时太密，往前翻总能找到。
- ⇒ 该改动**保留**（消除了事件超时与静默丢弃，本身有价值），但**不是空响应的解药**。

### 推翻三：「长 prompt / thinking / 工具调用导致 qwen 失败」
四轮直连探测全部无法复现失败：

| 假设 | 验证 | 结果 |
|---|---|---|
| prompt 长度 | 5k / 20k / 60k 分级 | 全正常，reasoning=0 |
| thinking 参数 | disabled / enabled / 不传，各 3 轮 | 全正常（正文 34–85 字） |
| 工具调用 | 带 2 个工具 + parallel_tool_calls | 全正常 |
| event loop 阻塞 | 超时归零后空响应照旧 | 不成立 |

---

## 3. 真因证据

**分块密度**（同等内容）：
```
非流式:  2.5s   1 次返回
流式:    4.6s   24 个 data 片段   ← 24 倍事件调度次数
```
横向对比：qwen 单次 **15 片段** vs gemini **1 片段**（约 15 倍）。
其余模型多返回 `tool_call` 大块结构，一次传完；qwen 逐字流式，每块都要 event loop 调度一轮。

**佐证**：
- 失败请求生成速率 ≈ **15 tok/s**，成功 ≈ **34 tok/s**（传输被拖慢）
- 同等 token 数：qwen **13.3s** vs gemini **4.76s**

---

## 4. 已实施改动（**均未提交**）

### A. `config/models.toml`
```toml
[models."qwen3.8-flash"]
...
# 2026-09-01: 该模型流式分块约为同任务其他模型的 15 倍，逐块消费对
# event loop 延迟极敏感，实测失败率 70%（其余模型 1%-7%），且 23 次判空
# 中仅 2 次上游真的没生成内容。改为非流式一次性取回完整响应。
stream = false
```
`tasks.expression` 顺序已恢复为 `qwen3.8-flash` 第 0 位（中途为验证曾降到第 7，已回退）。

### B. `plugins/life_engine/service/core.py`（+79 −5）
`record_message` 的 facts（2.0–4.6s）与 context（0.2–2.8s）移出同步事件路径：
- 新增 `self._message_persist_lock`
- 用项目既有 `get_task_manager().create_task(daemon=True)` + `asyncio.Lock` 串行执行
- **异常显式 WARNING**（后台化后无法向上冒泡，否则静默丢数据）
- **`enqueue`（0.00s）保持同步**，消息不会丢
- 开关：`storage.message_persist_async = false` 可一键回退（默认启用）
- 效果：event_bus 超时 26次/21分钟 → **0**

### C. `README.md` 已提交
`fe8731b3` — TTS 支持多后端路径，修正与架构文档的自相矛盾表述。

---

## 5. 待办与未解

| 项 | 状态 |
|---|---|
| **验证非流式方案** | ⚠️ 阻塞中：爱莉主动休息至 19:51，期间无消息；自动注入因 401 未走通 |
| 提交 A、B 两处改动 | 待验证通过后，分别单独提交（**严禁 `git add -A`**） |
| `facts 2.0-4.6s` 确切来源 | **仍未定位**。已排除：磁盘 IO（同步写中位 4.1ms）、`read_since`（0.0ms）、`MIN/MAX`（12ms）、远端 MySQL、witness 锁竞争、`publish_many` 下游 handler |
| `world_projection_changes.ingest_position` UNIQUE 冲突 | 既有问题（改动前 3 次/改动后 4 次，个位数/天），待查 |
| `memory_witnesses.projection_status` pending 3571 | 子代理新发现的**第二条积压线**，reconciliation 游标停在 8-20 而 frontier 已到 9-01 |
| witness 投递无死信 | `witness_pipeline.py:801` 只增 `attempt_count`，认领查询 `:736-752` 从不读它 |
| 重试无退避 | `memory_witness.py:541/578/609` 固定 60s |

---

## 6. 写给 Codex 的注意事项

1. **不要用 `raw_event_consumer_offsets` 判断任何 consumer 的落后量**，两行都是冻结快照。
2. **不要用 new-api 日志的 `content` 字段判断响应是否有内容**——它在成功请求上也是空字符串。
3. **不要用 `skipped_cooling=[]` 判断冷却是否失效**——`failover.py` 的
   `choose_index(start=self._idx+1)` 只扫当前索引之后的候选，刚失败的模型不在扫描范围内。
4. **远端 MySQL（`frp-one.com:65429`）是死配置**：`ss -tnp | grep 65429` 为 0 连接，
   `backend="local"` 生效。`core.py` 中"双实例共享 MySQL 竞争"的注释**已过时**。
   该地址握手延迟 61.5ms（本地 0.3ms，**200 倍**），切 `backend=mysql` 前必须先解决。
5. **重启 Elysium 需手动拉起**：`run_elysium.sh` 是 one-shot，不会自动重启。
   流程：`kill -INT <pid>`（约 12–15s 退出）→
   `tmux -L elysium send-keys -t elysium "/root/Elysia/Elysium/run_elysium.sh" Enter`。

---

## 7. 附：inbound inject API（供日后做不打扰验证）

- `POST /api/v1/chat/messages:inject`（`src/app/api/v1/inbound_messages.py:58`）
  文档明确：**回复回到应用侧，不直接对外发送，不依赖平台 Adapter** → 不打扰用户。
- HTTP 服务 `127.0.0.1:18000`（仅本地）。
- 认证 `POST /api/v1/auth/sessions`，body：
  `{grant_type:"service_credential", service_credential:<secret>, audience:<枚举>}`
  `audience` 仅接受 `elysium-user-frontend` / `elysium-admin-frontend` / `elysium-platform-service`。
- ⚠️ 项目根 `elysia_credential.json` 校验返回 **401**；凭证库在
  `runtime/app_api_v1/auth.sqlite3`，需从该库取有效凭据才能自动注入。

---

## 8. 补记：witness 投影积压核实（19:40 实测，比初判更严重）

`data/life_engine_workspace/.memory/memory.db` 只读查询结果：

| 项 | 值 |
|---|---|
| `memory_witnesses` `pending` | **3570** |
| `memory_witnesses` `complete` | 3234 |
| `completed_projection:v1` 游标 `cursor_order_value` | **2026-08-19T14:21:09** |
| `completed_projection:v1` 前沿 `frontier_order_value` | **2026-09-01T19:32:45** |
| `projection_filesystem:v1` `cycle_started_at` | 2026-08-19T04:55:04 |
| `projection_filesystem:v1` `last_completed_at` | **空（从未完成过）** |
| `legacy_pending_projection:v1` `last_completed_at` | 2026-09-01T19:37:53（在跑） |

**判读**：
- 见证文本**在生成**（`memory_witness_state.last_sequence` 每轮 +40），但
  **投影文件导出卡在 8-19**，游标落后前沿 **13 天**。
- 约 **52% 的见证（3570/6804）停留在 pending**，未落到可检索的投影。
- `projection_filesystem:v1` 自 8-19 启动后 `last_completed_at` 始终为空，
  revision 仅 1 —— 疑似卡死或极慢，需单独排查。

**影响**：不阻塞对话（不在 expression 关键路径），但影响生命记忆的可检索性。
**这是 witness 目前最值得跟进的问题**，优先于第 5 节中其余 witness 项目。

---

## 9. ⚠️ 协作风险：`config/` 整个目录不入库

**发现（19:58 提交时暴露）**：
- `.gitignore:120` 规则为 **`/config/*`** —— 整个 config 目录被忽略。
- 版本库里只有 `config/*.toml.example` 模板：
  `core.toml.example`、`elysium.toml.example`、`models.toml.example`。
- 因此本次 `config/models.toml` 的两处改动（qwen 非流式、expression 顺序）
  **无法提交，也不会同步给 Codex 线或其他部署**。

**模板已严重过时**：
`models.toml.example` 的 `[tasks.expression]` 为
```toml
models = ["deepseek-v4-flash", "gpt-5.6-luna", "gpt-5.6-terra",
          "gpt-5.6-sol", "gemini-3.5-flash", "MiMo-V2.5", "claude-sonnet-5"]
```
**其中根本没有 `qwen3.8-flash`**，与线上实际使用的 `models.toml` 差异极大。

**影响**：
1. 任何人基于 example 部署，得到的模型链路与线上完全不同。
2. 本次修复只存在于本机 `config/models.toml`；换机/重装即失效。
3. Codex 线若不知道这一点，可能会基于 example 做无效排查或错误改动。

**建议（待用户决策，本侧未擅自修改）**：
- 方案一：把 example 更新为与线上一致（改动大，但一劳永逸）。
- 方案二：在 example 顶部加注"本文件已过时，实际配置见部署机 config/"，
  并把本次修复写进 changelog。
- 方案三：维持现状，依靠本文档传递配置变更。

**本次已提交的代码改动**（不受影响，已入库）：
- `b348143e` perf(life_engine): 消息收集的慢阶段移出同步事件路径
- `fe8731b3` docs(readme): TTS 支持多后端路径

---

## 10. 最终结论（20:20）——第四次自我推翻：投影冲突**不是**回归

### 事实
正确统计（只匹配以时间戳开头的日志条目）：
```bash
awk "/^2026-09-01T[0-9][0-9]:/ && /IntegrityError/ {print substr($0,12,8)}" \
    logs/elysium-2026-09-01.log | sort | uniq -c
```
| 指标 | 全天实际 | 此前误报 |
|---|---|---|
| `IntegrityError` | **1 次**（02:29:06，凌晨） | "2 分钟内 6 次" |
| `PersistenceError` | **0 次** | "4 次" |

**误报原因（方法论教训）**：
1. 多行 Traceback 的**续行**同样被 `grep` 命中，同一次异常被重复计数。
2. `grep -n` 得到的是**全文件行号**，再 `sed -n "${n}p"` 会对过滤后的结果错位取行。

⇒ **后台化改动没有引入任何回归**。20:09:32 重启后 10 分钟内四项指标全为 0。

### ⚠️ 提交信息更正说明
`b897a66e` 的提交信息写的是"修复后台化引入的并发回归"——**该前提已被证伪**。
为免 rebase 风险，历史**未修改**，此处更正：
- 该提交添加的 `self._world_projection_lock` 属**防御性改动，非必需**。
- `79b0fca1`（gateway 层锁）同理：覆盖 `core.py:2502` / `3061` 这两个绕过
  service 层的入口，逻辑上更完备，但**都无实测收益**。
- 两者均无害，保留。

### 最终状态（20:09:32 重启后 10 分钟）
```
空响应 0 | event_bus 超时 0 | IntegrityError 0 | PersistenceError 0
witness last_sequence=163217  last_error=''
```

### 今日提交（本侧）
| commit | 说明 | 性质 |
|---|---|---|
| `79b0fca1` | 投影追赶串行化下沉到 gateway 层 | 防御性，无实测收益 |
| `b897a66e` | service 层投影锁 | 防御性，无实测收益（前提已证伪） |
| `b348143e` | 消息慢阶段移出同步事件路径 | **有实测收益**：event_bus 超时 26次/21分钟 → 0 |
| `fe8731b3` | README TTS 多后端文案 | 文档 |
| — | `config/models.toml` qwen 非流式 | **核心修复**，但**不入库**（见第 9 节） |
