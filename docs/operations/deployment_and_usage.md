# Elysium 部署、配置、测试与使用说明

> 文档状态：持续维护中
> 当前版本：Windows/WSL/Linux 共用部署脚本；QQ/飞书文本聊天与图片查看验收基线；阶段三 `/api/v1` 应用接口
> 最后核对日期：2026-08-13

本文面向第一次接手 Elysium 的开发者和维护者，目标是让接手者能够独立完成环境准备、配置、启动、验证、故障排查和日常使用，并逐步覆盖项目的全部功能。

Elysium 是数字生命系统，不是通用聊天机器人框架。修改配置或代码前，必须先阅读：

- [`AGENTS.md`](../../AGENTS.md)
- [`docs/principles.md`](../principles.md)
- [`docs/architecture/Elysium当前架构.md`](../architecture/Elysium当前架构.md)

尤其注意：工程安全限制与主体的认知裁决必须分离，不得用关键词匹配、固定阈值、默认类别、代码截断或情境自动触发替代主体判断。

新机器必须先按[安全部署脚本](./deployment_scripts.md)执行 `bootstrap` 与只读 `doctor`。本长文解释各子系统配置和真实验收；凡与脚本的 create-only、locked dependency、主体文件主权或手工前台运行合同冲突的历史命令均不再有效。

---

## 多后端共享数据库协议（实施状态）

当前已落地多写者基础协议与全部生产热路径：operation identity、claim/lease、operation receipt、typed runtime delta、带 claim fencing 的 outbox 状态存储、按节点隔离且连续推进的 projection progress，以及 MySQL/SQLite 共用 schema（v3）。`unknown` 外部发送结果不会被重新认领，单节点投影完成不会让其他节点伪完成。该协议只协调具体 operation，不把全局 Life Engine singleton writer 作为新增 operation 的前提。热路径接入状态：

- 入站消息：Distributor 在分发前经 core transport 的 inbound fact hook 落库不可变消息事实，并原子认领对应 stream turn（`UNIQUE(source_message_id)`，同一事件到达两个实例时只有一个能认领）；Chatter 消费后按 per-message turn 提交，fencing 拒绝陈旧 owner。
- Heartbeat：每轮先注册并认领本序列 heartbeat operation，被其他实例认领/已完成时跳过本轮；模型失败或超时释放为 retryable，成功提交 checkpoint（frontier 不推进时保留可重放语义）。
- 外部发送：平台调用前先落 outbox 意图（落库失败 fail-closed 阻止发送），发送完成后按回执收尾为 `sent`，明确失败为 `retryable`，结果不确定为 `unknown`（禁止盲目重发）。
- 记忆索引：worker 继续使用本地 SQLite 原子认领（多 worker 安全），每轮完成后推进本节点共享 projection progress（frontier 严格 +1，配置变化拒绝）。

协议启动门已接入 `[storage]` 配置与 selected storage 启动路径：`multi_writer_enabled` 默认关闭，旧路径仍申请 `life_engine.runtime_context/global` singleton claim，行为不变；显式启用时先做数据库只读观测（legacy singleton 是否退场、多写者锚表是否部署），并校验 schema >= 3、协议版本匹配、热路径就绪（`MULTI_WRITER_HOT_PATHS_READY`），任一不满足即 fail-closed，不会申请任何 claim 或 attach 任何域 store。当前热路径迁移已完成（`MULTI_WRITER_HOT_PATHS_READY=True`）。阶段 0 字段审计已产出 `docs/architecture/多后端共享数据库-runtime_context字段审计.md`（`global` full snapshot 的 25 个字段的 authority/owner/merge contract/迁移目标）。多实例身份合同已落地（`InstanceIdentity`：deployment_id/instance_id/boot_id/owner_id/protocol_version/schema_generation/config_digest/workspace_revision，boot_id 每次启动变化并用于 claim fencing，claim owner 格式为 `deployment:instance:boot`）。content-free 健康观测已落地（`observe_multi_writer_health`：operation/outbox 状态分布、过期 claim、本实例 claim 数、projection readiness、最近成功提交时间，缺失表降级为 not_ready，连接失败为 failed，绝不输出 payload/正文/密钥）。

维护窗口（规范 16.2）已按下列动作执行过一次，并已提交代码与配置：

- 新增 runtime schema v4 迁移 `life_runtime_state_singleton_claim_guard_retirement_v4`：幂等 DROP `runtime_states_singleton_claim_{insert,update,delete}_v2` 三个 claim-guard 触发器。
- 新增 learning schema v3 迁移 `life_learning_singleton_claim_guard_retirement_v3`：幂等 DROP learning 域 4 个 claim-guard 触发器。
- `ensure_runtime_state_schema` 迁移顺序为 v1→v3→v2→v4，之后只验证 append-only immutable 触发器合同；`ensure_learning_schema` 同理只验证 immutability 触发器。
- `writer_claims.prepare_runtime_state_write` 在 `shared_writers`（多写者 generation）运行时不再要求已注册 key 必须携带 claim；旧单写者路径行为不变。
- Life Engine 启动在 `multi_writer_enabled=true` 时不再为 learning 域申请 generation-scoped singleton claim（learning 触发器和 runtime_context/global 一样已退场）。
- 维护窗口执行脚本：`runtime/retire_singleton_multi_writer.py`（默认 dry-run，`--confirm` 才执行；执行前校验无 live claim；创建多写者 schema、DROP 退役触发器、清理已退役 claim 行、注册新 generation `schema_version=3`、推进 authority epoch 并轮换 fencing token）。
- 启动门验证脚本：`runtime/verify_multi_writer_startup_gate.py`（只读：`_guard_generation`、`join_generation`、`observe_multi_writer_state`、`validate_multi_writer_readiness`、读取 `runtime_context/global` checkpoint）。

配置语义：`multi_writer_enabled=true` 要求 `schema_version=3`、`backend_generation` 指向已激活的多写者 generation（注册时 `schema_version=3`）、`multi_writer_protocol_version` 与 generation 一致。旧单写者 generation（`schema_version=1`）仍在注册表中保留为只读历史，不允许作为运行 generation 复用（`register_generation` 以 manifest SHA-256 防 reuse）。

## 1. 当前验收状态

以下状态以 2026-08-02 的 Windows 本地真实验收为准。

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 项目 `.venv` 本地启动 | 已验证 | 不依赖 Docker |
| Core MySQL 数据库 | 已验证 | 13 张 Core 关系表支持直接读写 MySQL；切换后仍需按部署实例做启动与读写验收 |
| HTTP 服务 | 已验证 | 默认监听 `127.0.0.1:8000` |
| Life Engine 心跳 | 已验证 | 使用 `model_tasks.core` |
| Life Chatter 文本表达 | 已验证 | 必须启用 `[chatter].enabled` 且存在非空 `SOUL.md` |
| 飞书长连接收消息 | 已验证 | 不需要公网域名 |
| 飞书私聊文本回复 | 已验证 | 已处理私聊 `chat_id` 路由和引用消息 ID |
| QQ/NapCat 私聊文本聊天 | 已验证（2026-08-04 WSL） | NapCat 4.18.13 + QQNT 3.2.23-44343 已完成真实私聊收发；长期稳定性仍需持续观察 |
| QQ/NapCat 图片查看 | 历史已验证 | 是否启用由各部署环境自行配置；启用后应重新做当前版本冒烟 |
| 飞书图片查看 | 已验证 | 图片资源下载需要消息读取权限；详见 9.1 |
| 飞书图片保存 | 已验证 | 由主体主动调用 `nucleus_save_media` 保存到 Life Engine workspace |
| 飞书图片发送 | 已验证 | 由主体主动调用 `life_send_image`；超过飞书图片上传上限的静态图会只为传输生成压缩 JPEG 副本，workspace 原图保持不变 |
| 文本与图片模型 | 需按部署环境配置 | 所选模型必须声明并实际支持所需的文本、视觉和工具调用能力 |
| Life Memory 文本向量生成 | 需单独验收 | 向量维度必须与活动索引一致；完整记忆检索功能需单独验收 |
| 主体媒体能力 | 图片与语音四项已验收 | 飞书图片保存/发送、语音接收识别和语音合成发送均已通过真实端到端验收；所有能力由主体主动调用 |
| 普通附件收取、保存与发送 | 离线合同已实现，真实链路待验收 | QQ、KOOK、飞书入站文件会物化为内容寻址引用；主体可保存到 workspace 或发送单个本地文件，但尚未完成当前版本真实客户端闭环 |
| Ayla 独立应用通道 | 已验证（端到端） | `plugins/ayla_adapter`（platform=`ayla`）注册进 registry，出站虚拟确认，不接收入站（入站走 `messages:inject` 进标准接收链），投递由 Ayla 侧 SSE 投影完成；2026-08-12 完成真实 Ayla 前端 Playwright 端到端验收（收发 + 刷新持久化），见接入文档 §8.2 |
| 其他功能 | 暂不验收 | 包括群聊、视频、直播、Minecraft、屏幕观察、MCP 等；配置或代码存在不代表已验证 |

当前仍有效的真实端到端验收记录包括：**QQ 聊天、飞书聊天、QQ 查看图片、飞书查看图片、飞书保存图片、飞书发送图片、飞书语音接收识别、飞书语音合成发送、Ayla 独立应用聊天**。验收记录只说明相关链路曾经通过，不记录或规定任何个人部署环境当前是否启用该能力。

---

## 2. 运行架构速览

当前实际启动链路是：

```text
main.py
  -> src/app/runtime/bot.py:Bot.start()
  -> 初始化 Core、LLM、数据库、HTTP 服务
  -> 发现并加载 plugins/
  -> 启动调度、适配器和消息流
  -> Life Engine / Life Chatter
  -> LLM、工具、Action
  -> Sender / Adapter
```

日常私聊和群聊是事件源与回复目标，不是独立心智。默认由 `chat_global` 意识实例处理，共享同一滚动上下文，并通过全局异步锁保证同一时刻只有一个来源推进模型/工具链。

与部署直接相关的核心文件：

| 文件 | 用途 |
| --- | --- |
| `main.py` | 当前主入口 |
| `config/core.toml` | Core、数据库、HTTP、日志、全局 LLM 行为 |
| `config/models.toml` | 生产 Provider、模型注册和有序语义任务路由；不承载本地消息 TTS |
| `config/model.toml` | 旧格式显式迁移兼容；生产启动不读取，也不参与任务回退 |
| `config/mcp.toml` | MCP 服务配置 |
| `config/plugins/life_engine/config.toml` | Life Engine、心跳、Chatter、记忆及场景能力 |
| `config/plugins/feishu_adapter/config.toml` | 飞书应用、连接和消息行为 |
| `config/plugins/tts_voice_plugin/config.toml` | 当前本地消息 TTS Service；生产目标为微调 IndexTTS2.5 + vLLM-Omni，历史兼容服务只作显式回退 |
| `data/life_engine_workspace/SOUL.md` | 主体灵魂文件；Life Chatter 表达的硬前提 |
| `logs/` | 运行日志 |
| `data/` | SQLite、记忆、事件和运行数据 |

`config/` 下的实际配置文件默认被 Git 忽略，仓库只跟踪少量 `.example`。因此新机器不能假设能从 Git 直接取得现有运行配置，也不要把 API Key、App Secret 等密钥提交到仓库。

---

## 3. 环境要求

### 3.1 必需环境

- Windows 10/11、WSL 或 Linux；部署脚本已做离线契约验证，具体机器仍需手工运行验收
- Python 3.11 或更高版本
- Git
- `uv`（推荐的依赖管理器）
- 能访问所配置 LLM Provider 和飞书开放平台的网络
- 如需语音识别或飞书语音发送：安装项目依赖 `imageio-ffmpeg>=0.6.0`；Elysium 会优先使用 PATH 中的 FFmpeg，找不到时自动使用该依赖随包提供且支持 `libopus` 的 FFmpeg 二进制，不要求单独配置系统 PATH 或 `ffprobe`

项目声明见 `pyproject.toml`：

```toml
requires-python = ">=3.11"
```

### 3.2 本版不使用 Docker

本阶段只采用项目根目录下由 `uv` 管理的 `.venv`。旧根目录容器资产含 Elysium 自动重启和非锁定构建语义，已经退役；除非以后单独设计、审计并验收容器合同，否则不得自行恢复。

### 3.3 进入项目目录

PowerShell：

```powershell
Set-Location "<Elysium项目目录>"
```

Git Bash：

```bash
cd "<Elysium项目目录>"
```

所有启动和测试命令都应从项目根目录执行，因为当前入口使用了 `config/core.toml`、`plugins`、`logs` 等相对路径。

---

## 4. 创建虚拟环境并安装依赖

### 4.1 规范入口

生产依赖：

```powershell
.\deploy.ps1 bootstrap
```

Linux / WSL / Git Bash：

```bash
./deploy.sh bootstrap
```

开发机增加 `--with-dev`。脚本使用 `uv sync --locked`，随后执行 `uv pip check`；解析或安装输出不会原样回显，避免私有 index URL 中的凭据进入日志。`uv` 是硬前置，缺失时必须先从官方发行渠道安装，禁止改用全局 `pip` 或维护第二份手写依赖清单。

### 4.2 锁与恢复边界

生产运行使用 `uv run --frozen --no-sync`，启动过程中不得解析新版本、安装插件包或修改 lock。`config/core.toml.example` 因此默认设置 `[plugin_deps].enabled = false`。可选插件缺包时，先把依赖加入 `pyproject.toml`，更新并审查 `uv.lock`，完成测试后再部署；禁止在启动日志报错后逐包 `pip install`。

