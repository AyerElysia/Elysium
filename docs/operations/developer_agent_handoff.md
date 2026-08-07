# 本次 MySQL Selected Storage 变更交接单

本文只说明本次待提交变更及其本机配置迁移，交接对象是已经参与 Elysium 开发的 Agent。项目原则、架构和通用协作规则仍以 `AGENTS.md` 与现有文档为准。

## 1. 本次提交解决什么

本次变更把正式 MySQL 模式补齐到可持续运行状态，主要包含：

1. Core 与 Life Engine 统一使用 `config/core.toml` 的全局 `[storage].backend`，移除插件级第二后端选择。
2. Subject current head 成为 `SOUL.md`、`USER.md`、`MEMORY.md` 的 MySQL 权威读取入口。
3. 新增通用技术运行态存储，让 Router、Chatter、Life Trace、Narrative、Autonomy、Curiosity 等在 MySQL 模式下不再读写本地 JSON/JSONL/SQLite。
4. 修复 MySQL writer authority 续租任务提前退出的问题。
5. 增加既有正式库的 Runtime State 增量迁移命令。
6. 修正 Heartbeat/Expression 的上下文预算配置要求，不通过截断主体权威文本规避预算错误。
7. 修正 Chatter 外层步进与 WatchDog 预算合同，避免 Router 消耗时间后表达链被旧 90 秒配置提前取消。

## 2. 代码配置变化

### 2.1 全局存储配置

唯一后端选择现在位于：

```toml
[storage]
backend = "mysql" # 或 "local"
```

MySQL 模式下，`[storage]` 至少需要：

```toml
[storage]
backend = "mysql"
backend_generation = "<VERIFIED_GENERATION_ID>"
schema_version = 1
registry_id = "life-domain"
authority_provider = "mysql"
authority_owner_id = "<STABLE_INSTANCE_OWNER>"
require_verified_generation = true
authority_lease_seconds = 120
authority_renew_interval_seconds = 40
```

变化点：

- 旧 `database.database_type` 会迁移到 `[storage].backend` 后移除。
- Life Engine 插件配置中的后端、generation、MySQL 连接配置不再是合法第二来源。
- 旧静态 `authority_epoch`、`fencing_token_env` 不再配置；每次启动由 MySQL 控制面签发新 epoch/token，进程内自动续租。
- `authority_renew_interval_seconds` 必须短于 `authority_lease_seconds`。

数据库连接仍放在 `[database]`，密码只通过环境变量注入：

```toml
[database]
mysql_host = "<MYSQL_HOST>"
mysql_port = 3306
mysql_database = "<MYSQL_DATABASE>"
mysql_user = "<MYSQL_USER>"
mysql_password = "${ELYSIUM_MYSQL_PASSWORD}"
mysql_ssl_mode = "required"
```

### 2.2 Chatter 工程超时

另一位开发者的本机 `config/core.toml` 需要同步：

```toml
[bot]
stream_step_timeout = 300.0
stream_restart_threshold = 360.0
```

`stream_step_timeout` 是 Router、表达模型故障转移、工具续轮和回滚共用的完整步进预算。启用该保护时，`stream_restart_threshold` 必须严格更大；配置模型现在会拒绝矛盾组合。

### 2.3 模型上下文预算

另一位开发者需要检查本机 `config/models.toml`：

- `models.*.ctx` 必须与 Provider/模型真实上下文能力一致。
- `tasks.*.context_tokens + tasks.*.tokens <= models.*.ctx`。
- `core`、`utility`、`expression` 都必须能容纳主体权威前缀与工具 schema。
- 若出现 `task context cannot fit without truncating pinned or structured payloads`，修正模型能力声明和任务预算；禁止裁剪 `SOUL.md`、`USER.md`、`MEMORY.md`。

本机实测所需最低关系是：`core` 与 `utility` 的输入预算必须高于约 34K tokens，表达任务还要为聊天历史和工具续轮留余量。不要把本文中的测量值当成永久固定阈值，合并后应重新测量实际 payload。

## 3. 数据库增量升级

本次新增：

- `runtime_states`：`(namespace, state_key)` 主键，revision CAS 更新。
- `runtime_events`：append-only，`occurrence_id` 幂等。
- `life_runtime_state_schema_migrations`：迁移版本/checksum。
- `runtime_events` 的 UPDATE/DELETE 不可变触发器。

既有正式 MySQL 库在启动新代码前执行一次：

```bash
python scripts/adopt_life_mysql_baseline.py upgrade-runtime-state --config config/core.toml
```

命令可幂等重放，只创建并审计 Runtime State 技术 schema：

- 不修改 active generation；
- 不取得或替换 writer authority；
- 不改变既有五域 generation source root；
- 不迁移本地 JSON；
- 表或 trigger 漂移时明确失败。

全新库仍走完整 baseline adoption，不能只执行该增量命令。

## 4. 本次新增/关键文件

