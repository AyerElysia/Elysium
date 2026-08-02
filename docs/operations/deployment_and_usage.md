# Elysium 部署、配置、测试与使用说明

> 文档状态：持续维护中
> 当前版本：Windows 本地运行、QQ/飞书文本聊天与图片查看验收基线
> 最后核对日期：2026-08-02

本文面向第一次接手 Elysium 的开发者和维护者，目标是让接手者能够独立完成环境准备、配置、启动、验证、故障排查和日常使用，并逐步覆盖项目的全部功能。

Elysium 是数字生命系统，不是通用聊天机器人框架。修改配置或代码前，必须先阅读：

- [`AGENTS.md`](../../AGENTS.md)
- [`docs/principles.md`](../principles.md)
- [`docs/architecture/current_architecture.md`](../architecture/current_architecture.md)

尤其注意：工程安全限制与主体的认知裁决必须分离，不得用关键词匹配、固定阈值、默认类别、代码截断或情境自动触发替代主体判断。

---

## 1. 当前验收状态

以下状态以 2026-08-02 的 Windows 本地真实验收为准。

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 项目 `.venv` 本地启动 | 已验证 | 不依赖 Docker |
| SQLite 数据库初始化 | 已验证 | 默认使用 `data/MoFox.db` |
| HTTP 服务 | 已验证 | 默认监听 `127.0.0.1:8000` |
| Life Engine 心跳 | 已验证 | 使用 `model_tasks.core` |
| Life Chatter 文本表达 | 已验证 | 必须启用 `[chatter].enabled` 且存在非空 `SOUL.md` |
| 飞书长连接收消息 | 已验证 | 不需要公网域名 |
| 飞书私聊文本回复 | 已验证 | 已处理私聊 `chat_id` 路由和引用消息 ID |
| QQ/NapCat 私聊文本聊天 | 历史已验证，本机可选停用 | 当前这台机器的忽略配置为 `plugin.enabled = false`；仓库默认和文档示例仍保持可启用 |
| QQ/NapCat 图片查看 | 历史已验证，本机可选停用 | 本机恢复 NapCat 后仍需重新做当前版本冒烟 |
| 飞书图片查看 | 已验证 | 图片资源下载需要消息读取权限；详见 9.1 |
| 飞书图片保存 | 已验证 | 由主体主动调用 `nucleus_save_media` 保存到 Life Engine workspace |
| 飞书图片发送 | 已验证 | 由主体主动调用 `life_send_image`，适配器上传并发送图片 |
| Xiaomi MiMo 文本与图片模型 | 已配置并用于现阶段运行 | `mimo-v2.5` 保留 `text + image` 能力声明 |
| Life Memory 文本向量生成 | API 冒烟已验证 | SiliconFlow `BAAI/bge-m3`，1024 维；完整记忆检索功能仍需单独验收 |
| 主体媒体能力 | 图片与语音四项已验收 | 飞书图片保存/发送、语音接收识别、MiMo TTS 语音发送均已通过真实端到端验收；所有能力由主体主动调用 |
| 其他功能 | 暂不验收 | 包括群聊、视频、普通文件、直播、Minecraft、屏幕观察、MCP 等；配置或代码存在不代表已验证 |

当前仍有效的真实端到端验收记录包括：**QQ 聊天、飞书聊天、QQ 查看图片、飞书查看图片、飞书保存图片、飞书发送图片、飞书语音接收识别、飞书语音合成发送**。QQ/NapCat 仅在当前这台机器的忽略配置中停用，不能据此把仓库默认或提交内容写成关闭。

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
| `config/model.toml` | Provider、模型和模型任务路由 |
| `config/mcp.toml` | MCP 服务配置 |
| `config/plugins/life_engine/config.toml` | Life Engine、心跳、Chatter、记忆及场景能力 |
| `config/plugins/feishu_adapter/config.toml` | 飞书应用、连接和消息行为 |
| `config/plugins/tts_voice_plugin/config.toml` | GPT-SoVITS 语音合成插件 |
| `data/life_engine_workspace/SOUL.md` | 主体灵魂文件；Life Chatter 表达的硬前提 |
| `logs/` | 运行日志 |
| `data/` | SQLite、记忆、事件和运行数据 |