MySQL 异步引擎要求 SQLAlchemy ≥ 2.0.50；项目声明已经固定该下限，当前 lock 为 2.0.52。旧环境出现半卸载、元数据漂移或依赖不闭合时，不要原地强装单包。先保留可恢复的旧 `.venv`，再由操作者创建空的新环境并重新执行 `bootstrap`；新环境通过 doctor 与手工启动验收前不要删除旧环境。

安装期间不得并发运行第二个 uv，也不得启动 Elysium。依赖回退必须同时恢复匹配的 `pyproject.toml` 和 `uv.lock` 后重新 bootstrap，不能只降级 site-packages。

---

## 5. 首次生成配置

`bootstrap` 会从当前 schema 示例创建缺失的 `core.toml` 与 `models.toml`，同时写入默认关闭的可选插件工程配置。已存在普通文件绝不覆盖，异常目标类型直接失败。不要依赖一次失败启动来自动生成配置。

模型密钥保持为 `${ELYSIUM_NEXUS_API_KEY}` 等环境引用，真实值由当前终端或受控 secret manager 注入。正式模型加载会拒绝空值、未解析变量和示例占位符；部署 doctor 还会拒绝明文密钥。部署不创建 `.env`，也不把密钥放入命令参数或日志。本地 Router 等可选侧车在独立验收后再加入配置。

主体权威 `SOUL.md`、`USER.md`、`MEMORY.md` 不属于工程配置；只能从可信历史逐字节恢复。bootstrap、doctor 和普通启动都不得创作或补模板。完成配置与可信恢复后先运行 `doctor`，再由用户手工执行 `run`。

---

## 6. Core 基础配置

编辑：

```text
config/core.toml
```

Elysium 只允许通过 `config/core.toml` 配置全局存储。选择 `mysql` 时，Core 和 Life Engine 必须一起使用同一组 MySQL 连接、generation 和 authority 参数；选择 `local` 时，二者都使用本地存储。Life Engine 插件文件不再包含 MySQL 连接、generation 或后端选择，避免任何第二配置来源。

MySQL 模式的最小 Core 配置如下（真实地址和用户名只写入本机被 Git 忽略的 `config/core.toml`；密码只由环境变量注入，不要写入本文、TOML 或提交；epoch/token 不需要配置）：

```toml
[bot]
ui_level = "verbose"
log_level = "INFO"
llm_preflight_check = true

[storage]
backend = "mysql"

[database]
mysql_host = "<MYSQL_HOST>"
mysql_port = 3306
mysql_database = "elysium"
mysql_user = "<MYSQL_USER>"
mysql_password = "${ELYSIUM_MYSQL_PASSWORD}"
mysql_charset = "utf8mb4"
mysql_ssl_mode = "required"
mysql_ssl_ca = "<MYSQL_CA_PATH>"
connection_pool_size = 10
connection_timeout = 10
echo = false

[http_router]
enable_http_router = true
http_router_host = "127.0.0.1"
http_router_port = 8000
api_keys = []
```

Windows PowerShell 为当前终端设置密码环境变量（终端关闭后不保留）：

```powershell
$env:ELYSIUM_MYSQL_PASSWORD = "<MYSQL_PASSWORD>"
```

需要长期托管时使用受控 secret manager 在运行终端注入，不要把明文密码持久写入用户环境、TOML 或 Git。

Git Bash 临时设置当前终端环境变量：

```bash
export ELYSIUM_MYSQL_PASSWORD='<MYSQL_PASSWORD>'
```

说明：

- `[storage].backend` 是全局唯一后端选择，只接受 `local` 或 `mysql`。`database.database_type` 与 Life Engine 的 `storage.enabled`、`storage.authoritative_backend` 已废弃；旧 Core 配置会自动迁移，若新旧选择冲突则拒绝启动。
- `backend = "local"` 使 Core 使用 SQLite，并让 Life Engine 继续使用现有本地 SQLite、Markdown/JSON 和 Chroma 链；不要求创建 managed-local generation。Life Engine 启动前会只读校验 `data/life_engine_workspace/SOUL.md`、`USER.md`、`MEMORY.md` 三份主体权威文件；任一文件缺失、不可读或不是合法 UTF-8 都会直接拒绝启动，不会把缺失解释为空内容，也不会自动从 MySQL 回填。
- `backend = "mysql"` 使 Core 使用 MySQL，并强制 Life Engine 打开 MySQL runtime；verified generation 和 owner 必须配置，不能悄悄退回本地。首个进程激活 generation，后续开发者进程加入同一 generation 并获得并发写入资格；进程退出只释放本地连接，不撤销其他写入者。每个开发者应配置可审计且唯一的 `authority_owner_id`。
- MySQL 模式不使用进程级 `data/runtime/elysium.lock`，因此多个 worktree/开发者不会因另一个 Elysium 进程存在而被入口拒绝。`local` 模式仍保持单进程保护，避免多个进程并发写同一 SQLite 和本地权威文件。
- “允许启动”不等于掩盖故障：凭据错误、MySQL 不可达、generation 未验证、schema/checksum 漂移、迁移锁超时、权限不足、端口冲突或数据约束失败仍会显式报错。部署必须修复这些真实前置条件，禁止改成静默本地回退或假成功。
- `mysql_password` 支持 `${ELYSIUM_MYSQL_PASSWORD}` 环境变量插值。不要把真实密码填回 TOML、文档、日志或 Git；交互式设置环境变量时也要注意 shell 历史和终端录屏。
- 生产环境优先使用 `mysql_ssl_mode = "required"` 或 `"verify-full"`，并填写 CA/证书路径；只有本机受控网络且明确接受明文链路时才使用 `disabled`。
- MySQL 账号至少需要目标数据库及 Core 表的建表、读写、迁移和 `TRIGGER` 权限；不要给应用账号全库管理员权限。
- 正式 Life Engine activation 需要数据库级不可变触发器。若 MySQL 开启 `log_bin`，DBA 必须在维护窗口通过持久化服务端配置启用 `log_bin_trust_function_creators`，或采用等价且经过审计的管理员安装流程；业务账号不应获得 `SUPER` 或 `SYSTEM_VARIABLES_ADMIN`。该前置条件不满足时必须 fail closed，不能用空 generation 或应用层检查绕过。
- 本地单机运行建议保持 `127.0.0.1`，不要无理由监听 `0.0.0.0`。
- 如果对外开放 HTTP，必须配置强 API Key、反向代理、HTTPS 和访问控制。
- `llm_preflight_check = true` 会在启动时检查 Provider 网络连通性。
- 开发调试遇到断点导致 WatchDog 误判时，可临时关闭 `enable_watchdog`；普通运行建议开启。
- 全局改为 MySQL 不会自动迁移任何数据；Core 和 Life Engine 的目标数据、generation 与 authority 必须在切换前分别验收完成。

### 6.1 首次从 SQLite 切换到 MySQL

如果目标 MySQL 尚未包含当前 Core 和生命域数据，不能只改 `[storage].backend` 后直接启动。必须遵循以下顺序：

1. 用户手工停止 Elysium，建立明确停写窗口；不要由脚本或 agent 擅自停止进程。
2. 使用 `scripts/migrate_core_to_mysql.py snapshot` 为旧 Core SQLite 建立不可覆盖的在线快照。
3. 将迁移连接 URL只放入 `ELYSIUM_MYSQL_URL` 环境变量，执行 `migrate` 和 `verify`。
4. 完成 Life Engine MySQL generation 的审计、登记和 verified 签署，并确认不存在仍有效的旧 writer 租约；首次正式启动会自动取得新 authority epoch 与 fencing token，不能用测试 generation 或空 ID 冒充生产证据。
5. 只有 Core 与生命域的逐表数量、内容 SHA-256、frontier、版本链、触发器和目标备份都通过后，才把 `[storage].backend` 改为 `mysql`。
6. 用户手工启动 Elysium，执行 Core 与生命域读写冒烟；保留旧 SQLite/文件快照和全部迁移 manifest，不删除。

详细命令、目标库空库要求、幂等重放、备份与隔离恢复见 [MySQL 迁移、备份与恢复手册](./mysql_migration_and_backup.md)。如果部署本来就使用已经迁移并验证的 MySQL，则不应重复把旧 SQLite 强行导入。

现役 MySQL generation 合并新版本后，如果启动报某个新增生命域表不存在，不得让业务启动自动建表，也不要重新执行旧 SQLite 全量迁移。先停止 Elysium，在维护窗口使用同一份 `config/core.toml` 执行对应的幂等增量升级：

```powershell
uv run --frozen --no-sync python .\scripts\adopt_life_mysql_baseline.py upgrade-runtime-state
uv run --frozen --no-sync python .\scripts\adopt_life_mysql_baseline.py upgrade-attention
```

`upgrade-runtime-state` 只安装 `runtime_states/runtime_events`；`upgrade-attention` 只安装 AttentionThread canonical/legacy 五张表、迁移账本和不可变触发器。两者都不修改 generation、authority、配置或现有领域数据，可以幂等重放。命令成功后应确认输出状态分别为 `runtime_state_schema_upgraded` 或 `attention_schema_upgraded`，目标表审计完成，再手工启动 Elysium。若失败，保留原数据库与日志，不删除表、不关闭证书校验、不改成应用层不可变。

### 6.2 全局 local / mysql 模式

当前部署不再允许 Core 和 Life Engine 独立选择后端。`backend_generation`、MySQL 连接、registry 与 owner 是部署初始化时预先配置并长期保留的信息；完成初始化后，日常切换只允许修改 `[storage].backend` 一个字段。authority provider 由该字段自动派生，不再是用户配置项。唯一合法形态是：

```text
[storage].backend = "local"
├── Core：SQLite
└── Life Engine：现有本地 SQLite、Markdown/JSON 与 Chroma 投影

[storage].backend = "mysql"
├── Core：MySQL
└── Life Engine：同一 MySQL generation；Chroma/FTS 与工作区文件仍是可重建投影
```

Life Engine 插件配置不再包含 `[storage]` 或 `[storage_mysql]`；MySQL 的连接、generation、registry 和 owner 登记只在 `config/core.toml` 配置，后端选择只看 `[storage].backend`。`backend="local"` 时系统自动使用 file authority、忽略保留的 MySQL generation；`backend="mysql"` 时系统自动使用 MySQL authority并严格校验该 generation。旧 `[storage].authority_provider` 会被配置迁移移除，切换时不要清空/恢复 generation，也不要修改第二个开关。插件仅保留 local 模式所需的 `[storage_local]` 路径。任何插件级 `enabled`、`authoritative_backend`、generation 或 MySQL 连接字段都是旧配置，严格校验会拒绝加载。

MySQL 模式并不意味着把 Chroma 或媒体字节强行塞入关系表：Life Event、Life Memory、Presence、World、Learning、Attention 和主体文档版本由 MySQL 作为权威；Chroma、全文索引和工作区 Markdown 是可重建投影；图片、语音、视频和附件字节仍按受管媒体合同保存在文件或对象存储中，MySQL 保存其身份、哈希、权限和位置元数据。旧 `life_engine_workspace/thoughts/streams.json` 仅属于 local 模式和迁移证据；MySQL selected runtime 不得实例化文件型 `ThoughtStreamManager`，也不得继续修改该文件。

`action-report_state` 的提交目标是不可变 Life Event 与 World assertion，用于记录有来源的场景、关系或状态观察；它不是 `MEMORY.md` 主体文档写入。要修改 MySQL 中的 `MEMORY.md` current head，聊天意识必须走下述主体候选复盘与明确接受流程，不能把 World assertion 回执表述为主体文档已更新。

用户昵称、平台账号归属和跨平台人物键也属于运行数据，不应硬编码为 `config.toml` 中的个人记录。适配器配置只保留通用解析能力、权限策略和空的兼容 alias 列表；入站平台 ID、昵称、群名片及已确认的人物归属由 Core 的 `PersonInfo` 数据库记录承载。MySQL 模式下不得把插件配置 alias 当成用户数据权威；若旧部署曾填写具体 alias，应在确认相应数据库记录已存在后清空，并保留迁移/审计证据。

MySQL 模式下，聊天意识可通过主体复盘工具按 UTF-8 字节窗口读取当前远端 `SOUL.md`、`USER.md` 或 `MEMORY.md`，提交完整候选，再对精确候选作 `accepted` 决定。只有活跃意识实例、当前统一 revision、候选哈希和完整接受正文全部通过校验，才会在同一 MySQL 事务中追加主体版本并推进 `MEMORY.md` current head；不会写本地 Markdown，也不会自动合并或替主体决定。普通 Life Memory 的经历、解释、关系与回忆轨迹则继续通过 Memory ports 直接写 MySQL，不需要经过 `MEMORY.md` 文档改写。



MySQL runtime 采用“单一 verified generation、多进程共享写入”模型。第一次启动在 authority registry 中激活 generation；其他开发者只要连接同一库、同一 generation，便加入共享写入，不会夺走或撤销已有进程的资格。每笔提交仍在同一事务中校验 active backend、generation 和 epoch；generation 切换会使旧 token 失效。并发冲突由 InnoDB 行锁、唯一键、稳定 occurrence/idempotency identity、revision CAS 和事务 outbox 显式处理，schema migration 则继续使用连接级 advisory lock 串行执行。任一进程正常关闭都不会撤销全局 generation；健康信息会报告 `writer_mode = "shared"`。

MySQL 相比 local 慢的基础成本来自网络/TLS 往返、连接池 checkout、事务提交和服务端锁；它不可能像进程内 SQLite/本地文件一样接近零延迟。现役热路径已避免几类可消除放大：authority 审计链按已验证 head 在进程内复用；Router 正常轮只读取 SOUL/USER/MEMORY 的三个 head 指针作为变更标记，标记变化才传输并校验完整主体快照；有效 Router 投影在进程内复用；全局聊天历史按 messages、streams、persons 三次批量查询恢复，不再按消息逐条查询；滚动 payload chain 已合并历史后，后续回复轮不再重复读取未使用的全局历史。上述缓存只覆盖可验证控制标记和可重建投影，不缓存后冒充新的主体权威，也不省略写事务中的 generation/epoch fence。

