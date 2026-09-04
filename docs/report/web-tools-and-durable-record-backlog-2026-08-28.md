# 联网工具开放与耐久记录积压诊断（2026-08-28）

## 1. 浏览器 / 搜索工具

### 结论

爱莉没有独立的图形浏览器。公开网页能力是 Tavily Search / Extract，对应工具名是 `nucleus_web_search` 与 `nucleus_browser_fetch`。

此前生产 `WEB_TOOLS` 只注册了 `nucleus_web`。该 dispatcher 的 `execute` 使用 `**kwargs`，公共 schema 生成器会丢掉可变参数，模型只能看到 `action`，无法传入 `url` / `query`。聊天清单仍列出旧名，过滤后两边对不上，聊天里等于没有可用的网页工具。

### 已做修改

- `WEB_TOOLS` 重新注册 `LifeEngineWebSearchTool` 与 `LifeEngineBrowserFetchTool`。
- `nucleus_web` 保留为程序内转发，不向 LLM 注册。
- 聊天主工具文案补上这两个名字，并与 chat 意识清单对齐。
- 架构说明见 [生命引擎核心](../architecture/生命引擎核心.md) §3.7 与 [意识实例架构](../architecture/意识实例架构.md) §6。

### 验证

- 定向测试：`test_web_tools.py`、`test_tool_manifests.py` 相关契约通过（含 schema 含 `url`/`query`、chat 清单覆盖 `WEB_TOOLS`）。
- 用生产 `[web]` Tavily 配置对 `https://example.com` 做 extract、对公开查询做 search：两次都成功。未把密钥或正文写入本报告。
- 当前正在运行的 Elysium 进程仍是修改前加载的组件表。聊天要真正看到这两个工具，需要用户手动重启 Elysium。本任务没有启动或停止该进程。

## 2. 主动任务、认知、注意力是否“堆积无法消费”

先把两类东西分开：

- **主体耐久记录**：她明确 open 的 AttentionThread、initiative seed、洞察账本。代码不得因为数量多就自动关掉或丢掉。心跳里最多投影一页线索，不是后台任务队列。
- **工程消费队列**：反思入队、Life Event 消费游标、记忆索引 outbox。这些必须能排空，积压应出现在 health 里。

### 只读快照（2026-08-28，`mode=ro`）

权威库 `data/life_storage/local.sqlite3`：

| 对象 | 数量 | 是否“无法消费的堆积” |
|---|---|---|
| `attention_thread_heads` / events / focus | 全 0 | 否。没有未闭合线索 |
| `life_initiative.seed_heads` | 0 | 否。没有已注册未释放的主动种子 |
| `life_epistemic.opportunities` 事件 | 361 | 否。这是候选历史账本；当前投影只有一份 `life_epistemic.projection`，不是 361 个待办 |
| 学习洞察 | 439 | 否。这是知识投影，不是待处理作业 |
| 待审实验 | 0 | 否 |
| 反思队列 `pending_reflections_v1` | 16 / 上限 512 | 否。游标 `event_cursor = event_frontier = 17157`，连续失败 0，最近成功约 05:55 +08。这是有界在制品，不是顶满后丢弃经历 |
| `stream_turns` / `operations` | 0 | 否。没有卡住的外联表达 turn |

`data/life_engine_workspace/life_events.sqlite3`：

| 消费者 | 游标 | 相对 max `ingest_position` 225962 |
|---|---|---|
| `memory_experience_ingest:v1`（当前见证管线 raw ingest） | 225948 | 落后 **14**，属正常在制品 |
| `memory_witness` | 109294 | 数字差约 11.7 万。当前代码推进的是 `memory_experience_ingest:v1`，这条旧消费者名不再是 live owner。不能把它当成正在增长的见证积压 |

`data/life_engine_workspace/.memory/memory.db`：

| `memory_index_jobs` | 数量 |
|---|---|
| completed | 4581 |
| stale | 759 |
| pending | 0（本次 GROUP BY 未出现） |
| total | 5340 |

759 条 `stale` 是**向量索引投影** outbox 残留，影响检索可达性，不改变权威事实，也不是她注册的主动任务。health 把 stale 算进 backlog；这是工程投影问题，不是主体线索爆炸。

### 总判断

没有出现“主动任务 / 注意力线索 / 认知候选大量登记后无人消费、队列顶死”的情况。注意力和主动种子当前是空的。认知机会与洞察是耐久历史或知识投影，心跳只给有界投影，不要求她逐条做完。真正需要盯的工程项是记忆索引的 759 条 stale job，以及重启后确认聊天进程已加载 `nucleus_browser_fetch` / `nucleus_web_search`。