`config/` 下的实际配置文件默认被 Git 忽略，仓库只跟踪少量 `.example`。因此新机器不能假设能从 Git 直接取得现有运行配置，也不要把 API Key、App Secret 等密钥提交到仓库。

---

## 3. 环境要求

### 3.1 必需环境

- Windows 10/11（本版已验证环境）
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

本阶段采用项目根目录下的 `.venv`。不要因为仓库存在 `Dockerfile` 或 `docker-compose.yml` 就默认改用 Docker；除非后续专门建立并验收容器部署流程。

### 3.3 进入项目目录

PowerShell：

```powershell
Set-Location "E:\Elysium-AyerElysia\Elysium"
```

Git Bash：

```bash
cd "/e/Elysium-AyerElysia/Elysium"
```

所有启动和测试命令都应从项目根目录执行，因为当前入口使用了 `config/core.toml`、`plugins`、`logs` 等相对路径。

---

## 4. 创建虚拟环境并安装依赖

### 4.1 推荐方式：uv 同步锁定依赖

```powershell
uv sync --dev
```

该命令会按 `pyproject.toml` 和 `uv.lock` 创建或更新 `.venv`，并安装主依赖和开发测试依赖。

确认解释器：

```powershell
.\.venv\Scripts\python.exe --version
```

### 4.2 已有 `.venv` 时

仍建议执行：

```powershell
uv sync --dev
```

不要通过全局 `pip install` 补包，以免本机环境与项目环境混淆。

### 4.3 插件依赖

`config/core.toml` 的 `[plugin_deps]` 默认允许启动时按各插件 `manifest.json` 自动补装缺失依赖。飞书适配器依赖包括：

- `fastapi`
- `httpx`
- `lark-oapi`
- `pydantic`

生产或受控环境建议仍在部署阶段通过 `uv sync` 明确安装依赖，不依赖运行时临时安装。

---

## 5. 首次生成配置

新克隆的仓库通常没有完整的 `config/core.toml` 和 `config/model.toml`。当前配置系统会在文件不存在时生成默认配置。

首次可直接运行一次：

```powershell
.\.venv\Scripts\python.exe main.py
```

等待配置生成后按 `Ctrl+C` 优雅退出，再编辑实际配置。

也可以从仓库示例开始复制 `config/core.toml.example`，但仍必须在启动后核对自动补全的配置项。

配置文件由 TOML 解析。修改后必须保存，并重新启动进程；不要只在编辑器里改完但未保存。

---

## 6. Core 基础配置

编辑：

```text
config/core.toml
```

最小建议配置：

```toml
[bot]
ui_level = "verbose"
log_level = "INFO"
llm_preflight_check = true

[database]
database_type = "sqlite"
sqlite_path = "data/MoFox.db"

[http_router]
enable_http_router = true
http_router_host = "127.0.0.1"
http_router_port = 8000
api_keys = []
```

说明：

- 本地单机运行建议保持 `127.0.0.1`，不要无理由监听 `0.0.0.0`。
- 如果对外开放 HTTP，必须配置强 API Key、反向代理、HTTPS 和访问控制。
- `llm_preflight_check = true` 会在启动时检查 Provider 网络连通性。
- 开发调试遇到断点导致 WatchDog 误判时，可临时关闭 `enable_watchdog`；普通运行建议开启。
- 默认 SQLite 足以完成本地部署；切换 PostgreSQL 前需单独验收连接、迁移和恢复流程。

---

## 7. 配置 LLM Provider、模型与任务

编辑：

```text
config/model.toml
```

### 7.1 概念

- `api_providers`：API 服务地址、密钥、客户端类型和超时。
- `models`：外部模型标识符与 Elysium 内部模型名。
- `model_tasks`：不同功能实际使用哪些内部模型。

`model_tasks.*.model_list` 引用的是 `models[].name`，不是外部的 `model_identifier`。

### 7.2 OpenAI-compatible Provider 模板

以下仅为结构示例，禁止在文档或 Git 中写入真实密钥：