性能验收应分别记录冷启动总耗时、首轮回复模型调用前耗时和同一滚动链后续回复模型调用前耗时，并同时采集 MySQL `performance_schema`/slow log 的语句次数和等待时间。建议至少使用 20 次冷启动与 100 个真实文本 turn，分别报告 p50/p95；测试期间保持相同 Provider、上下文规模、TLS、网络位置和外部适配器配置。若延迟仍高，先按“模型耗时 / 数据库往返 / 外部平台发送”拆分，禁止通过关闭 TLS、跳过迁移/authority 校验、删除历史或把数据库失败伪装为空结果来换取数字。

这项能力只解决“另一个合法 MySQL 写入者已存在”造成的启动拒绝，不承诺绕过真实运行冲突。例如，多进程不能绑定同一个 HTTP 端口或独占同一外部适配器会话；开发者应为各 worktree 配置不同监听端口和独立外部资源，或关闭本次不需要的组件。数据库认证、TLS、DDL/TRIGGER 权限和 schema 完整性仍是必须满足的启动条件。

项目还提供 `[shared_sync]` 与 `[memory_archive_sync]` 两项同步/归档能力。它们不是存储模式开关，不得用来制造第二个可写权威。正式切换、generation 校验与 authority 激活见 [生命域存储后端运行手册](./life_storage_backend_runbook.md)；设计依据见 [Elysium 生命域可选 MySQL 与本地存储重构方案](../architecture/Elysium生命域可选MySQL与本地存储重构方案.md)。

### 6.3 启动和回退验收

修改数据库配置不会热加载。完成配置后，由用户在维护窗口手工启动或重启，并至少检查：

- 启动日志确认数据库方言为 MySQL，且没有认证、TLS、连接超时、缺表或字符集错误；
- 查询已有聊天流/人物/图片元数据，确认不是连到了空库；
- 通过真实消息产生一条新的 Core 记录，再只读确认写入目标 MySQL；
- 验证四字节 Unicode、时间戳精度和连接回收；
- 执行一次 MySQL 逻辑备份并恢复到隔离库，再做逐表指纹复核。

若 MySQL 不可达，不要一边保留 MySQL 新写入一边直接把配置指回旧 SQLite。应先停止 Core 写入、备份 MySQL、核对切换后的差异，再选择恢复 MySQL 或执行经过审计的反向同步；否则会形成两份分叉的聊天和人物历史。

local 模式启动报 `SubjectAuthoritySourceMissing: <name>` 时，含义是主体权威快照不完整，不是 SQLite 或 `[storage].backend` 解析失败。不要新建空文件、复制模板或放宽读取逻辑来绕过。恢复只能来自可信版本历史或备份，并逐字节核对目标文件的长度与 SHA-256；如果没有可证明无损的恢复源，应停止并保留现场。恢复完成后至少运行主体三源读取和 Router/LifeChatter 定向测试，再由用户手工启动 Elysium 验收。

### 6.4 阶段三 `/api/v1` 认证基座

阶段三应用接口默认不挂载。只有明确需要前端或独立应用后端联调时，才在本机 `config/core.toml` 中启用：

```toml
[http_router]
enable_http_router = true
enable_app_api_v1 = true
app_api_v1_database_path = "runtime/app_api_v1/auth.sqlite3"
app_api_v1_allowed_origins = ["http://127.0.0.1:5173"]
app_api_v1_max_concurrency = 32
app_api_v1_max_websocket_connections = 64
app_api_v1_rate_limit_requests_per_minute = 600
app_api_v1_rate_limit_burst = 60
app_api_v1_max_command_concurrency = 8
app_api_v1_max_command_backlog = 1000
```

还必须通过环境变量提供：

- `ELYSIUM_APP_API_V1_SIGNING_SECRET`：至少 32 字节的稳定随机密钥；重启时必须保持一致，不得写入 TOML、日志或 Git；
- `ELYSIUM_INSTALLATION_ID`：该部署实例的稳定非空 ID，用于绑定本机 bootstrap challenge。

注入方式：启动链（`main.py`）在最前面自动读取 git-ignored 的
`runtime/app_api_v1_env.local`（`KEY=VALUE` 逐行格式，支持 `#` 注释与 CRLF），
把其中未在进程环境中设置的变量注入 `os.environ`；已存在的环境变量优先。
桌面 `start_elysium.bat` 的注入逻辑保留为兼容冗余。因此 IDE/终端直接运行
`main.py` 与通过启动脚本运行行为一致，不会因缺少签名密钥导致挂载失败。

安全与恢复边界：

- `api_keys` 是旧 WebUI 合同，不会替代 `/api/v1` 的短时 session、refresh、撤销和单次 WebSocket ticket；
- Origin 使用精确 allowlist，不支持通配符；localhost 也不能绕过认证；
- API SQLite 必须位于 workspace 的 `runtime/` 下；认证部分只保存凭据哈希、授权、到期与撤销状态，不保存可回显明文凭据；同库命令账本保存请求 payload 以供耐久执行和查询，因此备份、访问控制与留存策略必须按业务数据级别保护；
- 普通请求体上限 1 MiB，受管上传上限 32 MiB，HTTP 并发和 WebSocket 连接分别受配置预算约束；API 请求按脱敏调用方键执行有界 token-bucket 限流，命令 backlog 在耐久受理事务内原子检查，预算耗尽时不会写入一个随后又返回 429 的命令；
- 当前已挂载 P3-01 的五个认证端点、P3-02 的 `/bootstrap`、`/capabilities`、`/readiness`、`/health`，P3-03 的 `/events`、`/events/{event_id}`、`/events/stream` 和 `/event-subscriptions/validate`，P3-04 的命令创建、列表、单项查询和受限取消端点，P3-05 的五个只读聊天历史端点，P3-06 的 13 个耐久聊天命令端点，以及 P3-07 的 8 个用户媒体端点；除 `/health` 仅用于 API 存活探测外，其余接口要求短时 Bearer 会话和对应 scope；
- P3-08 直播端点为 `GET /livestream/status`、场次列表／详情／事件历史、`POST /livestream/session:start|stop|interrupt`、`POST /livestream/speech:request`、`POST /livestream/danmaku:send` 和 `WS /livestream/stage/ws`；P3-09 语音通话端点为 `POST /voice-calls`、`GET /voice-calls/{call_id}`、resume／interrupt／end／text、transcripts、tickets，以及 participant／observer WebSocket；P3-10 狼人杀用户端点为 `GET /tabletop/games`、room create/query/join/leave/start/end、授权 events、actor-bound private view、actions、replay 和 `WS /tabletop/rooms/{room_id}/ws`；P3-11 管理端点覆盖 overview／components／metrics／incidents／audit／logs／sync、session 撤销、credential 创建轮换撤销、allowlist settings、integrations、jobs，以及 chat 公告与 pin／unpin；P3-12 端点覆盖 consciousness 状态与受保护 suspend/resume/drain、world 断言/变化/观察/投影重建、memory 只读投影与 projection rebuild、commitments／autonomy 只读状态与外部 suggestion、Neko Surface 用户连接与管理连接，以及安全能力目录 `/abilities`；P3-13 引入统一权限矩阵、限流、并发/上传/WS 预算、秘密扫描与故障恢复测试；当前 OpenAPI schema（`docs/api/openapi.json`）覆盖已注册的 134 个操作，无重复 operation id，WebSocket 端点不进入 OpenAPI `paths`（见 `docs/api/permissions.md`）；
- 事件接口以耐久 Life Event SQLite ledger 的全局 ingest position 为权威位置，cursor 不透明、签名且绑定账本；授权过滤后的 cursor 表示“已扫描位置”，不可见事件不会造成虚假历史缺口，也不会通过单事件读取泄露存在性；
- 聊天历史接口为 `GET /api/v1/chat/streams`、`GET /api/v1/chat/streams/{stream_id}`、`GET /api/v1/chat/streams/{stream_id}/messages`、`GET /api/v1/chat/messages/{message_id}` 和 `GET /api/v1/chat/messages/{message_id}/receipts`，统一要求 `chat:read`。管理员可读全量；普通 actor 只能读取自己的事实或获授 `stream:{stream_id}`、`chat:*`、`*` 的 stream。不可见资源与不存在资源统一返回 404；同一 message ID 跨 provider／stream 冲突时返回 409，并要求使用 `provider` 或 `stream_id` 查询参数消歧；
- 聊天分页 cursor 绑定独立 `chat-events-v1` 账本标识，仍以 Life Event 全局 ingest position 为扫描位置。聊天查询服务未注入可用事件 store 时返回 503，不会为了只读查询隐式创建 Life Engine；历史缺口返回显式 gap 错误和恢复 cursor；
- 稳定聊天事实区分 `chat.message.send_requested`、`delivery_confirmed`、`delivery_failed` 与 `delivery_unknown`。发送前事件绝不代表平台投递成功；只有 Adapter 调用成功、发送历史写入完成后才确认。Provider 响应只提取真实返回的受控 receipt 字段，未返回时 receipts 中保持 `provider_receipt = null`，不得编造平台 message ID；
- 聊天消息中的媒体只导出经过重新校验的 descriptor，不导出本地路径、base64、原始 bytes 或任意资源 URL。当前 NapCat notice 已进入耐久兜底；飞书长连接仍只订阅 `im.message.receive_v1`，飞书撤回、回应和成员变化等 notice 尚未接入，部署验收必须按 Provider 分开记录；
- SSE 支持 `Last-Event-ID` 或 `cursor` 断点恢复，二者不一致会显式拒绝；先补历史再轮询 tail，heartbeat 不推进业务 cursor，断线不写服务端 durable offset。当前没有明确动态订阅或 ack 消费者，因此 `/events/ws` 保持 planned，不重复实现无消费者协议；
- 所有副作用命令必须携带 `Idempotency-Key`。同一 actor 使用同键提交相同规范请求会返回原 command；同键异请求返回 409。内部 session/resource grant 快照不参与公共请求 hash，因此同一 actor 刷新 session 后重放同一规范请求仍命中原 command；快照不会出现在公共响应。HTTP 受理只表示命令已耐久记录，不代表外部副作用成功，调用方必须通过命令查询读取最终状态；
- P3-06 普通聊天命令位于 `/api/v1/chat/...`，统一要求 `chat:write`；公告发布／删除和 pin／unpin 位于 `/api/v1/admin/chat/...`，同时要求 `chat:admin`、`chat:moderate` 与 `administrator` 或受信 `platform_service` 身份。普通用户即使意外持有管理 scope，也会收到 `role_required`；
- durable 聊天命令执行前会重新读取受理时绑定的 session，并验证当前撤销、access 到期、credential 撤销、resource grants 缩减和 P3-05 目标可见性。任一状态失效都拒绝尚未完成的操作；不能把受理时授权永久视为有效；
- 文本 send/reply 通过 `MessageSender`；reply/forward 的公共 message ID 在执行时解析为同一 Provider 的原生 message ID，跨 Provider forward 显式拒绝。edit/recall 仅允许命令 actor 自己已投递的消息。Provider 不支持、Feishu/NapCat Adapter 尚未加载或 capability 缺失时返回 `capability_disabled`，不得改发文本或误报 `provider_failed`；
- P3-06 媒体 part 只接受 `media_id`，禁止本地路径、任意 URL、base64、裸 bytes 和 `data`。P3-07 已为图片与语音 part 接入受管媒体 resolver：执行时使用当前重新校验的 actor/resource grants 读取对象并复核完整性；未接入 resolver 的部署仍返回 `capability_disabled`。视频、文件和 emoji 尚无聊天 resolver 映射，不能把已有主体媒体工具的本地能力误当成 `/api/v1` 受管媒体合同；
- P3-07 用户媒体端点为 `POST /media/uploads`、`PUT /media/uploads/{upload_id}`、`POST /media/uploads/{upload_id}:complete`、`GET /media/{media_id}`、`GET /media/{media_id}/content`、`POST /media/{media_id}:save`、`POST /media/{media_id}:recognize` 和 `GET /media/{media_id}/derivatives`。分别要求 `media:write`、`media:read` 或 `media:recognize`；上传声明上限 32 MiB，内容只存于 workspace `runtime/media/`；
- 媒体 owner 可访问自己的对象；共享对象必须绑定调用会话实际持有的精确 grant、`namespace:*` 或 `*`。非 owner 越权与不存在统一 404。下载支持 ETag 与单一 Range，无效 Range 返回 416；complete、下载和聊天发送前都会校验大小、SHA-256、MIME 与文件签名；
- API SQLite 保存媒体对象元数据，`runtime/media/objects/` 保存内容，二者必须作为同一恢复单元备份。`runtime/media/uploads/` 是临时上传区；当前只提供只读 cleanup candidate 识别，不自动删除未知或孤儿文件。恢复后必须验证 descriptor、对象 hash、saved 状态和 `sync_outbox` 连续性；
- 媒体 complete/save 事件复用既有 `sync_outbox`，保持 `private`、`held`，不包含 path、base64 或原始 bytes。recognize 后只持久化识别状态和文本；Provider 异常原文不会通过 API 返回；
- P3-08 直播端点为 `GET /livestream/status`、场次列表／详情／事件历史、`POST /livestream/session:start|stop|interrupt`、`POST /livestream/speech:request`、`POST /livestream/danmaku:send` 和 `WS /livestream/stage/ws`。只读端点要求 `livestream:read`；副作用端点要求 `livestream:operate` 且角色为 `administrator` 或受信 `platform_service`；
- 直播查询只打开已经存在的 Livestream ledger，不会创建场次、连接 B站、启动 TTS 或唤醒意识实例。start 仍要求已连接 primary stage，API 挂载与查询不会自动开播，也不会自动启动或重启 Elysium；平台运行中断线时 status 显示 `degraded`；
- 直播 stage WebSocket 复用 `/auth/ws-tickets` 的资源、Origin、subprotocol 和单次消费绑定。observer 只读且不能申请 primary 或提交播放回执；operator ticket 需同时携带 `livestream:operate`。播放副作用仍以直播 ledger 中稳定的 `playback.dispatched`／`playback.receipt` 以及 playback／utterance／chunk identity 为证据；
- 弹幕发送先写 `platform.danmaku_send_requested`，再写 confirmed／failed 结果，命令最终状态可通过 `/commands/{command_id}` 查询。当前 Bilibili Adapter 是只读接入且不持有账号 CSRF 写凭据，因此真实弹幕发送会显式失败；不得配置成普通聊天降级或把 `False` 当成功；
- P3-09 语音通话端点为 `POST /voice-calls`、`GET /voice-calls/{call_id}`、resume／interrupt／end／text、transcripts、tickets，以及 participant／observer WebSocket。创建只耐久登记 `call_id=episode_id`，不连接 Provider；participant WebSocket 才拥有实时会话资源；
- 语音 participant 继续使用 Voice Live v1 PCM16 二进制帧，禁止 base64 音频。公共网关强制把 start/resume 绑定到 URL `call_id`，客户端不能切换到另一 episode；observer 只接收该 call 的 JSON 状态／字幕，不转发音频且拒绝写操作；
- 语音 ticket 绑定 resource、subprotocol、Origin、session、credential 和 scope，单次消费；transcripts 只导出 append-only episode store 中的 final 记录，按 owner 或精确 `voice_call:{call_id}` grant 过滤；旧 `/voice-live` 路由迁移期保留；
- Voice Live 插件或 Provider 不可用时显式返回 capability unavailable／Provider failure，不自动加载插件、不切换主体模型、不启动或重启 Elysium。当前仅完成离线 API 与网关契约测试，真实 Provider、双向音频、断线重连和客户端 E2E 仍需单独授权验收；
- P3-10 狼人杀用户端点为 `GET /tabletop/games`、room create/query/join/leave/start/end、授权 events、actor-bound private view、actions、replay 和 `WS /tabletop/rooms/{room_id}/ws`。读取要求 `tabletop:read`，动作要求 `tabletop:play`；服务端始终从认证 session 取得 actor，客户端不能传入 player id 冒充他人；
- 新桌游场次保存在 `runtime/api/tabletop.sqlite3` 的追加式 ledger 和 revisioned projection 中。每个动作必须携带 `Idempotency-Key`；同一 action id 相同内容只返回原结果，同 id 异内容与 stale revision 返回 409。每次已提交状态还以仅裁判可见的 ledger snapshot event 保存，projection 损坏时可从 ledger 显式重建；数据库必须与其他 API 状态一起备份；
- 公共、玩家、裁判和复盘视图由 `plugins/werewolf_game/projections.py` 生成；公共视图不含角色、夜间状态或私密事件，玩家视图只包含该 actor 的身份、狼队友、查验和角色资源。实时事件流沿用同一可见性过滤，不能通过序号缺口、raw payload 或错误信息探测他人夜间动作；
- 新 API 不扫描、迁移或接管启动前已有的 `plugin._werewolf_games` 内存房间。命中 ledger 新房间的群命令与 HTTP 共用同一 domain，平台 message id 作为 action id；旧房间继续由旧生命周期处理到结束。真实前端 WebSocket、跨平台群命令和管理裁判台尚未 E2E 验收；
- 重启只会重新调度 `accepted`。进程退出前已经进入 `executing` 而无法证明投递结果的命令会转为 `delivery_unknown`，不得由客户端或服务端自动盲重试；应先查询外部系统或领域 receipt，再由具有明确幂等证据的领域流程决定后续动作；
- 命令技术状态事件与状态迁移在同一 SQLite 事务进入既有 `sync_outbox`，当前保持 `private`、`held`，不复制原始 payload，也不建立第二套远程同步；备份恢复后应检查 accepted 恢复、executing 栅栏和 Outbox 连续性；
- `/readiness` 只读聚合已经存在的内存状态，不调用插件或 Adapter 主动 health，不建立连接、不建表、不执行修复，也不访问会创建 Life Engine service 的懒加载属性；配置停用的平台显示 `disabled`，而不是失败或从列表消失；
- `local_ready` 只表示当前已落地的本地关键路径（API 与 Life Event ledger）可用。远程同步不可用或 Adapter 断开会保留在脱敏诊断中并使总体状态为 `degraded`，但不会伪造本地不可用；生产 API mount 存活时 command store 显示 `ready`，未挂载或已关闭时显示 `unavailable`；
- 受信启动器通过 `AuthStore.create_bootstrap_challenge()` 生成绑定 Origin、安装实例和短 TTL 的一次性 challenge；公共 HTTP 不提供匿名 challenge 生成端点；
- 备份认证库时必须同时保护签名密钥；恢复后验证旧撤销 session 仍不可用、refresh 不能重放、ticket 只能消费一次。签名密钥遗失时旧 token 不可恢复，必须按凭据失效事故处理，不得临时生成密钥伪装连续会话。
- API mount 是启动过程中按配置取得的可选资源。关闭时先停止 HTTP 接收、关闭 API mount，再卸载其引用的领域插件，避免请求落入已关闭的插件 ledger；只关闭已经成功取得的 mount。若初始化在挂载前失败或测试使用部分构造的 `Bot`，缺少该属性必须按“从未取得”幂等跳过，不能阻断 MCP、数据库、向量库和日志等后续资源回收。
- 签名值的错误语义区分结构与真实性：非规范 Base64 或无法解析的封装返回 `value_invalid`，结构规范但 HMAC 不匹配返回 `signature_invalid`。篡改测试必须修改签名中完整编码的字符，不能改动无填充 Base64 的尾部保留位后仍固定期待签名错误。

