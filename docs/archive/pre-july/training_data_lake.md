# Training Data Lake

Neo-MoFox 的 text-only LLM 轨迹数据基础设施，用于未来的后训练（SFT、Agent Trajectory、偏好对）。

---

## 目录结构

```
data/training_data_lake/
├── raw/                     # 在线落盘：UTC 日期分片 YYYY-MM-DD.jsonl（append-only）
├── processed/               # 预留：归一化/清洗后的数据
├── export/                  # 导出：后训练用的格式化数据集
├── archive/                 # 归档：迁移来的历史数据 + 旧 metrics 备份
│   ├── messages_migration_YYYY-MM-DD.jsonl
│   ├── life_events_migration_YYYY-MM-DD.jsonl
│   ├── llm_metrics_migration_YYYY-MM-DD.jsonl
│   └── legacy_llm_metrics/  # 原始 llm_metrics.json + corrupt 备份（只读，已迁移）
├── .schema/
│   └── trajectory.v1.json   # schema 元数据
└── README.md
```

---

## 数据格式（schema v1）

每条记录是一行独立 JSON（JSONL），核心字段：

| 字段 | 说明 |
|------|------|
| `schema_version` | 恒为 `1` |
| `trace_id` | 同一次对话/任务的跨 attempt 共享 ID |
| `request_id` | 本次 `LLMRequest.send()` 的唯一 ID |
| `attempt_id` | 本次模型调用尝试的 ID |
| `parent_attempt_id` | failover 切换时上一次 attempt 的 ID |
| `timestamp` | ISO-8601 UTC，带 `Z` 后缀 |
| `request_name` | 业务语义标签（`life_chatter`、`life_engine_heartbeat`、…） |
| `task_tags` | 从 request_name 派生的检索标签列表 |
| `model_identifier` | 实际使用的模型名（failover 后可能和第一次不同）|
| `messages` | OpenAI 风格的对话轮次，纯文本，media 已脱敏 |
| `response` | `{content, reasoning, tool_calls}` |
| `tool_results` | 工具返回值列表 |
| `usage` | `{prompt_tokens, completion_tokens}` |
| `latency_s` | 端到端延迟（秒），流式为消费完毕时刻 |
| `success` | `true` / `false` |
| `error` / `error_type` | 失败原因 |
| `metadata` | 扩展字段：`attempt_index`、`retry_count`、`stream_id`、`heartbeat_run_id`、… |
| `extensions` | 预留，未来放 `quality_score`、`annotation`、`is_synthetic`、… |

---

## 媒体脱敏规则

落盘前一律通过 `sanitize_text_only()` 处理：

- `data:image/...;base64,...` 数据 URL → `[removed]`
- 纯 base64 body（≥ 256 字符的 `[A-Za-z0-9+/]` 串）→ `[removed]`
- 内嵌在长文本中的 blob（`_EMBEDDED_DATA_URL_RE` / `_EMBEDDED_BASE64_RUN_RE`）→ `[removed]`
- bytes / PathLike 对象 → `[removed]`
- 媒体 URL / 本地路径（`.jpg`、`.mp4`、… 结尾，或 `~/`、`/` 前缀）→ `[removed]`
- 媒体 source key（`data`、`base64`、`url` 等来自 provider 的字段）→ `[removed]`

---

## 在线落盘

`LLMRequest.send()` 在每次模型调用完成后写入一条记录：

- 非流式：成功时同步写，失败时在 `except` 里写。
- 流式：挂在 `resp._on_complete` 回调，消费完毕后写（`latency_s` 含消费时间）。
- failover 切换：每个 attempt 各写一条，`parent_attempt_id` → `attempt_id` 形成链。
- 写入出错不影响主流程（`record_trajectory` 吞掉所有异常）。

开关在 `config/core.toml`：

```toml
[llm]
enable_trajectory_logging = true
trajectory_base_path = "data/training_data_lake"
trajectory_flush_interval = 5.0    # 后台 flush 间隔（秒）
trajectory_queue_limit = 10000
```

---

## 历史数据迁移

已迁移 14443+ 条历史记录进 `archive/`（包含从损坏备份里救回的 17521 条指标数据）：

```bash
# dry-run 查看预计数量
python scripts/migrate_training_data.py --all

# 实际写入
python scripts/migrate_training_data.py --all --run

# 单独迁移某一来源
python scripts/migrate_training_data.py --messages --run
python scripts/migrate_training_data.py --life-events --run
python scripts/migrate_training_data.py --metrics --run
```

各来源的 `completeness` 字段：

| 来源 | completeness | 说明 |
|------|-------------|------|
| `messages` 表 | 0.6 | 缺 system prompt 和模型名 |
| `life_events.jsonl` | 0.7 | 有 tool 链路，缺 usage |
| `llm_metrics.json` | 0.2 | 只有 usage/latency |

---

## 导出后训练数据集

```bash
# 先看清分布
python scripts/export_training_data.py --stats --include-archive

# 导出 SFT 对话数据（闲聊/回复类）
python scripts/export_training_data.py --format sft_chat --include-archive

# 导出 Agent 轨迹（带 tool 链路）
python scripts/export_training_data.py --format agent --include-archive

# 按 request_name 过滤
python scripts/export_training_data.py --format sft_chat --request-name life_chatter
```

输出文件在 `data/training_data_lake/export/`。

---

## 旧落盘方式的迁移说明

| 旧组件 | 新状态 |
|--------|--------|
| `MetricsCollector._json_path` | 默认不再写文件（内存统计保留供 WebUI） |
| `data/json_storage/llm_metrics.json` | 已迁移 → `archive/legacy_llm_metrics/` |
| `llm_metrics.json.corrupt.*` | 已救回数据 → `archive/llm_metrics_migration_*.jsonl` |
| `RequestInspector` 内存 deque(200) | 不变，仍用于 WebUI 实时预览 |

如需恢复旧 JSON 落盘，设置环境变量：

```bash
export MOFOX_LLM_METRICS_PATH=data/json_storage/llm_metrics.json
```

---

## 新增文件索引

| 文件 | 说明 |
|------|------|
| `src/kernel/llm/trajectory_types.py` | schema 常量、类型、sanitize/ensure/derive 工具函数 |
| `src/kernel/llm/trajectory_collector.py` | 线程安全 append-only JSONL collector + 全局单例 |
| `src/kernel/llm/policy/failover.py` | FailoverPolicy：首败即换，不反复重试 |
| `scripts/migrate_training_data.py` | 历史数据迁移脚本 |
| `scripts/export_training_data.py` | 后训练数据集导出工具 |
| `test/kernel/llm/test_trajectory_collector.py` | 单元测试（15 项） |