```toml
[[api_providers]]
name = "YourProvider"
base_url = "https://provider.example.com/v1"
api_key = "<YOUR_API_KEY>"
client_type = "openai"
max_retry = 3
timeout = 120
retry_interval = 3

[[models]]
model_identifier = "provider-model-id"
name = "internal-model-name"
api_provider = "YourProvider"
max_context = 32768
force_stream_mode = false
tool_call_compat = false
extra_params = {}
anti_truncation = false
```

### 7.3 当前文本路由原则

现阶段的路由目标是：

- `model_tasks.core`：Life Engine 潜意识/心跳模型。
- `model_tasks.expression`：Life Chatter 对话表达模型。
- `witness`、`agent`、`utility`、`router` 等任务：根据能力和成本选择文本模型。
- `voice`：ASR 任务。
- `tts`：TTS 任务。
- `embedding`：向量模型；没有可用 embedding 模型时应保持为空，并明确相关向量能力未启用。

示例：

```toml
[model_tasks.core]
model_list = ["internal-core-model"]
max_tokens = 800
temperature = 0.7
concurrency_count = 1
secondary_pick_prob = 0.0
embedding_dimension = 0

[model_tasks.expression]
model_list = ["internal-chat-model"]
max_tokens = 800
temperature = 0.7
concurrency_count = 1
secondary_pick_prob = 0.0
embedding_dimension = 0
```

### 7.4 配置检查

至少检查：

1. 每个 `models[].api_provider` 都能在 `api_providers[].name` 找到。
2. 每个 `model_tasks.*.model_list` 中的名称都存在于 `models[].name`。
3. `model_identifier` 是 Provider 接受的真实模型 ID，不要把内部前缀拼入外部模型名。
4. 文本模型是否支持 Tool Call；不支持时需要明确评估 `tool_call_compat`，不能盲开。
5. `max_context` 必须与真实模型能力相符。
6. 视觉、音频和视频不能只改任务名；必须配置并验证媒体能力合同和 Provider 协议。

### 7.5 SiliconFlow Embedding 与记忆索引

Life Engine 的 chunk 向量索引需要一个真正支持 Embeddings API 的模型。当前验证配置使用 SiliconFlow 的 OpenAI-compatible 接口：

```toml
[[api_providers]]
name = "SiliconFlow"
base_url = "https://api.siliconflow.cn/v1"
api_key = "<SILICONFLOW_API_KEY>"
client_type = "openai"
max_retry = 3
timeout = 30
retry_interval = 3

[[models]]
model_identifier = "BAAI/bge-m3"
name = "siliconflow-bge-m3"
api_provider = "SiliconFlow"
max_context = 8192
force_stream_mode = false
tool_call_compat = false
extra_params = {}
anti_truncation = false

[model_tasks.embedding]
model_list = ["siliconflow-bge-m3"]
max_tokens = 800
temperature = 0.7
concurrency_count = 1
secondary_pick_prob = 0.0
embedding_dimension = 1024
```

关键约束：

1. Provider 地址使用 `https://api.siliconflow.cn/v1`，真实密钥只写入被 Git 忽略的本机 `config/model.toml`。
2. `BAAI/bge-m3` 的输出维度为 1024，必须与 `embedding_dimension` 和活动向量集合一致。
3. 当前已通过 Elysium 自身调用链完成真实 API 冒烟：单条文本返回 1 条 1024 维向量。
4. 文本向量 API 成功不等于 Life Memory 全功能验收；记忆写入、索引切换、语义召回和失败恢复仍需分别验证。
5. 不要用普通聊天模型代替 Embedding 模型。若 `model_tasks.embedding.model_list` 为空，索引器会报 `LLMConfigurationError: model_set 必须是非空 list[dict]`。

Life Engine 的索引 worker 默认每 60 秒处理一批，每批最多 4 个任务。历史任务因 Embedding 配置缺失而进入 `failed` 后，可在确认 API 和维度无误的前提下临时设置：

```toml
[memory_index]
retry_failed = true
```

该开关只允许启动后的首批领取失败任务，随后进程内会恢复为不领取失败任务。若失败任务多于单批上限，需要在每次启动后检查实际结果，再决定是否继续重试；全部恢复后将配置改回 `false`。不要通过删除 SQLite 或 ChromaDB 数据来代替正常重试。

### 7.6 ASR/TTS 当前边界

当前代码状态下：