定向验收：

```bash
uv run --group dev python -m pytest test/api/v1 test/kernel/commands test/plugins/life_engine/test_chat_events.py test/plugins/test_message_delivery_event_handlers.py test/core/transport/test_message_sender_bot_sender.py test/plugins/test_napcat_outgoing_sender.py test/plugins/test_feishu_adapter.py -q --no-cov -n 0
```

启用或修改该配置需要用户手工重启 Elysium。本轮开发没有启动或重启运行实例，也没有完成真实前端／Provider 端到端验收。P3-05/P3-06/P3-07 当前结论来自离线契约、API、MessageSender 和 Adapter 回归；真实客户端应另外验证短时会话、scope、stream grant、断点续查、媒体上传/下载、聊天 `media_id` 发送、命令最终状态、Provider capability 和 notice 支持矩阵。直播和语音通话领域接口仍属于 P3-08/P3-09，不能因 P3-07 已提供媒体对象而标记完成。

### 6.5 旧插件路由弃用与迁移期

阶段三统一接口由 `/api/v1` 取代的四组旧插件路由保留运行，但已声明弃用（P3-14）。旧路由仍可用，响应自动附加 `Deprecation`、`Sunset` 与 `Link` 头；`Sunset` 是建议迁移期限，不强制执行删除，也未设置自动下线。

| 旧路由组 | 取代者 | 迁移期限 |
| --- | --- | --- |
| `plugins/livestream/router.py` 的 `/livestream/*` | `/api/v1/livestream/*` | 2027-02-01 |
| `plugins/voice_live/router.py` 的 `/voice-live/*` | `/api/v1/voice-calls/*` | 2027-02-01 |
| `plugins/neko_surface/router.py` 的 `/api/neko-surface/*` | `/api/v1/surfaces/*` | 2027-02-01 |
| `plugins/life_engine/memory/router.py` 的 `/memory_vis/*` | `/api/v1/admin/memory/*` | 2027-02-01 |

约定：

- 弃用标记在 `BaseRouter` 基类声明（`deprecation_notice`／`deprecation_sunset_date`／`deprecation_migration_link`），由中间件自动附加响应头；提示文本按 RFC 5987 编码，避免非 latin-1 字符破坏 header；
- 弃用标记不改变旧路由的状态码、payload 或授权语义；迁移期旧客户端不受影响；
- `/api/v1` 路由不继承旧插件的弃用头；
- 迁移期结束后是否删除旧路由需另行决策，当前不自动删除；删除前必须保证新路由功能与授权等价（阶段三 §24 铁律）。

### 6.6 阶段三文档产物

- `docs/api/openapi.json`：完整 OpenAPI schema（由 `scripts/generate_api_openapi.py` 生成，只注册路由不执行 endpoint）；
- `docs/api/events.md`：事件目录；
- `docs/api/errors.md`：错误码目录；
- `docs/api/permissions.md`：权限矩阵与实现状态；
- `docs/api/frontend-example.md`：前端最小参考实现；
- `docs/api/README.md`：目录索引与生成/校验命令；
- `docs/api/verification.md`：阶段三 P3-14 验证报告（已验收/暂不验收/已回退）。

以上文档与实现冲突时以实现为准并更新文档；schema 不含凭据、路径、私聊原文或运行数据。

---

## 7. 配置 LLM Provider、模型与任务

编辑：

```text
config/models.toml
```

### 7.1 概念

- `[providers.*]`：API 服务地址、密钥、客户端类型和超时。
- `[models.*]`：内部模型别名、真实外部模型 ID、Provider 和能力。
- `[tasks.*]`：不同功能的有序模型主备链、输出预算和温度。

`tasks.*.models` 引用的是 `[models]` 下的键，不是外部 `id`。数组顺序就是 `failover` 的配置优先级；模型定义本身在文件中的先后顺序不代表优先级。

### 7.2 OpenAI-compatible Provider 模板

以下仅为结构示例，禁止在文档或 Git 中写入真实密钥：

```toml
[providers.YourProvider]
base_url = "https://provider.example.com/v1"
api_key = "${ELYSIUM_YOUR_PROVIDER_API_KEY}"
client_type = "openai"
max_retry = 3
timeout = 120
retry_interval = 3

[models.internal-model-name]
provider = "YourProvider"
id = "provider-model-id"
ctx = 32768
stream = false
tool_call_compat = false
extra = { enable_thinking = true }

[tasks.expression]
models = ["internal-model-name"]
tokens = 32000
context_tokens = 100000
temp = 0.7
```

在用户准备执行 `doctor` 和手工启动的同一终端中设置 `ELYSIUM_YOUR_PROVIDER_API_KEY`。未设置、空值、明文 key 或示例占位符都会被部署检查拒绝，密钥值不会输出到诊断。

### 7.3 当前文本路由原则

现阶段的路由目标是：

- `tasks.core`：Life Engine 潜意识/心跳模型。
- `tasks.expression`：Life Chatter 对话表达模型。文本请求优先使用任务列表首个模型（当前生产为 DeepSeek-V4-Flash 正式版 `ark-code-latest` 路由）；聊天请求可能直接携带图片/表情，`LLMRequest` 会在发送前按 payload 媒体模态把 `models` 列表过滤到支持对应模态的成员，因此列表内**必须至少保留一个声明并真实支持 `vision = true` 的模型**（生产为 `xiaomi-mimo-v2.5`）承接识图，不能换成纯文本单模型。
- `witness`、`agent`、`utility`、`router`、`router_context_projection` 等任务：根据能力和成本选择纯文本模型。
- `vision`：显式图片/视频观察任务，只能绑定经过媒体协议验收的多模态模型。
- `live`：场景任务可能携带多模态感知；没有完成场景级媒体路由核对前，按多模态任务管理，不随纯文本模型批量切换。
- `voice`：ASR 任务。
- `tts`：TTS 任务。
- `embedding`：向量模型；当前生产注册表要求任务非空，暂不启用时应配置一个经过验证的模型或显式关闭其消费子系统，不能留下空路由。

切换文本 Provider 时应按任务逐项变更，不能全局替换现有模型别名。尤其要保留 `expression`、`vision`、`live`、`voice`、`tts` 和 `embedding` 的能力边界，除非新 Provider 已分别完成图片、场景媒体、ASR、TTS 与 Embeddings 契约验收。Provider 套餐若限制调用场景（例如仅授权 AI 编程工具），部署者还必须先核对服务条款；配置可解析不代表业务用途已获授权。

示例：

```toml
[tasks.core]
models = ["internal-core-model", "internal-core-backup"]
tokens = 32000
context_tokens = 100000
temp = 0.7

[tasks.expression]
models = ["internal-chat-model", "internal-chat-backup"]
tokens = 32000
context_tokens = 200000
temp = 0.7
```

### 7.4 配置检查

至少检查：

1. 每个 `models.*.provider` 都能在 `providers.*` 找到。
2. 每个 `tasks.*.models` 中的名称都存在于 `models.*`，并且同一任务不得重复。
3. `models.*.id` 是 Provider 接受的真实模型 ID，不要把内部前缀拼入外部模型名。
4. 文本模型是否支持 Tool Call；不支持时需要明确评估 `tool_call_compat`，不能盲开。
5. `ctx` 必须与真实模型能力相符；`tasks.*.context_tokens` 是该任务允许使用的输入预算，必须满足 `context_tokens + tokens <= ctx`。主体权威前缀和工具 schema 属于不可静默截断的固定输入：若日志出现 `task context cannot fit without truncating pinned or structured payloads`，先核对模型官方上下文能力和任务预算，禁止通过裁剪 SOUL、USER、MEMORY 规避错误。
6. 视觉、音频和视频不能只改任务名；必须配置并验证媒体能力合同和 Provider 协议。
7. 启动日志必须出现 `模型路由快照已加载`；其中的任务链应与文件一致，摘要不得包含密钥。
8. 若日志显示首选模型被冷却跳过，应核对 `routing_task`、`snapshot`、`configured_primary`、`selected` 和 `skipped`，不要把健康调度误判为乱序。
9. Compact registry 的 `api_key` 必须是单个字符串；密钥轮换应由中转站或显式凭据组件负责，不接受一个实际上不会轮换的伪列表配置。
10. Provider 使用“控制台选择模型”的稳定别名时，`models.*.id` 应填写 Provider 官方要求的别名，而不是把控制台显示的底层模型名误填为预览版路由；同时按该别名官方公布的保守上下文能力设置 `ctx`，不要把某个可选底层模型的最大窗口直接冒充为稳定别名合同。
11. 配置解析测试只证明 schema 与任务引用有效。外部 Provider 冒烟或真实端到端验收还必须分别确认账号授权范围、模型版本、Tool Call、流式输出和错误语义。