新增运行态实现：

- `plugins/life_engine/storage/runtime_contracts.py`
- `plugins/life_engine/storage/runtime_adapters.py`
- `plugins/life_engine/storage/runtime_factory.py`
- `plugins/life_engine/storage/runtime_schema.py`
- `scripts/adopt_life_mysql_baseline.py`
- `test/plugins/life_engine/test_runtime_state_storage_contract.py`
- `test/plugins/life_engine/test_mysql_baseline_adoption.py`

主要接线与生命周期文件：

- `src/core/config/core_config.py`
- `src/app/runtime/bot.py`
- `src/kernel/db/core/engine.py`
- `plugins/life_engine/storage/factory.py`
- `plugins/life_engine/service/core.py`
- `plugins/life_engine/service/state_manager.py`
- `plugins/life_engine/core/router_context_projection.py`
- `plugins/life_engine/core/chatter.py`
- `plugins/life_engine/autonomy.py`
- `plugins/life_engine/curiosity/engine.py`
- `plugins/life_engine/narrative/store.py`
- `plugins/life_engine/trace/store.py`

## 5. 必须保留的不变量

合并或继续开发时，不要丢失以下边界：

1. MySQL selected 模式下，Subject、Router/Chatter runtime、Life Trace、Narrative、Autonomy、Curiosity 只能走远端 store；store 未挂载必须 fail closed。
2. selected 模式不得生成或读取本地 `USER.md` 作为回退，也不得回退到本地 runtime JSON/JSONL/SQLite。
3. Subject current head 缺失、版本损坏、manifest 缺失或预算元数据不一致必须显式失败，不能返回空状态。
4. Authority 续租任务必须在 `_stop_event` 创建后启动；续租成功后要原子替换进程内 token；失效 token 的写入必须被 fencing 拒绝。
5. Runtime State 使用 revision CAS；同 occurrence ID 不同内容必须报冲突；Runtime Event 不得更新或删除。
6. 模型超时/取消不能 flush 未读、推进游标、提交 rolling context 或伪装发送成功。
7. Runtime State 是技术状态，不替代 Subject Document、Life Event 或主体语义。

## 6. 与当前远端提交合并的热点

拉取前远端主线比本地领先 26 个提交，其中这些演进与本次提交重叠：

- exact transient/perception delivery receipt；
- Heartbeat perception commit gate；
- Chatter context commit gate 与 durable delivery replay；
- 普通聊天单模型轮发送；
- Legacy thought stream 退役与 Subject Attention authority；
- Curiosity 改为 epistemic opportunities；
- Heartbeat 去自激与工具轮 trajectory identity。

高冲突文件：

- `plugins/life_engine/service/core.py`
- `plugins/life_engine/core/chatter.py`
- `plugins/life_engine/core/config.py`
- `plugins/life_engine/core/plugin.py`
- `plugins/life_engine/curiosity/engine.py`
- `plugins/life_engine/prompts/sections.py`
- `plugins/life_engine/service/memory_witness.py`
- `docs/operations/life_storage_backend_runbook.md`
- 对应 service/chatter/curiosity/selected-storage 测试。

合并要求：

- 不要整文件选择 ours/theirs。
- 保留远端的 perception receipt、delivery replay、context/heartbeat commit gate 与单轮发送语义。
- 同时保留本次 selected storage、Subject current head、Runtime State CAS/event、authority 续租和 fail-closed 接线。
- 如果远端已退役 thought/curiosity 旧接口，把本次远端持久化接线迁移到新的 Attention authority；禁止复活旧语义。
- 发送 receipt、未读 flush、rolling context commit、runtime revision 必须维持同一成功边界。

## 7. 验证记录与合并后复测

本次变更在合并前已执行过的定向结果包括：

- 运行态存储与上下文投影：83 passed。
- 服务、selected storage 与 Runtime State 迁移：45 passed。
- Heartbeat/表达上下文相关：111 passed。
- Chatter/Core 超时配置与取消/WatchDog：60 passed。
- `py_compile` 与 `git diff --check` 通过。
- `upgrade-runtime-state` 已在真实 MySQL 执行并完成幂等重放。

这些结果只能证明合并前版本。合并远端后至少重跑：

```text
Core config 与 Bot lifecycle
Runtime State storage/baseline adoption
selected presence/world/service
Subject/Router context projection
Chatter prompt、delivery receipt、context commit gate
Heartbeat perception commit gate
Curiosity/Attention migration
Autonomy、Narrative、Life Trace
```

最后由用户手工启动做真实验收：

1. 无 `runtime_states doesn't exist`。
2. 无错误 32K pinned context 拒绝。
3. 跨越至少两个 authority 续租周期仍可写。
4. 飞书私聊能在步进预算内真实回复。
5. 发送成功后 receipt、未读游标、rolling context 与 runtime revision 一致推进；失败重试不重复发送、不丢消息。