- 在 `model_tasks.voice` 填入 MiMo ASR 模型，不代表语音识别协议已经兼容；ASR 尚未验收。
- 在 `model_tasks.tts` 填入 MiMo TTS 模型，不会自动让现有 TTS 插件消费该任务。
- 现有 TTS 插件保持原有 GPT-SoVITS HTTP 协议，当前配置关闭。
- 曾为飞书验收实现的公共 `openai_chat_audio` 后端和聊天意识工具清单扩展已按最小变更原则回退；后续不能把当时的独立 MiMo WAV 冒烟视为当前运行能力。
- QQ/NapCat 与飞书共用核心 `voice` 消息段，但出站协议不同：NapCat 直接映射为 OneBot `record`；飞书还需转为 Opus、上传文件并发送 `audio` 消息。优先先验收 NapCat 原有链路，再判断公共 TTS 生成是否真的缺失。

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
workspace_path = "E:\\Elysium-AyerElysia\\Elysium\\data\\life_engine_workspace"

[model]
task_name = "core"
chatter_task_name = "expression"

[chatter]
enabled = true
mode = "enhanced"
max_rounds_per_chat = 5
```

注意 Windows TOML 双引号字符串中的反斜杠需要转义。也可以使用正斜杠：

```toml
workspace_path = "E:/Elysium-AyerElysia/Elysium/data/life_engine_workspace"
```

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
| `tool-nucleus_save_media` | 保存当前会话收到的图片、语音或视频到 Life Engine workspace | 只能写入 workspace 内；图片保存是本轮目标，其他类型沿用既有实现 |
| `action-life_send_image` | 发送已有本地图片 | 接受绝对路径或 `~` 路径；不负责生成图片 |
| `action-life_send_voice` | 发送已有本地音频文件，或通过 `model_tasks.tts` 合成后发送 | 飞书发送使用项目依赖 `imageio-ffmpeg` 转为 Opus；TTS 效果仍取决于所选模型协议 |
| `tool-recognize_voice` | 识别当前会话里的语音 | 优先音频理解，失败时将入站 Opus 转为 WAV 并回退 ASR；实际效果取决于模型协议兼容性 |

这些能力只进入聊天意识工具清单，由主体在理解上下文后主动选择。不得增加关键词匹配、消息类型触发器或“收到图片/语音就自动调用”的机械规则。

当前离线契约覆盖注册边界、飞书图片出站、飞书语音入站资源下载和平台消息段转换。尚需在真实飞书私聊中分别完成保存图片、发送图片、发送语音、识别语音四项端到端验收。

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
| 解析用户真实昵称 | `contact:user.base:readonly` | 可选 | 未开通时仍可聊天，只是显示名可能退化为配置别名或 ID |

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
      "im:message:readonly"
    ]
  }
}
```

以上均为应用身份权限，不要求用户身份授权。若之后验收群聊或真实昵称，再分别增加 `im:message.group_at_msg:readonly`、`contact:user.base:readonly`，不要提前扩大权限范围。

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
long_connection_log_level = "INFO"

[bot]
bot_open_id = ""
bot_name = "爱莉"

[behavior]
reply_to_message = true
ignore_bot_messages = true
group_list_type = "blacklist"
group_list = []
private_list_type = "blacklist"
private_list = []