### 7.5 Embedding 与记忆索引

Life Engine 的 chunk 向量索引需要一个真正支持 Embeddings API 的模型。具体 Provider、模型名称、接口地址和密钥均属于部署环境配置，不写入公共文档；请按所选 Provider 的官方说明填写 `config/models.toml`。

通用关键项：

```toml
[models.internal-embedding-model]
provider = "YourProvider"
id = "provider-embedding-model-id"
ctx = 8192

[tasks.embedding]
models = ["internal-embedding-model"]
tokens = 8192
temp = 0.0
```

关键约束：

1. `tasks.embedding.models` 中的名称必须对应已定义、可实际调用的 Embedding 模型。
2. 模型实际输出维度必须与活动向量集合一致；更换维度前先制定索引迁移或重建方案。
3. 真实 API 冒烟必须确认返回非空向量且维度正确，但这不等于 Life Memory 全功能验收；记忆写入、索引切换、语义召回和失败恢复仍需分别验证。
4. 不要用普通聊天模型代替 Embedding 模型。空 `tasks.embedding.models` 会在启动验证阶段直接拒绝，而不是等索引器运行后才失败。
5. Provider 地址、模型标识和真实密钥仅写入被 Git 忽略的本机配置，不得写入文档、测试、提交或日志示例。

Life Engine 的索引 worker 默认每 60 秒处理一批，每批最多 4 个任务。历史任务因 Embedding 配置缺失而进入 `failed` 后，可在确认 API 和维度无误的前提下临时设置：

```toml
[memory_index]
retry_failed = true
```

该开关只允许启动后的首批领取失败任务，随后进程内会恢复为不领取失败任务。若失败任务多于单批上限，需要在每次启动后检查实际结果，再决定是否继续重试；全部恢复后将配置改回 `false`。不要通过删除 SQLite 或 ChromaDB 数据来代替正常重试。

### 7.6 ASR/TTS 当前边界

语音链路的通用实现和协议要求为：

- `tasks.voice` 必须指向兼容当前音频输入合同的音频理解或 ASR 模型。飞书入站 Opus 会先转为 16 kHz 单声道 WAV；`MediaManager` 可先尝试原生音频理解，失败后回退 ASR。
- 当前消息 TTS 使用本地微调 **IndexTTS2.5**，由 `tts_voice_plugin:service:tts` 通过本机 `config/plugins/tts_voice_plugin/config.toml` 接入 vLLM-Omni 的 `/v1/audio/speech`。`models.toml` 没有也不需要 `tasks.tts`；Life Chatter 的 `life_send_voice(text=...)` 直接消费 Service，绝不回退旧 `model.toml`、MiMo speech client 或其他模型任务。
- Service 的生产协议是 OpenAI-compatible speech API；历史 GPT-SoVITS `api_v2` 兼容 `/tts` 仅保留为显式回退。长表达会在同一表达内按自然边界分段，vLLM-Omni 默认最多并发 2 段、硬上限 4，随后按原序拼成一条语音；内部片段不形成多次表达，也不分段外发。启用插件时必须安装锁定的 `aiohttp`、`soundfile`、`pedalboard` 依赖，并单独验证本机地址、完整模型 bundle、参考音色和 IndexTTS2.5 revision。
- `[tts].idle_shutdown_seconds` 默认 1800 秒：只对插件通过 `start_command` 创建的后端进程组生效。最后一条完整表达结束并持续闲置到期后释放模型；下一次语音自动按需启动。设为 0 可保持常驻。外部手工服务、正在执行的长表达和 replacement process 不受旧计时影响；不要用定时 kill、端口猜测或 vLLM sleep endpoint 代替 owner 校验。
- N.E.K.O Surface 自动调用同一消息 TTS Service 并按回复顺序播放，显式 TTS 动作在该场景必须抑制。直播使用自己的有界 HTTP TTS 客户端；Voice Live 使用 Realtime Provider，二者都不能冒充消息 TTS 的平台发送回执。
- QQ/NapCat 与飞书共用核心 `voice` 消息段，但出站协议不同：NapCat 映射为 OneBot `record`，飞书转为 Opus 并发送 `audio`。QQ/NapCat 语音收发尚未完成真实端到端验收。

可在不发送平台消息的情况下验证本地合成；脚本只输出字符数、格式、音频字节数与 SHA-256，不落合成正文：

```bash
PYTHONPATH=. .venv/bin/python scripts/verify_local_tts.py
```

具体 Provider 地址、served model name、音色资产路径和插件启停状态属于部署环境配置，不写入公共文档；当前模型家族为 IndexTTS2.5 是项目事实，不应再写成 GPT-SoVITS。

---

## 8. 配置 Life Engine 和灵魂文件

编辑：

```text
config/plugins/life_engine/config.toml
```

最小关键项：

```toml
[settings]
enabled = true
heartbeat_interval_seconds = 30
heartbeat_timeout_seconds = 120
workspace_path = "<Elysium项目目录>/data/life_engine_workspace"

[model]
task_name = "core"
chatter_task_name = "expression"

[chatter]
enabled = true
mode = "enhanced"
max_rounds_per_chat = 5
```

Windows 下建议在 TOML 路径中使用正斜杠；若使用反斜杠，则需在双引号字符串中转义。`workspace_path` 应替换为当前部署环境的实际绝对路径。

### 8.1 `SOUL.md` 是表达硬前提

确保以下文件存在、UTF-8 编码且内容非空：

```text
data/life_engine_workspace/SOUL.md
```

当前 Life Chatter 在 `SOUL.md` 不存在、为空或读取失败时会拒绝生成回复。日志通常包含：

```text
SOUL.md 不可用，life_chatter 拒绝生成回复
```

因此“模型能思考但不说话”时，第一时间检查：

1. `[chatter].enabled = true`。
2. `[model].chatter_task_name` 指向有效任务。
3. `workspace_path` 是否指向预期目录。
4. `SOUL.md` 是否存在、已保存、非空、编码正确。

不要用通用人格兜底替代缺失的 `SOUL.md`。

### 8.2 主体媒体能力

`chat_global` 当前注册以下主体主动能力：

| LLM 可见名称 | 能力 | 边界 |
| --- | --- | --- |
| `tool-nucleus_save_media` | 保存当前会话收到的图片、语音、视频或普通附件到 Life Engine workspace | 只能写入 workspace 内；普通附件只接受适配器已经物化的受控本地引用或合法 Base64，不会自动读取正文 |
| `action-life_send_file` | 向当前聊天表面发送一个已有本地普通文件 | 文件类型开放；拒绝目录、通配符和不可读路径，通用上限 100 MiB；飞书进一步要求非空且不超过 30 MiB |
| `action-life_send_image` | 发送已有本地图片 | 接受绝对路径或 `~` 路径；不负责生成图片 |
| `action-life_send_voice` | 发送已有本地音频文件，或通过本地 `tts_voice_plugin:service:tts` 合成后发送 | 飞书发送使用项目依赖 `imageio-ffmpeg` 转为 Opus；Surface 自动语音场景拒绝重复显式合成 |
| `tool-recognize_voice` | 识别当前会话里的语音 | 优先音频理解，失败时将入站 Opus 转为 WAV 并回退 ASR；实际效果取决于模型协议兼容性 |

这些能力只进入聊天意识工具清单，由主体在理解上下文后主动选择。不得增加关键词匹配、消息类型触发器或“收到图片/语音就自动调用”的机械规则。

当前离线契约覆盖注册边界、QQ/KOOK/飞书普通附件物化、普通附件保存与三平台文件出站，以及飞书图片出站、飞书语音入站资源下载、ASR/TTS 协议和平台消息段转换。飞书私聊中的保存图片、发送图片、语音合成发送和语音接收识别四项均已完成真实端到端验收；群聊、视频和普通文件真实闭环仍未验收。

### 8.3 可选能力先按需关闭

新部署建议先跑通最小文本链路，再逐项打开：

- 记忆索引
- 第一人称见证
- 好奇层
- 网络搜索
- MCP
- 原生多模态
- 屏幕观察
- Minecraft
- 子代理编排

每打开一项，都要记录依赖、配置、资源预算、权限和验证结果。

---

## 9. 配置飞书应用机器人

插件说明亦见：

- [`plugins/feishu_adapter/README.md`](../../plugins/feishu_adapter/README.md)

### 9.1 飞书开放平台操作与权限

1. 创建企业自建应用并启用机器人能力。
2. 在“权限管理”中按下表开通**应用身份权限（tenant scope）**。权限名称以标识为准，飞书后台中文显示可能随版本调整。

| 场景 | 权限标识 | 当前要求 | 说明 |
| --- | --- | --- | --- |
| 私聊接收消息 | `im:message.p2p_msg:readonly` | 当前验收需要 | 允许机器人接收用户发来的单聊消息事件 |
| 机器人发送和回复消息 | `im:message:send_as_bot` | 当前验收需要 | 用于发送私聊回复及 reply API |
| 读取消息及下载图片资源 | `im:message:readonly` | 图片查看需要，推荐 | 最小只读方案；资源 API 会据此读取消息中的 `image_key` 内容 |
| 群聊中接收 @ 消息 | `im:message.group_at_msg:readonly` | 可选，当前未验收 | 只在后续验收群聊时开通 |
| 解析用户真实昵称 | `contact:user.base:readonly` | 可选 | 未开通时仍可聊天；无显式别名时显示为稳定的“身份未解析用户” |
| 从当前群解析成员昵称 | `im:chat.members:read` | 群聊身份识别推荐 | 通讯录不可见时的最小只读兜底；缺失时返回 `99991672` |

飞书图片资源下载曾真实返回：

```text
99991672 Access denied
```

该错误响应允许 `im:message:readonly`、`im:message.history:readonly`、`im:message` 三者之一。当前部署应优先开通权限范围最小的 `im:message:readonly`；只有明确需要历史消息或完整消息读写能力时，才考虑后两者。不要为了排障一次性授予全部宽权限。

当前私聊文本与图片查看基线可在“权限管理 → 批量导入/导出权限”中导入：

```json
{
  "scopes": {
    "tenant": [
      "im:message.p2p_msg:readonly",
      "im:message:send_as_bot",
      "im:message:readonly",
      "im:chat.members:read"
    ]
  }
}
```

以上均为应用身份权限，不要求用户身份授权。若之后验收群聊事件或通讯录真实昵称，再分别增加 `im:message.group_at_msg:readonly`、`contact:user.base:readonly`，不要提前扩大权限范围。`im:chat.members:read` 只读取机器人所在会话的成员信息，是当前群聊身份解析的最小推荐权限。

3. 在“事件与回调”中选择“长连接”，添加事件：

```text
im.message.receive_v1
```

4. 在应用“版本管理与发布”中创建新版本并提交审核。
5. 如企业开启管理员审批，在管理后台批准新增权限和应用版本。
6. 确认版本已发布上线，并确认应用可用范围包含测试用户。
7. 从飞书私聊机器人；群聊需另外把机器人加入目标群。

**权限或事件修改后，仅在后台点击保存是不够的。必须重新创建版本、完成审批并发布，运行中的机器人才能获得新权限。**

### 9.2 Elysium 飞书配置

编辑：

```text
config/plugins/feishu_adapter/config.toml
```

模板：

```toml
[plugin]
enabled = true
config_version = "0.1.0"

[app]
app_id = "<FEISHU_APP_ID>"
app_secret = "<FEISHU_APP_SECRET>"
verification_token = ""
encrypt_key = ""
api_base_url = "https://open.feishu.cn"

[connection]
subscription_mode = "long_connection"
auto_start_long_connection = true
long_connection_log_level = "WARNING"

[bot]
bot_open_id = ""
bot_name = "<机器人名称>"

[behavior]
reply_to_message = true
ignore_bot_messages = true
group_list_type = "blacklist"
group_list = []
private_list_type = "blacklist"
private_list = []

[identity]
user_name_aliases = []
canonical_identity_aliases = []
resolve_display_names = true
display_name_cache_ttl = 21600.0
display_name_negative_cache_ttl = 300.0
```

`user_name_aliases` 使用 `open_id或union_id=显示名`；同一账号的两种 ID 应映射到同一个名字。`canonical_identity_aliases` 使用 `open_id或union_id=人物键`，QQ 适配器中的同一人物也配置相同人物键。人物键只能来自人工确认，禁止按昵称或消息内容自动建立跨平台关系。

安全要求：

- `app_secret` 不得出现在文档、日志截图、Issue 或 Git 提交中。
- 如果密钥曾泄露，立即在飞书开放平台轮换，不要只删除文本。
- 长连接不需要公网域名、frp、ngrok 或 cloudflared。
- 当前 HTTP 回调模式不支持加密回调，不要启用 Encrypt Key。
- SDK 的短暂断线、恢复和逐次重试日志会被聚合，不作为故障刷屏。
- 长连接 URL 中的 `access_key`、`ticket` 等临时票据始终脱敏；相同连接错误最多每 5 分钟输出一次。

