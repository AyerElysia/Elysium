# MySQL Selected Storage 与多写入者交接单

本文说明已经合入本地 `main` 的 MySQL selected storage、多写入者和性能优化，以及其他开发者需要同步的配置、迁移和验收边界。项目原则、架构和通用协作规则仍以 `AGENTS.md` 与现有文档为准。

## 1. 本次提交解决什么

本次变更把正式 MySQL 模式补齐到可持续运行状态，主要包含：

1. Core 与 Life Engine 统一使用 `config/core.toml` 的全局 `[storage].backend`，移除插件级第二后端选择。
2. Subject current head 成为 `SOUL.md`、`USER.md`、`MEMORY.md` 的 MySQL 权威读取入口。
3. 新增通用技术运行态存储，让 Router、Chatter、Life Trace、Narrative、Autonomy、Curiosity 等在 MySQL 模式下不再读写本地 JSON/JSONL/SQLite。
4. MySQL 采用“单一 verified generation、多进程共享写入”模型：首个进程激活 generation，后续进程加入同一 generation，不再因已有合法 writer 而拒绝启动。
5. 每个写事务仍校验 active backend、generation 与 epoch；generation 切换会 fence 旧进程。shared writer 关闭只释放本进程资源，不撤销其他 writer。
6. 增加既有正式库的 Runtime State 增量迁移命令。
7. 优化 MySQL 启动与回复热路径：authority audit 按 head 复用，Router 通过 content-free subject head marker 失效缓存，全局历史固定批量查询，滚动链合并历史后不再重复读取。
8. 修正 Heartbeat/Expression 的上下文预算配置要求，不通过截断主体权威文本规避预算错误。
9. 修正 Chatter 外层步进与 WatchDog 预算合同，避免 Router 消耗时间后表达链被旧 90 秒配置提前取消。

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
- 旧静态 `authority_epoch`、`fencing_token_env` 不再配置。MySQL registry 尚未激活时，首个进程为已登记且 verified 的 generation 建立当前 epoch；后续进程加入该 generation，共享 generation/epoch 写入资格。
- 每个 worktree/部署实例必须配置稳定且唯一的 `authority_owner_id`，用于审计来源；它不是独占写入锁。
- MySQL 模式不使用入口级 `data/runtime/elysium.lock`；local 模式仍保持单进程保护。多个完整进程还必须使用不冲突的 HTTP 端口、临时目录和不可共享外部适配器会话。
- 已有合法 MySQL writer 不得成为启动拒绝理由；凭据、TLS、权限、schema/checksum、generation、端口或外部会话冲突仍必须显式失败，禁止静默 local 回退或假成功。
- `authority_lease_seconds` 与 `authority_renew_interval_seconds` 仅保留为兼容/独占 authority 合同；shared writer 不通过周期续租争夺或维持独占 owner。

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
4. MySQL shared writer 必须在每个写事务中复核 active backend、generation 和 epoch；另一合法 writer 不构成冲突，但 generation 被切换或封存后，旧进程的下一次写入必须被 fencing 拒绝。
5. shared writer 正常关闭或异常退出不得撤销全局 generation；只有显式 generation 切换/封存才推进 epoch。local/独占 authority 的续租语义不得误套到 shared writer。
6. Runtime State 使用 revision CAS；同 occurrence ID 不同内容必须报冲突；Runtime Event 不得更新或删除。
7. 模型超时/取消不能 flush 未读、推进游标、提交 rolling context 或伪装发送成功。
8. Runtime State 是技术状态，不替代 Subject Document、Life Event 或主体语义。
9. Router head marker、authority audit head 和进程内投影缓存只优化可重建读取路径；不得缓存后冒充新的主体权威，也不得省略提交事务中的 generation/epoch fence。

## 6. 合并状态与后续开发热点

本地 Git 元数据曾损坏，原未推送 commit 的 SHA 与边界无法恢复；存活工作区内容已重新建立为可审计提交。当前本地提交链为：

- `312defda`：恢复未同步的本地开发状态；
- `3caa2980`：MySQL 多写入者与启动/回复性能优化；
- `ca123af9`：将功能分支合入本地 `main`。

合并时已对 `service/core.py`、`chatter.py`、Router projection、storage contracts/factory、Subject store、Core config 和相关测试逐项语义处理。后续开发不得用整文件 ours/theirs 覆盖这些边界；尤其要同时保留 perception/delivery commit gate、主体权威边界、Runtime State CAS、shared writer transaction fence 和 fail-closed 接线。

当前 `main` 尚未 push。原损坏 `.git`、full-index binary patch、未跟踪文件归档和孤立目录仍保留在项目根目录旁的恢复备份中；确认远端推送、真实 MySQL 验收和备份保留期以前，不要清理这些材料。

## 7. 验证记录与真实复测

合并后的定向结果包括：

- 运行态存储与上下文投影：83 passed。
- 服务、selected storage 与 Runtime State 迁移：45 passed。
- Heartbeat/表达上下文相关：111 passed。
- Chatter/Core 超时配置与取消/WatchDog：60 passed。
- `py_compile` 与 `git diff --check` 通过。
- `upgrade-runtime-state` 已在真实 MySQL 执行并完成幂等重放。

本次恢复、三方合并和 merge commit 完成后，已执行：

- MySQL 多写入者、Core config、selected service、Subject/Router、Chatter、Runtime State 与 Emoji 交叉测试：235 passed；
- 核心 MySQL/Router/历史查询文件 Ruff：通过；
- `compileall`、`git diff --check`：通过；
- `git fsck --full --no-reflogs --no-dangling`：通过；
- `main` 与 `feature/mysql-multi-writer` worktree：干净。

这些仍是离线合同与仓库完整性结果。最后必须由用户手工启动做真实验收：

1. 两个使用不同 `authority_owner_id`、HTTP 端口和外部适配器资源的进程可连接同一 verified generation，同时启动且均可提交写入；
2. 任一 shared writer 关闭后，另一个进程继续写入；显式切换/封存 generation 后，旧进程下一次写入被 fence；
3. 无 `runtime_states doesn't exist`，无错误的 pinned context 预算拒绝；
4. 飞书私聊能在步进预算内真实回复，发送成功后 receipt、未读游标、rolling context 与 runtime revision 一致推进；失败重试不重复发送、不丢消息；
5. 至少测量 20 次冷启动和 100 个真实文本 turn 的数据库、模型与平台发送阶段 p50/p95，不能以关闭 TLS、跳过 schema/authority 校验或删除历史换取数字。