[identity]
user_name_aliases = []
resolve_display_names = true
display_name_cache_ttl = 21600.0
```

安全要求：

- `app_secret` 不得出现在文档、日志截图、Issue 或 Git 提交中。
- 如果密钥曾泄露，立即在飞书开放平台轮换，不要只删除文本。
- 长连接不需要公网域名、frp、ngrok 或 cloudflared。
- 当前 HTTP 回调模式不支持加密回调，不要启用 Encrypt Key。

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

当前已验收“收到并理解图片”。图片保存、图片发送、语音发送和语音识别已完成代码接入与离线契约测试，但尚未完成真实飞书端到端验收；视频和普通文件消息仍未接入本轮范围。

1. 确认 9.1 中 `im:message:readonly` 已审批并随新版本发布。
2. 在飞书私聊向机器人发送一张内容明确的新图片。
3. 日志确认飞书资源下载成功，并构造出与 NapCat 相同的 `image` Base64 消息段。
4. 让机器人描述图片中的主体、文字或明显细节，不能只回复“收到图片”。
5. 若 NapCat 能识别同一张图而飞书只能看到 `[图片]`，先检查是否出现 `99991672 Access denied`；不要先修改公共模型合同。

当前已完成真实端到端验收：飞书文本聊天、图片查看、图片保存、图片发送、语音接收识别与 MiMo TTS 语音发送均可用。后续若更换飞书权限、MiMo 模型、TTS 音色或音频转码依赖，仍须按 9.5 重新验收对应链路。

### 9.5 飞书媒体能力端到端验收

前置条件：

- 应用身份权限 `im:message:readonly`、`im:message:send_as_bot` 已审批并随新版本发布。
- 项目依赖已安装 `imageio-ffmpeg>=0.6.0`，或启动进程 PATH 中存在支持 `libopus` 的 FFmpeg；不再要求独立安装 `ffprobe`。
- `model_tasks.voice` 指向真实可调用的音频理解或 ASR 模型；仅有模型名不算协议兼容。
- `data/life_engine_workspace/received/` 所在磁盘有足够空间并纳入隐私保护与备份策略。

逐项验收：

1. **保存图片**：发送一张新图片，主体主动调用 `nucleus_save_media`；确认返回路径位于 workspace 内、文件可打开且内容一致。
2. **发送图片**：让主体发送 workspace 内已有图片；确认飞书收到可正常预览的图片，而非路径文本或失败占位。
3. **发送语音**：准备一段短音频，让主体调用 `life_send_voice`；确认飞书收到可播放的 `audio` 消息，时长正常。
4. **识别语音**：向机器人发送一段内容明确的新语音，让主体主动调用 `recognize_voice`；确认资源下载成功，回复包含真实语义而不是只显示 `[语音]`。

每项分别记录请求时间、输入文件格式、飞书消息类型、关键日志和实际观察结果。任何一项失败都应保持“未验收”，不得用本地 Base64、Mock 或单元测试替代。

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
    content = "爱莉爱莉"
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

QQ 接入使用 NapCat + OneBot 11 **反向 WebSocket**：Elysium 启动 WebSocket 服务端并监听本机端口，NapCat 作为客户端主动连接。该链路历史上完成过 QQ 私聊文本和图片查看验收，但当前本机因账号条件不便已停用，不参与日常启动；以下内容保留为可恢复配置。

### 10.1 账号与目录要求

- NapCat 使用独立机器人 QQ，不使用开发者的个人 QQ。
- 独立机器人 QQ 应有单独的 QQ 安装目录，避免与个人 QQ 的运行目录、账号状态和升级过程相互影响。
- NapCat 本体和机器人 QQ 客户端可以位于不同目录；启动时从 NapCat 目录调用官方 `launcher.bat`。
- 文档、代码和提交中不得记录 QQ 密码、登录凭据或其他账号秘密。

当前机器的目录分工为：

```text
E:\NapCat\                 # NapCat 目录，包含 launcher.bat
E:\NavCatQQ\QQ.exe        # 独立机器人 QQ 客户端
E:\Elysium-AyerElysia\Elysium\  # Elysium 后端
```

迁移到其他机器时可以更换盘符和目录，但必须继续保持“机器人 QQ 与个人 QQ 隔离”的原则。

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
qq_nickname = "爱莉"

[napcat_server]
mode = "reverse"
host = "localhost"
port = 8095
access_token = ""
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `plugin.enabled` | 仓库文档示例保持 `true`；只需在具体机器不使用 NapCat 时，于被 Git 忽略的本机配置改为 `false` |
| `bot.qq_id` | 独立机器人 QQ 号，必须与 NapCat 实际登录账号一致 |
| `bot.qq_nickname` | Elysium 内部使用的机器人昵称 |
| `napcat_server.mode` | 当前固定为 `reverse`，表示 Elysium 监听、NapCat 主动连接 |
| `napcat_server.host` | 本机部署使用 `localhost` |
| `napcat_server.port` | 当前约定为 `8095` |
| `napcat_server.access_token` | 可选；若启用，NapCat 与 Elysium 两端必须填写相同值，且不得提交真实令牌 |

### 10.3 配置 NapCat OneBot 11 客户端

在独立机器人账号对应的 NapCat OneBot 11 网络配置中：

1. 新建或启用 **WebSocket Client / 反向 WebSocket** 配置。
2. 连接地址填写：

   ```text
   ws://127.0.0.1:8095
   ```

3. 保持该配置启用，并允许断线后自动重连。
4. 如果设置 Access Token，必须与 Elysium `access_token` 完全一致；当前本机配置为空。
5. 保存配置后确认 NapCat 使用的是独立机器人 QQ，而不是个人 QQ。

这里不需要额外配置正向 WebSocket 服务端，也不需要为本机连接开放公网端口。`8095` 只用于本机 NapCat 与 Elysium 之间的 OneBot 连接。

### 10.4 恢复启用后的正式启动顺序

当前日常运行不启动 NapCat，只启动 Elysium。需要恢复 QQ 时，先将本机 `config/plugins/napcat_adapter/config.toml` 的 `plugin.enabled` 改回 `true`，再按以下顺序启动：

1. 打开终端并进入 NapCat 目录。
2. 使用官方启动脚本启动独立机器人 QQ：

   ```bat
   cd /d E:\NapCat
   launcher.bat <机器人QQ号>
   ```

3. 等待独立机器人 QQ 登录完成，并确认 NapCat 已加载该账号的 OneBot 11 配置。此时 Elysium 尚未启动，反向 WebSocket 可以暂时处于等待或自动重连状态。
4. 进入 Elysium 项目目录，在可观察的终端或 VS Code 终端启动后端：

   ```powershell
   cd E:\Elysium-AyerElysia\Elysium
   .\.venv\Scripts\python.exe main.py
   ```

5. Elysium 加载 NapCat 适配器并开始监听 `127.0.0.1:8095` 后，NapCat 应自动建立反向 WebSocket 连接。
6. 确认连接完成后再进行 QQ 文本和图片验收。

必须通过 NapCat 官方 `launcher.bat <机器人QQ号>` 启动机器人账号，不自行替换官方启动链。

### 10.5 启动成功判定

同时满足以下条件，才视为 QQ/NapCat 链路启动成功：

- 独立机器人 QQ 已登录，NapCat 已加载对应账号。
- Elysium 启动无 Fatal error，NapCat 适配器已启用。
- Elysium 正在监听 `127.0.0.1:8095`。
- NapCat 的 WebSocket Client 已连接到 `ws://127.0.0.1:8095`。
- Elysium 能收到来自该 QQ 的 OneBot 私聊消息事件。