### 9.3 飞书启动成功标志

启动日志中应看到类似信息：

```text
飞书长连接后台线程已启动
飞书长连接正在连接开放平台
```

随后从飞书私聊向机器人发送一条简单文本，确认日志依次出现：

1. 飞书事件到达。
2. 消息进入统一消息流/Life Engine。
3. `expression` 模型生成回复。
4. 飞书发送接口成功。

### 9.4 飞书图片查看端到端验收

当前已验收“收到并理解图片”，图片保存、图片发送、语音合成发送和语音接收识别也已完成真实飞书端到端验收；普通文件已经完成离线接入，但尚未完成真实飞书闭环，视频仍未接入本轮范围。

1. 确认 9.1 中 `im:message:readonly` 已审批并随新版本发布。
2. 在飞书私聊向机器人发送一张内容明确的新图片。
3. 日志确认飞书资源下载成功，并构造出与 NapCat 相同的 `image` Base64 消息段。
4. 让机器人描述图片中的主体、文字或明显细节，不能只回复“收到图片”。
5. 若 NapCat 能识别同一张图而飞书只能看到 `[图片]`，先检查是否出现 `99991672 Access denied`；不要先修改公共模型合同。

当前已完成真实端到端验收：飞书文本聊天、图片查看、图片保存、图片发送、语音接收识别与语音合成发送均可用。后续若更换飞书权限、语音模型、TTS 音色或音频转码依赖，仍须按 9.5 重新验收对应链路。

### 9.5 飞书媒体能力端到端验收

前置条件：

- 应用身份权限 `im:message:readonly`、`im:message:send_as_bot` 已审批并随新版本发布。
- 项目依赖已安装 `imageio-ffmpeg>=0.6.0`，或启动进程 PATH 中存在支持 `libopus` 的 FFmpeg；不再要求独立安装 `ffprobe`。
- `model_tasks.voice` 指向真实可调用且兼容 `input_audio` 的音频理解或 ASR 模型；仅有模型名不算协议兼容。
- `tts_voice_plugin` 已显式启用，`tts_voice_plugin:service:tts` 可取得，且本机配置指向已验收的 IndexTTS2.5/vLLM-Omni `/v1/audio/speech` 与正确模型 bundle；`config/models.toml` 不需要 `tasks.tts`。
- 插件的可选音频依赖已由锁定环境安装；固定短句能生成非空、可解码音频，日志不打印正文或完整请求体。
- `data/life_engine_workspace/received/` 所在磁盘有足够空间并纳入隐私保护与备份策略。

逐项验收：

1. **保存图片**：发送一张新图片，主体主动调用 `nucleus_save_media`；确认返回路径位于 workspace 内、文件可打开且内容一致。
2. **发送图片**：让主体发送 workspace 内已有图片；确认飞书收到可正常预览的图片，而非路径文本或失败占位。
3. **发送语音**：准备一段短音频，让主体调用 `life_send_voice`；确认飞书收到可播放的 `audio` 消息，时长正常。
4. **识别语音**：向机器人发送一段内容明确的新语音，让主体主动调用 `recognize_voice`；确认资源下载成功，回复包含真实语义而不是只显示 `[语音]`。
5. **保存普通附件**：发送一个内容可核对的非空文件；确认入站消息只携带 `path`、`sha256`、`storage_key` 等引用而不携带正文，主体调用 `nucleus_save_media` 后 workspace 副本与原文件 SHA-256 一致。
6. **发送普通附件**：让主体调用 `life_send_file` 发送 workspace 内一个小于 30 MiB 的非空文件；确认飞书收到 `file` 消息且可正常下载，不能只出现本地路径文本。再以空文件和超限文件各验证一次显式拒绝。

每项分别记录请求时间、输入文件格式、飞书消息类型、关键日志和实际观察结果。任何一项失败都应保持“未验收”，不得用本地 Base64、Mock 或单元测试替代。第 5、6 项在本次代码交付时仍为待执行状态。

### 9.6 本地 HTTP 冒烟