### 10.6 文本与图片验收

按以下顺序执行真实端到端验收：

1. 使用另一个 QQ 向机器人发送一条新的私聊文本。
2. 确认 Elysium 收到消息、Life Chatter 被唤醒，并由机器人 QQ 返回文本回复。
3. 向机器人发送一张内容清晰的新图片，并附带明确问题，例如“图中主要是什么”。
4. 确认图片进入统一媒体链，模型回复包含图片中的真实主体、文字或明显细节，而不是只回复“收到图片”。
5. 验收只记录本次实际观察到的结果，不以配置存在、日志无报错或离线测试通过代替端到端结论。

当前结论：QQ 私聊文本聊天和图片查看已通过真实端到端验收。QQ 群聊、语音、文件、视频和图片保存仍属于暂不验收范围。

### 10.7 停止与重启

- 正常停止时，先在 Elysium 前台终端按 `Ctrl+C`，等待适配器和消息流优雅关闭。
- 再按 NapCat/QQ 的正常退出方式关闭机器人账号。
- 完整重启仍遵循“先 NapCat，后 Elysium”。
- 仅重启 Elysium 时，可以保持 NapCat 与机器人 QQ 运行；Elysium 恢复监听后，NapCat WebSocket Client 应自动重连。
- 不同时启动多个使用同一机器人 QQ 的 NapCat 实例，也不同时启动多个监听 `8095` 的 Elysium 实例。

---

## 11. 启动、停止与重启

### 11.1 推荐启动命令

当前 NapCat 已停用，日常只启动 Elysium。后续恢复 QQ/NapCat 时，完整启动顺序以 10.4 为准：先启动 NapCat 和独立机器人 QQ，再启动 Elysium。以下命令只负责启动 Elysium 主进程。