状态接口：

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/feishu/api/status"
```

Git Bash/curl：

```bash
curl "http://127.0.0.1:8000/feishu/api/status"
```

本地注入一条飞书格式消息：

```powershell
$body = @{
    content = "测试消息"
    open_id = "local_user"
    sender_name = "本地飞书用户"
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8000/feishu/api/message" `
    -ContentType "application/json" `
    -Body $body
```

该接口用于本地链路测试，不等于真实飞书出站权限已经验收。

---

## 10. 配置并启动 QQ/NapCat

QQ 接入使用 NapCat + OneBot 11 **反向 WebSocket**：Elysium 启动 WebSocket 服务端并监听本机端口，NapCat 作为客户端主动连接。该链路已完成 QQ 私聊文本和图片查看验收；是否启用由各部署环境自行决定，启用后应重新完成当前版本冒烟。2026-08-04 的 WSL 掉线恢复、版本组合、复合健康判断和真实收发证据见 [NapCat / QQNT 掉线恢复与真实链路验证](../report/napcat-qqnt-recovery-validation-2026-08-04.md)。

### 10.1 账号与目录要求

- NapCat 使用独立机器人 QQ，不使用开发者的个人 QQ。
- 独立机器人 QQ 应有单独的 QQ 安装目录，避免与个人 QQ 的运行目录、账号状态和升级过程相互影响。
- NapCat 本体和机器人 QQ 客户端可以位于不同目录；启动时从 NapCat 目录调用官方 `launcher.bat`。
- 文档、代码和提交中不得记录 QQ 密码、登录凭据或其他账号秘密。

目录应按部署环境自行选择，例如：

```text
<NapCat目录>/              # 包含官方 launcher.bat
<机器人QQ客户端路径>       # 独立机器人 QQ 客户端
<Elysium项目目录>/         # Elysium 后端
```

无论采用何种盘符和目录，都必须继续保持“机器人 QQ 与个人 QQ 隔离”的原则。

### 10.2 配置 Elysium NapCat 适配器

配置文件：

```text
config/plugins/napcat_adapter/config.toml
```

关键配置：

```toml
[plugin]
enabled = true

[bot]
qq_id = "<机器人QQ号>"
qq_nickname = "<机器人昵称>"

[napcat_server]
mode = "reverse"
host = "127.0.0.1"
port = 0  # 替换为部署环境选择的未占用端口
access_token = "<仅写入本机忽略配置的强随机令牌>"
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `plugin.enabled` | 是否加载 NapCat 适配器；由各部署环境根据是否使用 QQ 接入自行设置 |
| `bot.qq_id` | 独立机器人 QQ 号，必须与 NapCat 实际登录账号一致 |
| `bot.qq_nickname` | Elysium 内部使用的机器人昵称 |
| `napcat_server.mode` | 当前固定为 `reverse`，表示 Elysium 监听、NapCat 主动连接 |
| `napcat_server.host` | 同一网络命名空间使用 `127.0.0.1`；NapCat 位于 Docker bridge 时，只绑定宿主的精确 bridge 地址，不绑定所有网卡 |
| `napcat_server.port` | 当前约定为 `<OneBot端口>` |
| `napcat_server.access_token` | NapCat 与 Elysium 两端填写相同值，且不得提交真实令牌；reverse 服务端会在接管连接前校验 `Authorization: Bearer <token>` |

### 10.3 配置 NapCat OneBot 11 客户端

在独立机器人账号对应的 NapCat OneBot 11 网络配置中：

1. 新建或启用 **WebSocket Client / 反向 WebSocket** 配置。
2. 连接地址填写：

   ```text
   ws://127.0.0.1:<OneBot端口>
   ```

3. 保持该配置启用，并允许断线后自动重连。
4. 如果设置 Access Token，必须与 Elysium `access_token` 完全一致；真实令牌不得写入文档或提交。
5. 保存配置后确认 NapCat 使用的是独立机器人 QQ，而不是个人 QQ。

这里不需要额外配置正向 WebSocket 服务端，也不需要为本机连接开放公网端口。`<OneBot端口>` 只用于本机 NapCat 与 Elysium 之间的 OneBot 连接。若 NapCat 运行在 Docker bridge 中，容器内的 `127.0.0.1` 只指向容器自身；此时应让 Elysium 只监听宿主 bridge 地址，并让 WebSocket Client 连接同一地址。不得为了省事把监听暴露到公网网卡。

配置非空 token 后，Elysium reverse 服务端会在设置连接 owner、绑定 NapCat client 或进入监听循环之前，以常量时间比较 Bearer token。缺失或错误 token 会以通用 `Unauthorized` 拒绝，真实 token 不进入响应头、关闭原因或日志。空 token 仅保留旧部署兼容，不是新部署建议。

### 10.4 恢复启用后的正式启动顺序

部署环境需要启用 QQ 接入时，先将 `config/plugins/napcat_adapter/config.toml` 的 `plugin.enabled` 设为 `true`，再按以下顺序启动：

1. 确认 NapCat/QQNT 的生命周期 owner 已启动或自动拉起唯一的机器人实例；需要人工启动时进入 NapCat 目录。
2. Windows 人工启动或自动化 owner 都必须调用官方启动脚本，不得绕过官方启动链：

   ```bat
   cd /d <NapCat目录>
   launcher.bat <机器人QQ号>
   ```

3. 等待独立机器人 QQ 登录完成，并确认 NapCat 已加载该账号的 OneBot 11 配置。此时 Elysium 尚未启动，反向 WebSocket 可以暂时处于等待或自动重连状态。
4. 进入 Elysium 项目目录，在可观察的终端或 VS Code 终端启动后端：

   ```powershell
   cd <Elysium项目目录>
   .\deploy.ps1 doctor
   .\deploy.ps1 run
   ```

5. Elysium 加载 NapCat 适配器并开始监听 `127.0.0.1:<OneBot端口>` 后，NapCat 应自动建立反向 WebSocket 连接。
6. 确认连接完成后再进行 QQ 文本和图片验收。

Windows 部署必须通过 NapCat 官方 `launcher.bat <机器人QQ号>` 启动机器人账号；自动恢复也不得自行替换官方启动链。

Linux/WSL 部署使用已经完成当前版本验收的 QQNT/NapCat 入口。具有明确 owner 的部署机制可以自动启动和恢复；人工前台启动可使用：

```bash
cd <QQNT运行目录>
xvfb-run -a ./qq --no-sandbox
```

无论人工还是自动启动，都必须先确认没有现存实例。如果实例已经运行且 OneBot、反向 WebSocket 和真实消息入站均正常，不得再次执行该命令。不要同时运行两个使用同一账号与会话目录的实例。

### 10.5 启动成功判定

同时满足以下条件，才视为 QQ/NapCat 链路启动成功：

- 独立机器人 QQ 已登录，NapCat 已加载对应账号。
- Elysium 启动无 Fatal error，NapCat 适配器已启用。
- Elysium 正在监听 `127.0.0.1:<OneBot端口>`。
- NapCat 的 WebSocket Client 已连接到 `ws://127.0.0.1:<OneBot端口>`。
- Elysium 能收到来自该 QQ 的 OneBot 私聊消息事件。

### 10.6 文本、图片与普通附件验收

按以下顺序执行真实端到端验收：

1. 使用另一个 QQ 向机器人发送一条新的私聊文本。
2. 确认 Elysium 收到消息、Life Chatter 被唤醒，并由机器人 QQ 返回文本回复。
3. 向机器人发送一张内容清晰的新图片，并附带明确问题，例如“图中主要是什么”。
4. 确认图片进入统一媒体链，模型回复包含图片中的真实主体、文字或明显细节，而不是只回复“收到图片”。
5. 发送一个内容可核对的普通文件，确认 NapCat 取得文件正文后生成 `materialized=true` 的内容寻址引用；超限或下载失败必须明确保持未物化状态。
6. 让主体把该文件保存到 workspace，核对 SHA-256 一致；再让主体发送一个小型本地文件，确认 QQ 收到的是真实文件而不是路径字符串。
7. 验收只记录本次实际观察到的结果，不以配置存在、日志无报错或离线测试通过代替端到端结论。

当前结论：QQ 私聊文本聊天和图片查看已通过真实端到端验收。普通附件已完成离线合同但尚未执行上述真实闭环；QQ 群聊、语音、文件、视频和图片保存仍属于待验收范围。

### 10.7 停止与重启

- 正常停止时，先在 Elysium 前台终端按 `Ctrl+C`，等待适配器和消息流优雅关闭。
- 再按 NapCat/QQ 的正常退出方式关闭机器人账号。
- 完整重启仍遵循“先确认 NapCat 健康，再由用户手动启动 Elysium”。
- 仅重启 Elysium 时，可以保持 NapCat 与机器人 QQ 运行；Elysium 恢复监听后，NapCat WebSocket Client 应自动重连。
- 不同时启动多个使用同一机器人 QQ 的 NapCat 实例，也不同时启动多个监听 `<OneBot端口>` 的 Elysium 实例。

---

## 11. 启动、停止与重启

### 11.1 推荐启动命令

NapCat 是否启用由部署环境决定；2026-08-04 的 WSL 环境已启用并完成真实 QQ 私聊复验。完整启动顺序以 10.4 为准：先启动 NapCat 和独立机器人 QQ，再启动 Elysium。以下命令只负责启动 Elysium 主进程。

PowerShell：

```powershell
.\deploy.ps1 doctor
.\deploy.ps1 run
```

Linux / WSL / Git Bash：

```bash
./deploy.sh doctor
./deploy.sh run
```

`start.bat` 只是 `deploy.ps1 run` 的兼容转发器，不再执行 lease 清理、数据库写入或依赖安装。`run` 只在同仓库 PID、端口、锁定环境、配置和主体权威检查全部通过后，前台执行 `uv run --frozen --no-sync python main.py`。

### 11.2 Elysium 只允许手工前台启动

Elysium 主进程必须由用户在可观察的终端或 VS Code 终端手工启动。当前部署明确禁止为 Elysium 配置 systemd、Windows 服务、计划任务、登录启动项、shell profile 自动命令或其他守护拉起。某些临时后台方式还会因 stdin EOF 或会话退出造成“看似启动、很快消失”，同样不作为 Elysium 的正式启动方式。

NapCat/QQNT 可以由具有明确 owner 的部署机制自动启动和自动恢复。自动恢复必须使用持续的复合故障证据，核对 PID、父进程、运行目录、监听端口和现存实例，并采用有界退避；禁止仅凭单次 `online=false`、单次心跳异常或本地端口状态形成重启循环。

本地 New API 中转站是 LLM 基础设施，按当前机器约定保持自动启动；它也不属于 Elysium 自启动禁令。Elysium 的重试逻辑不会自行拉起该进程。检查生命周期边界见 [活体记忆迁移与健康检查](./living_memory_migration.md)。

### 11.3 停止

在前台终端按：

```text
Ctrl+C
```

等待适配器、任务和数据库优雅关闭。不要在正常情况下直接强杀进程。

### 11.4 重启前检查重复实例

如果新实例提示 8000 端口被占用，先确认是否有旧 Elysium/Python 进程仍在运行。不要通过反复换端口掩盖残留进程，否则飞书长连接可能同时存在多个消费者，日志和回复会混乱。

---

## 12. 启动验收清单

每次新机器部署至少完成以下检查。

### 12.1 基础进程

- [ ] `.venv` 中 Python 版本不低于 3.11。
- [ ] `deploy.sh bootstrap` 或 `deploy.ps1 bootstrap` 成功，依赖与 `uv.lock` 一致。
- [ ] `config/core.toml`、`config/models.toml` 已生成并保存；真实密钥未进入 Git。
- [ ] SQLite 文件可创建或打开。
- [ ] 启动过程无 Fatal error。
- [ ] 仅有一个 Elysium 实例占用目标端口。

### 12.2 HTTP

- [ ] 日志显示 `HTTP 服务器已启动: http://127.0.0.1:8000`。
- [ ] `http://127.0.0.1:8000/docs` 可打开 FastAPI 文档。
- [ ] 飞书状态接口可访问。

### 12.3 LLM

- [ ] Provider 域名和端口可访问。
- [ ] 启动预检通过。
- [ ] 启动日志中的路由快照摘要和有序任务链与 `models.toml` 一致。
- [ ] `core` 和 `expression` 的内部模型名都存在。
- [ ] 日志中没有 `Model does not exist`、401、403、429 或持续超时。
- [ ] 实际调用的外部 `model_identifier` 与 Provider 文档一致。
- [ ] 启用记忆索引时，`embedding` 任务使用真正的向量模型且维度与活动索引一致。
- [ ] Embedding 冒烟请求能返回非空向量，且向量维度与 `embedding_dimension` 和活动索引一致。
- [ ] 日志中没有持续出现 `Embedding 生成失败` 或 `completed=0 failed>0`。

### 12.4 Life Engine

- [ ] `settings.enabled = true`。
- [ ] 心跳按配置间隔运行。
- [ ] `chatter.enabled = true`。
- [ ] `SOUL.md` 存在且非空。
- [ ] 收到消息后 `LifeChatter` 被唤醒。
- [ ] 回复由 `expression` 任务生成，而非错误地全部走 `core`。

### 12.5 飞书端到端

- [x] 飞书应用已发布，机器人能力已启用。
- [x] 长连接事件 `im.message.receive_v1` 已添加。
- [x] `im:message.p2p_msg:readonly`、`im:message:send_as_bot` 已生效。
- [x] 图片读取所需的 `im:message:readonly` 已生效并随版本发布。
- [x] 长连接日志正常。
- [x] 私聊能收发文本。
- [x] 私聊图片资源能下载并完成真实视觉识别。
- [x] 图片保存、图片发送、语音合成发送和语音接收识别已通过真实飞书端到端验收。
- [ ] 普通文件接收、workspace 保存和文件发送完成真实飞书闭环。
- [ ] 群聊和视频仍未验收。

### 12.6 QQ/NapCat 端到端（2026-08-04 WSL 已复验）

- [x] 使用独立机器人 QQ，与个人 QQ 安装和账号隔离。
- [x] NapCat 按平台使用已验收的启动链：Windows 使用官方 `launcher.bat <机器人QQ号>`，Linux/WSL 使用 10.4 所列 QQNT 入口。
- [x] NapCat OneBot 11 WebSocket Client 指向 `ws://127.0.0.1:<OneBot端口>`。
- [x] Elysium NapCat 适配器使用 `reverse` 模式并监听 `<OneBot端口>`。
- [x] 启动顺序为先 NapCat、后 Elysium，连接能够自动建立。
- [x] QQ 私聊文本能收发。
- [x] QQ 私聊图片能进入统一媒体链并完成真实视觉识别。
- [ ] QQ 普通文件接收、workspace 保存和文件发送完成真实闭环。
- [ ] 群聊、语音、视频及图片保存暂不验收。

### 12.7 Ayla 独立应用通道（端到端验收，见接入文档 §8）

- [x] `plugins/ayla_adapter` 已注册，`AylaAdapter.platform == "ayla"`。
- [x] foundation 能识别 `ayla_adapter`（provider=`ayla`）。
- [x] `ProviderFacadeRegistry` 含 `ayla` 映射，`AylaChatFacade.capabilities()` 全 False。
- [x] `_infer_adapter_signature` 对 `platform="ayla"` 命中 `ayla_adapter`；ayla 不进入 virtual send。
- [x] `life_send_text` 在 ayla 流出站返回成功（虚拟确认），不 `ConnectError`/「未找到匹配 Adapter」。
- [x] ayla 流命令返回 `capability_disabled`（不误路由到 feishu/qq/kook）。
- [x] 契约测试全绿（`test_ayla_adapter.py` / `test_ayla_message_sender.py` / API 相关）。
- [x] Ayla 侧 profile `stream_id` 为 `generate_stream_id("ayla", ...)` 独立流，inject/SSE 订阅三处一致（2026-08-12 数据迁移 + 运行时校验）。
- [x] Ayla 真实端到端验收通过（2026-08-12）：Ayla 前端 Playwright 发送 → inject 进标准接收链 → life_engine 回复 → SSE → bridge 投影 → 前端实时显示；刷新后历史持久化。
- [x] 入站可观测日志：`AylaAdapter | INFO | 收到 Ayla 消息…` / `消息接收器 | INFO | <ayla> 汐汐: …` / `UserQuery | INFO | …`（2026-08-12 起）。
- [x] SSE 长连接稳定：heartbeat 独立于 `has_more`，不再 60 秒读超时断线；Elysium 重启后 bridge 走 `reset_session` secret 重签恢复（2026-08-12 起）。

### 12.7.1 Ayla 后端启动（内嵌 SSE 出站投影，2026-08-12 起）

Ayla 后端（`Ayla/backend`）一键启动入口为 `python launcher.py`（Windows 双击仓库根目录 `start_ayla.bat`）。**run_bridge（SSE 出站投影）已内嵌到 Ayla 后端进程**（`apps/elysia_bridge/apps.py::ready()` 启动 daemon 线程），无需独立进程：

- **内嵌机制**：`ELYSIA_BRIDGE_INLINE`（默认 True，`.env` 可关）控制；`ready()` 判定当前进程是 server（runserver/daphne/uvicorn）且配置就绪后启动 daemon 线程跑 `run_bridge_loop`；单实例文件锁 `runtime/elysia_bridge.lock` 防 runserver reload / 多 worker 双启（后启动进程跳过并 warning）。
- **依赖**：Elysium 运行在 `ELYSIA_BASE_URL`（Ayla `.env`，默认 `http://127.0.0.1:8000`）；service credential 落盘 `runtime/elysia_credential.json`（Git 忽略）；Elysium 未启动时内嵌 bridge 有界退避重试，不崩溃。
- **端口占用**：launcher 启动前检查 `AYLA_PORT`（默认 8100），被占用则报告监听 PID 并拒绝启动（不偷偷起第二实例）。
- **关闭**：runserver/daphne 进程退出，daemon 线程随之终止（SSE 断连，Elysium 侧幂等 + bridge 重连保护兜底）。
- **拆分调试**：`ELYSIA_BRIDGE_INLINE=False` 时可用 `manage.py runserver` + 独立 `manage.py run_bridge` 分开运行（排障用）。
- **冒烟**：`curl http://127.0.0.1:8100/api/v1/health/` 返回 200；后端日志出现 `内嵌 SSE 出站投影已启动` + `POST /auth/sessions 200` + `GET /events/stream 200` 即 SSE 订阅成功。

---

## 13. 自动化测试

### 13.1 全量测试

```powershell
uv run --group dev python -m pytest test -q --no-cov -n 0
```

`pyproject.toml` 默认启用并行、覆盖率、30 秒超时和严格 marker。全量测试可能受机器资源、外部依赖或尚未完成的功能影响；必须记录本次实际结果，不能引用历史通过数量代替当前验收。

### 13.2 飞书适配器测试

```powershell
uv run --group dev python -m pytest test/plugins/test_feishu_adapter.py -q --no-cov -n 0
```

需要快速排除并行、覆盖率和超时插件干扰时，可定向运行：

```powershell
uv run --group dev python -m pytest `
    -n 0 `
    -p no:timeout `
    -p no:cov `
    -o addopts= `
    test/plugins/test_feishu_adapter.py `
    -q
```

2026-08-01 以单进程、关闭覆盖率运行飞书与 NapCat 定向测试，历史结果为 `52 passed`，NapCat 启动、文本、图片和音频文件子集为 `26 passed`。这些数量只记录当时的离线契约结果；端到端结论以真实验收为准：QQ 聊天与图片查看，以及飞书聊天、图片查看、图片保存、图片发送、语音合成发送和语音接收识别已通过；QQ/NapCat 语音及其他未列能力仍未验收。

飞书发送修复至少应有两个小型契约测试长期保护：

1. 内部 `msg_om_xxx` 引用 ID 在调用飞书 reply API 前还原为 `om_xxx`。
2. 收到私聊并缓存会话后，后续发送优先使用对应 `chat_id`，没有缓存时才回退 `open_id`。

飞书与 NapCat 适配器定向测试：

```powershell
uv run --group dev python -m pytest `
    test/plugins/test_feishu_adapter.py `
    test/plugins/test_napcat_adapter_startup_validation.py `
    test/plugins/test_napcat_image_handler.py `
    test/plugins/test_napcat_audio_file_handler.py `
    test/plugins/test_napcat_outgoing_sender.py `
    -q -n 0 --no-cov
```

### 13.3 Ayla 适配器契约测试

Ayla 注册级契约（接入文档 §8.1）由以下测试保护：

```powershell
uv run --group dev python -m pytest `
    test/plugins/test_ayla_adapter.py `
    test/plugins/test_ayla_message_sender.py `
    test/api/v1/test_chat_commands.py `
    test/api/v1/test_chat_platforms.py `
    test/api/v1/test_foundation_api.py `
    -q -p no:cacheprovider --no-cov
```

2026-08-11 该组测试结果为 `52 passed`（14 个 Ayla 相关 + 38 个 API 相关）。Windows 沙箱下若 pytest 退出阶段触发 safe-delete bulk guard，用 `PYTEST_DEBUG_TEM_ROOT=$TEMP/pytest_tmp_root` + `-p no:cacheprovider -o cache_dir=/dev/null` 规避。

Ayla 侧（跨仓库子模块 `Ayla/backend/apps/elysia_bridge`）契约测试：`test_models.py`/`test_profile_api.py`/`test_inject.py`/`test_elysia_client.py`/`test_outbound.py`/`test_bridge_loop.py` 全绿（platform 默认 `ayla`、stream_id 自动生成、inject 带 `ayla`、SSE 投影过滤匹配）。2026-08-12 补充：桥接方向过滤（`test_loop_skips_received_and_requested_chat_events`，只投影 delivered）与 401 secret 重签恢复（`test_loop_unauth_reissues_from_secret_when_refresh_fails`）。Elysium 侧 `test/api/v1/test_events_api.py` 补充 SSE 心跳独立于 `has_more` 的用例（`test_stream_heartbeat_survives_busy_tail`）。测试环境需安装 `Pillow`（`apps.media` 导入链依赖）；`test_voice_*` 的 `MockTransport.get` 失败为既有 voice 测试环境问题，与 Ayla 接入无关。

该组测试用于适配器离线回归，但测试通过不等于对应能力已完成端到端验收。飞书语音接收识别与语音合成发送之所以列为可用，是因为已经完成真实飞书端到端验收，而不是因为音频相关用例存在；QQ/NapCat 语音仍不能据此宣称可用。

2026-08-04 在 WSL 环境运行上述飞书与 NapCat 组合清单，结果为 `77 passed`；其中四个 NapCat 测试文件的独立子集为 `41 passed`。同日另以真实 QQ 私聊完成消息入站、统一消息处理和文本回复动作验收。

### 13.3 格式检查

```powershell
git diff --check
```

Python 改动还应按项目约定运行相应 `ruff` 和定向测试。

---

## 14. 常见故障排查

### 14.1 `uv` 不在 PATH

现象：

```text
uv: command not found
```

处理：安装或修复 `uv`，确认新终端能执行 `uv --version`，然后重新运行 `bootstrap`。不要绕过 doctor 直接调用虚拟环境解释器。

### 14.2 模块缺失

先执行：

```powershell
.\deploy.ps1 bootstrap --with-dev
```

不要直接向全局 Python 安装依赖。若仅某插件缺包，检查其 `manifest.json`，把依赖纳入 `pyproject.toml` 和 `uv.lock` 后重新 bootstrap；生产配置禁止运行时安装。

### 14.3 HTTP 8000 端口被占用

常见原因是上一次运行未退出或同时从多个终端启动。停止旧实例后再重启。不要让多个飞书长连接实例长期并存。

### 14.4 `Model does not exist`

逐项核对：

- `models[].model_identifier` 是否是 Provider 的真实模型 ID。
- 是否错误地把 provider/internal name 前缀拼入模型 ID。
- API Key 是否属于该服务和套餐。
- Base URL 是否已经包含正确的 `/v1`。

### 14.5 401/403

- API Key/App Secret 错误或已失效。
- 飞书权限未申请、未审批或修改后未重新发布应用。
- 应用可用范围不包含当前用户或群。
- 本地中转站若只有某个渠道持续返回固定 403，应先确认渠道协议或客户端限制；确认永久不兼容后只停用该渠道并保留其他同模型渠道，不要靠延长超时或无限重试掩盖错误。
- New API 同时维护 `channels` 与派生 `abilities` 路由；停用渠道时必须让对应 ability 一并失效并验证真实请求不再选中它。没有任何健康 ability 的模型应暂时移出自动任务候选，但可继续保留注册信息用于恢复探针。

### 14.6 429/502/超时

- 429：套餐、额度、并发或速率限制。
- 502：上游服务或代理临时异常。
- 超时：网络、模型响应慢或配置超时过短。

Provider `timeout` 限制单个模型尝试；`bot.stream_step_timeout` 限制一次 Chatter 步进的完整故障转移链，默认 300 秒。后者必须大于前者并留出至少一次备用模型尝试和状态回滚时间；`stream_restart_threshold` 还必须严格大于步进总预算，默认 360 秒。先查看完整日志、具体渠道和 Provider 状态，不要用无限重试掩盖问题。

### 14.7 心跳正常但没有对话回复

重点检查：

```toml
[chatter]
enabled = true
```

以及：

```toml
[model]
chatter_task_name = "expression"
```

心跳与表达层是两条不同链路。只有心跳启动时，模型可能正常思考，但不会注册 Life Chatter 负责外部回复。

### 14.8 `SOUL.md` 不可用

确认 `workspace_path`、文件名大小写、UTF-8 编码和非空内容。没有 `SOUL.md` 时拒绝表达是当前设计，不应添加通用人格 fallback。

### 14.9 飞书能收到但发送报 `230101`

该错误常见于直接按用户 `open_id` 主动发送受限。当前适配器会缓存私聊入站消息中的 `chat_id`，后续优先向已有会话发送。

若再次出现：

- 确认是当前修复版本代码。
- 确认消息确实先从该私聊会话进入。
- 检查应用发布、可用范围和发送权限。
- 检查是否有旧实例仍运行旧代码。

### 14.10 飞书引用回复失败

内部生命事件 ID 可能是 `msg_om_xxx`，飞书 API 接受的是原始 `om_xxx`。当前适配器发送前会规范化 ID。相关逻辑不得在没有等价替代和契约测试的情况下删除。

### 14.11 飞书图片只能看到 `[图片]`

按顺序排查：

- 在飞书“权限管理”确认应用身份权限 `im:message:readonly` 已开通。
- 确认权限变更后已创建新版本、完成管理员审批并发布；只保存配置不会生效。
- 日志若出现 `99991672 Access denied` 或“飞书图片下载失败”，说明消息资源 API 仍无权读取；飞书允许 `im:message:readonly`、`im:message.history:readonly`、`im:message` 中任一项，优先使用最小只读权限 `im:message:readonly`。
- 确认应用可用范围包含当前用户，且测试的是权限发布后发送的新图片。
- 权限无误后，再检查资源 `image_key`、模型 `media_capabilities` 的 `image` 与 MIME，以及 Provider 是否收到 `image_url` Data URL。

### 14.12 飞书语音合成成功但发送失败

按顺序排查：

- TTS 响应是否包含有效 `choices[0].message.audio.data`。
- Base64 解码后的音频是否是 WAV/MP3 且非空。
- `imageio-ffmpeg` 是否已随项目依赖安装，或 PATH 中的 FFmpeg 是否可用并支持 `libopus`；当前实现不依赖独立 `ffprobe`。
- 飞书文件上传是否返回 `file_key` 和非零时长。
- 最终发送体是否为 `msg_type = "audio"`。

### 14.13 `入口点不存在: plugin.py`（已停用插件）

插件 `manifest.json` 顶层支持：

```json
{
  "enabled": false
}
```

加载器仍会读取清单，但会在构建加载计划时跳过该插件，不再检查或导入入口文件。该机制仅适用于实现仍完整、需要可逆停用的插件；已经正式退役的插件必须同时删除清单、入口、组件与专项测试，不能用 `enabled = false` 长期保留隐式复活路径，也不能用空 `plugin.py` 掩盖未完成实现。

### 14.14 改了配置但运行行为没有变化

- 确认文件已保存。
- 确认修改的是当前项目目录下实际使用的配置。
- 完整重启 Elysium。
- 排查是否存在另一个旧进程。
- 从启动日志确认实际 Provider、任务和插件开关。

---

## 15. 数据、安全与备份

### 15.1 敏感信息

以下内容不得进入 Git、公开文档、Issue 或聊天截图：

- LLM API Key
- 飞书 App Secret
- Verification Token / Encrypt Key
- HTTP API Key
- PostgreSQL 密码
- MCP Token 和第三方服务密钥

`config/` 被忽略只能降低误提交概率，不能替代密钥管理。后续应逐步迁移到环境变量或专用 secret 管理方案。

### 15.2 需要保护的数据

至少关注：

```text
data/Elysium.db
data/life_engine_workspace/
data/training_data_lake/
logs/
config/
```

其中 `data/` 可能包含主体记忆、事件历史和个人对话，不应当成普通缓存删除。

### 15.3 备份原则

- 停止服务后再备份 SQLite 主文件及相关 WAL/SHM 文件，或使用 SQLite 官方在线备份机制。
- 配置备份必须加密并限制访问。
- 恢复测试与备份同等重要；未经恢复验证的备份不能视为可靠。
- 不要覆盖或重建生命工作空间来“修复”单个配置问题。

---

## 16. 日常使用流程

推荐顺序：

1. 拉取代码并检查变更说明。
2. 执行 `deploy.ps1 bootstrap --with-dev` 或 `./deploy.sh bootstrap --with-dev`。
3. 核对部署环境中未提交的 `config/` 和 `data/`，避免误覆盖。
4. 按部署环境需要启用或停用可选适配器；启用 QQ 接入时按 10.4 启动独立机器人 QQ。
5. 执行 doctor，通过后由用户手工前台执行 run。
6. 查看 LLM 预检、HTTP、Life Engine 和已启用适配器的连接日志。
7. 对已启用的消息平台执行文本和媒体冒烟；新启用或恢复的适配器应单独重做对应端到端验收。
8. 如本次改动涉及某功能，运行对应定向测试。
9. 停止时先对 Elysium 使用 `Ctrl+C` 并等待优雅关闭，再按需退出外部适配器和客户端。

---

## 17. 新功能接入与文档维护要求

每接入一项新功能，必须在本文补充以下内容：

1. 功能目的与主体性边界。
2. 外部依赖、版本和资源要求。
3. 配置文件、字段和安全要求。
4. 启动顺序与生命周期。
5. 最小冒烟步骤。
6. 定向契约测试。
7. 端到端验收标准。
8. 常见错误与恢复方法。
9. 数据持久化、备份和隐私影响。
10. 当前状态：未接入、已配置、已测试、已验收。

验证状态必须基于本轮真实执行结果更新，不能因为代码或配置存在就宣称可用。

---

## 18. 后续待补章节

按建议优先级持续完善：

- [x] Windows/WSL/Linux 共用 `.venv` 部署脚本（离线契约通过；真实机器仍需手工前台验收）
- [x] 当前 schema 配置模板与模型环境变量密钥注入
- [ ] 模型 Provider 兼容性矩阵
- [x] ASR 协议接入、Opus/WAV 转码和飞书端到端验收
- [ ] IndexTTS2.5/vLLM-Omni 本地部署、模型/声音 revision、参考音频或命名音色、并发 1/2/4、Service 合同和独立语音发送验收
- [x] QQ/NapCat 与飞书私聊文本、图片查看真实端到端验收
- [x] 飞书图片保存、图片发送、语音合成发送和语音接收识别验收
- [ ] 飞书群聊、普通文件和视频验收
- [ ] QQ/NapCat 语音、文件、视频和图片保存验收
- [ ] QQ/NapCat 部署与恢复路径完善
- [ ] Life Memory、见证、学习、叙事和自主意向的功能测试
- [ ] MCP 服务配置与权限隔离
- [ ] Tavily 网络搜索配置
- [ ] 原生图片/视频/音频能力矩阵
- [ ] 屏幕观察的隐私和平台差异
- [ ] Minecraft 具身体验部署
- [ ] 直播和实时语音场景意识接入
- [ ] PostgreSQL 部署、迁移、备份与恢复
- [ ] 日志轮转、监控、告警与长期运行
- [x] Windows 手工前台启动入口（明确不引入服务/计划任务自启动）
- [ ] 发布前全功能验收表

---

## 19. 维护记录

| 日期 | 阶段 | 变更 |
| --- | --- | --- |
| 2026-08-13 | 安全部署脚本 | 新增跨平台 bootstrap/doctor/run/backup；配置 create-only、主体权威只读校验、模型密钥环境引用、可选插件默认关闭，移除 Elysium systemd/Docker 自动重启资产 |
| 2026-08-01 | 阶段一 | 建立 Windows `.venv`、文本模型、Life Engine、飞书长连接的部署运行基线；明确 ASR/TTS 尚未验收 |
| 2026-08-02 | 验收基线 | QQ 聊天、飞书聊天、QQ 图片查看、飞书图片查看完成真实验收；回退图片保存注册改动，其他功能统一暂不验收；补全飞书最小权限、审批发布和 `99991672` 排障说明 |
| 2026-08-02 | Embedding 配置 | 补充 Embedding 通用配置、真实 API 冒烟要求、索引维度约束、历史失败任务单批重试和验收检查项 |
| 2026-08-02 | 媒体能力接入 | 为聊天意识注册保存媒体、发送图片、发送已有语音和识别语音能力；补飞书图片出站与语音入站资源链、离线契约和真实端到端验收标准 |
| 2026-08-02 | 飞书媒体验收与 TTS 边界 | 记录飞书图片保存/发送、语音合成发送和语音接收识别均已真实验收；当时的任务式 TTS 结论已被 2026-08-17 现场审计纠正 |
| 2026-08-03 | 依赖安装与环境恢复 | 补充无 `uv` 时通过项目 `.venv` 安装完整依赖、为插件安装器补装并暴露项目级 `uv`、`pip check` 与启动导入验收，以及损坏环境改名备份、重建和回退流程 |
| 2026-08-04 | TTS 文档过渡 | 将当前模型记为 IndexTTS2，但错误保留了不存在的 `tasks.tts` 路由；该偏差于 2026-08-17 修正 |
| 2026-08-17 | 本地 TTS 路由收口 | 移除 Life Chatter 的 MiMo/`tasks.tts` 遗留调用，统一到启用的本地 TTS Service；明确消息、Surface、直播与 Voice Live 的场景所有权 |
| 2026-08-04 | 阶段三 P3-07 | 接入 8 个受管媒体端点、`runtime/media/` 持久化、resource grant、完整性校验、既有 `sync_outbox` 和聊天图片/语音 `media_id` resolver；明确尚未做真实客户端/Provider E2E |
| 2026-08-05 | 阶段三 P3-08 | 接入直播状态、场次与事件历史、5 类耐久命令和统一 stage ticket/WS；保持手工开播、observer 只读、平台断线 degraded，并明确 B站弹幕写能力尚未具备真实凭据与 E2E |
| 2026-08-05 | 阶段三 P3-09 | 接入语音通话耐久登记、状态/转写查询、4 类耐久命令、资源绑定 ticket 和 participant/observer WS；保留 PCM16 二进制协议与旧路由，明确真实 Provider/双向音频/重连 E2E 暂未验收 |
| 2026-08-07 | 阶段三 P3-10 | 接入狼人杀四类授权投影、追加式 ledger、revision snapshot/action 幂等、ledger 恢复、用户层 REST/WS，并让新房间群命令与 HTTP 共用 domain；明确旧内存房间不迁移，管理裁判台及真实客户端/跨平台 E2E 暂未验收 |
| 2026-08-09 | 阶段三 P3-11/P3-12/P3-13 | 接入管理总览/访问/集成/jobs 与 consciousness/world/memory/commitments/autonomy/surfaces/abilities 端点，补全 scope×resource 权限矩阵、限流与并发/上传/WS 预算、秘密扫描与故障恢复测试；管理路由要求全能管理员身份；明确部分管理领域（chat 管理、voice 监督、media 管理、tabletop 裁判台、memory 详情）仍为 planned/experimental |
| 2026-08-09 | 阶段三 P3-14 | 旧插件路由声明弃用并附加 Deprecation/Sunset/Link 头（迁移期至 2027-02-01，不自动删除）；生成完整 OpenAPI（134 操作、无重复 operation id）与事件目录/错误码/权限矩阵/前端示例文档；本轮仅离线契约与文档，未做真实前端/Provider E2E |
| 2026-08-11 | 依赖修复 | `life_engine` 加载失败修复：SQLAlchemy 2.0.46 的 asyncmy pre-ping 缺参 bug（issue #13306）已通过升级到 2.0.51 解决；在 4.2 补充 SQLAlchemy ≥2.0.50 版本要求与脏 pymysql 环境说明 |