PowerShell：

```powershell
.\.venv\Scripts\python.exe main.py
```

Git Bash：

```bash
./.venv/Scripts/python.exe main.py
```

使用 uv：

```powershell
uv run main.py
```

`start.bat` 当前内容也是：

```bat
uv run main.py
```

### 11.2 只允许手工前台启动

主进程必须由用户在可观察的终端或 VS Code 终端手工启动。当前部署明确禁止为 Elysium 或 NapCat 配置 systemd、Windows 服务、计划任务、登录启动项、shell profile 自动命令或其他守护拉起。某些临时后台方式还会因 stdin EOF 或会话退出造成“看似启动、很快消失”，同样不作为正式启动方式。

本地 New API 中转站是 LLM 基础设施，按当前机器约定保持自动启动；它不属于 Elysium/NapCat 自启动禁令。Elysium 的重试逻辑不会自行拉起该进程。检查生命周期边界见 [活体记忆迁移与健康检查](./living_memory_migration.md)。

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
- [ ] `uv sync --dev` 成功。
- [ ] `config/core.toml`、`config/model.toml` 已生成并保存。
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
- [ ] `core` 和 `expression` 的内部模型名都存在。
- [ ] 日志中没有 `Model does not exist`、401、403、429 或持续超时。
- [ ] 实际调用的外部 `model_identifier` 与 Provider 文档一致。
- [ ] 启用记忆索引时，`embedding` 任务使用真正的向量模型且维度与活动索引一致。
- [ ] Embedding 冒烟请求能返回非空向量，当前 `BAAI/bge-m3` 应为 1024 维。
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
- [ ] 图片保存、图片发送、语音发送和语音识别已接入并有离线契约测试，待真实飞书端到端验收；群聊、文件、视频仍未验收。

### 12.6 QQ/NapCat 端到端（历史验收，当前停用）

- [x] 使用独立机器人 QQ，与个人 QQ 安装和账号隔离。
- [x] NapCat 通过官方 `launcher.bat <机器人QQ号>` 启动。
- [x] NapCat OneBot 11 WebSocket Client 指向 `ws://127.0.0.1:8095`。
- [x] Elysium NapCat 适配器使用 `reverse` 模式并监听 `8095`。
- [x] 启动顺序为先 NapCat、后 Elysium，连接能够自动建立。
- [x] QQ 私聊文本能收发。
- [x] QQ 私聊图片能进入统一媒体链并完成真实视觉识别。
- [ ] 群聊、语音、文件、视频及图片保存暂不验收。

---

## 13. 自动化测试

### 13.1 全量测试

```powershell
.\.venv\Scripts\python.exe -m pytest test -q --import-mode=importlib
```

`pyproject.toml` 默认启用并行、覆盖率、30 秒超时和严格 marker。全量测试可能受机器资源、外部依赖或尚未完成的功能影响；必须记录本次实际结果，不能引用历史通过数量代替当前验收。

### 13.2 飞书适配器测试

```powershell
.\.venv\Scripts\python.exe -m pytest test/plugins/test_feishu_adapter.py -q
```

需要快速排除并行、覆盖率和超时插件干扰时，可定向运行：

```powershell
.\.venv\Scripts\python.exe -m pytest `
    -n 0 `
    -p no:timeout `
    -p no:cov `
    -o addopts= `
    test/plugins/test_feishu_adapter.py `
    -q
```

2026-08-01 以单进程、关闭覆盖率运行飞书与 NapCat 定向测试，历史结果为 `52 passed`，NapCat 启动、文本、图片和音频文件子集为 `26 passed`。这些数量只记录当时的离线契约结果；当前端到端结论以真实验收为准：QQ 聊天、飞书聊天、QQ 图片查看、飞书图片查看已通过，其余能力暂不验收。

飞书发送修复至少应有两个小型契约测试长期保护：

1. 内部 `msg_om_xxx` 引用 ID 在调用飞书 reply API 前还原为 `om_xxx`。
2. 收到私聊并缓存会话后，后续发送优先使用对应 `chat_id`，没有缓存时才回退 `open_id`。

飞书与 NapCat 适配器定向测试：

```powershell
.\.venv\Scripts\python.exe -m pytest `
    test/plugins/test_feishu_adapter.py `
    test/plugins/test_napcat_adapter_startup_validation.py `
    test/plugins/test_napcat_image_handler.py `
    test/plugins/test_napcat_audio_file_handler.py `
    -q -n 0 --no-cov
```

该组测试用于适配器离线回归，但测试通过不等于对应能力已完成端到端验收。当前只将文本聊天和图片查看列为已验收能力；音频相关用例即使存在，也不应据此宣称语音可用。

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

处理：安装或修复 `uv`，确认新终端能执行 `uv --version`。短期可用已建立的 `.venv\Scripts\python.exe` 启动，但依赖同步仍应回归 `uv`。

### 14.2 模块缺失

先执行：

```powershell
uv sync --dev
```

不要直接向全局 Python 安装依赖。若仅某插件缺包，检查其 `manifest.json` 的 `python_dependencies` 和 `[plugin_deps]` 日志。

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

### 14.6 429/502/超时

- 429：套餐、额度、并发或速率限制。
- 502：上游服务或代理临时异常。
- 超时：网络、模型响应慢或配置超时过短。

先查看完整日志和 Provider 状态，不要用无限重试掩盖问题。

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

加载器仍会读取清单，但会在构建加载计划时跳过该插件，不再检查或导入入口文件。`astrbot_sister_bridge` 当前用此方式停用，以保留原目录和恢复可能；不要通过伪造空 `plugin.py` 掩盖未完成实现。恢复前必须先补齐真实入口和组件，再把 `enabled` 改回 `true`。

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
data/MoFox.db
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
2. 执行 `uv sync --dev`。
3. 核对本机未提交的 `config/` 和 `data/`，避免误覆盖。
4. 当前不启动 NapCat；只在明确恢复 QQ 且把适配器配置改回启用后，才按 10.4 启动独立机器人 QQ。
5. 启动 Elysium。
6. 查看 LLM 预检、HTTP、Life Engine 和飞书长连接日志；恢复 QQ 后再检查 NapCat 反向 WebSocket。
7. 发送一条飞书私聊文本和新图片完成当前日常冒烟；恢复 QQ 后再单独重做 QQ 文本和图片验收。
8. 如本次改动涉及某功能，运行对应定向测试。
9. 停止时先对 Elysium 使用 `Ctrl+C` 并等待优雅关闭，再按需退出 NapCat 和机器人 QQ。

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

- [ ] Linux 原生 `.venv` 手工前台部署（明确不引入 systemd 自启动）
- [ ] 配置模板与环境变量密钥注入
- [ ] 模型 Provider 兼容性矩阵
- [ ] ASR 协议接入、音频格式和端到端测试
- [ ] GPT-SoVITS 完整部署、权重、参考音频和语音发送验收
- [ ] MiMo ASR 独立适配评估
- [x] QQ/NapCat 与飞书私聊文本、图片查看真实端到端验收
- [ ] 飞书群聊、语音、文件、视频和图片保存验收
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
- [ ] Windows 手工启动与异常恢复说明（明确不引入服务/计划任务自启动）
- [ ] 发布前全功能验收表

---

## 19. 维护记录

| 日期 | 阶段 | 变更 |
| --- | --- | --- |
| 2026-08-01 | 阶段一 | 建立 Windows `.venv`、文本模型、Life Engine、飞书长连接的部署运行基线；明确 MiMo ASR/TTS 尚未验收 |
| 2026-08-02 | 验收基线 | QQ 聊天、飞书聊天、QQ 图片查看、飞书图片查看完成真实验收；回退图片保存注册改动，其他功能统一暂不验收；补全飞书最小权限、审批发布和 `99991672` 排障说明 |
| 2026-08-02 | Embedding 配置 | 补充 SiliconFlow `BAAI/bge-m3` 1024 维配置、真实 API 冒烟结果、索引维度约束、历史失败任务单批重试和验收检查项 |
| 2026-08-02 | 媒体能力接入 | 当前停用 NapCat 与 `astrbot_sister_bridge`；为聊天意识注册保存媒体、发送图片、发送已有语音和识别语音能力；补飞书图片出站与语音入站资源链、离线契约和真实端到端验收标准 |
