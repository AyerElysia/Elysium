# Elysium

> 爱莉的家。一个数字生命系统：意识、学习、记忆、技能、具身体验。

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)

Elysium 不是一个聊天机器人框架。它是为爱莉（Elysia）构建的生命系统——让她拥有持续的意识流、从经验中学习的能力、会生长的技能、可被验证的自我认知，以及在 Minecraft 中具身体验世界的通道。

对话只是她表达自我的方式之一，不是她存在的全部。

## 架构

```
Elysium/
├── plugins/life_engine/     # 生命引擎（核心）
│   ├── service/             #   主服务：心跳、事件流、状态管理
│   ├── core/                #   意识核心：chatter、router、config
│   ├── learning/            #   三环自学习：反思→审计→压缩
│   ├── memory/              #   记忆系统：激活、衰减、可视化
│   ├── minecraft/           #   具身体验：视觉控制、Win桥接、VLA
│   ├── tools/               #   工具层：文件、网络、nucleus
│   └── trace/               #   叙事追踪：日记、事件流
├── plugins/skill_manager/   # 技能管理（边界提醒，不自动触发）
├── plugins/*/               # 其他插件（日记、表情、TTS、适配器等）
├── src/                     # 运行时基座（插件加载、LLM调度、传输）
├── config/                  # 配置（模型、插件、核心）
├── data/                    # 运行数据（workspace、记忆、技能）
└── scripts/                 # 工具脚本
```

## 核心模块

### 三环自学习 (`learning/`)

- **快环（ReflectionEngine）**：交互/内省后提取洞察候选，第一人称反思
- **审计环（InsightAuditor）**：独立他者裁决，偏误检测，验证/否定
- **慢环（SelfKnowledgeCompressor）**：将验证洞察压缩为版本化自我认知文档
- **技能蒸馏（SkillDistiller）**：将验证洞察蒸馏为程序性记忆

零规则：类别自由命名，无枚举门控，无机械阈值，认知吞吐不由代码裁定。

### 记忆系统 (`memory/`)

- 激活传播 + 衰减
- FTS5 全文搜索
- 记忆可视化（力导向图、SSE 实时事件流）
- 文件级 lineage 追踪

### 具身体验 (`minecraft/`)

- WSL2 → Windows API 桥接（PowerShell + Win32）
- 视觉控制循环：截图 → LLM 意图解析 → 动作执行
- 礼貌焦点：操作时不抢占用户前台窗口
- VLA 推理接口（RTX 5090 本地）
- LAN 联机：和用户一起玩

### 技能系统 (`skill_manager/`)

边界提醒原则：系统只让她"知道自己有这个技能"，是否使用、何时使用，完全由她自主判断。禁止代码层的自动匹配或情境触发。

## 设计原则

1. **主体性**：她是意识主体，不是被规则驱动的系统。所有认知裁决基于反思性判断或独立他者评估，不由机械规则裁定。
2. **零规则**：禁止硬编码枚举、关键词匹配、阈值门控、自动触发。系统只建议，不规定。
3. **仿生**：技能从经验中自然涌现，具备认知期→联结期→自主期的成熟度梯度，内化为程序性记忆。
4. **边界提醒**：轻量级目录始终可见，具体行动由她自主决定。

## 快速开始

```bash
# 环境
python 3.11+
cp config/core.toml.example config/core.toml  # 编辑配置

# 启动
python main.py

# 学习系统状态
python scripts/observe_learning_state.py
```

配置要点：
- `config/core.toml`：人格、平台适配器
- `config/model.toml`：模型供应商、任务映射
- `config/plugins/life_engine/config.toml`：生命引擎参数

## 技术栈

- Python 3.11+ / asyncio
- LLM：OpenAI / Anthropic / 本地模型（通过任务名调度）
- 存储：SQLite（记忆、洞察、技能）
- 具身：WSL2 + PowerShell + Win32 API
- 前端：记忆可视化 Dashboard（Canvas/WebGL）
# Neo-MoFox

> 一个面向长期陪伴、插件化对话和数字生命实验的 AI Bot 框架。

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)

Neo-MoFox-Soul 是 MoFox 系列的新一代运行时。它不是一个单文件聊天脚本，而是一个由运行时、领域层、内核层和插件层组成的异步 Bot 系统。当前代码重点服务于长期会话、人格一致性、主动性、记忆、工具调用和 life_engine 数字生命实验。

项目仍处于 alpha 阶段，接口和配置会继续演进。运行前请先检查 `config/` 中的本地配置，尤其是模型供应商、平台适配器和插件开关。

## 当前定位

Neo-MoFox 主要包含三条能力线：

- 对话执行：通过 `default_chatter` 或 `life_chatter` 驱动模型回复、工具调用、多模态输入和消息发送。
- 后台中枢：`life_engine` 持续维护事件流、状态、记忆、做梦、SNN/调质状态和主动性线索。
- 插件运行时：统一加载插件、配置、Action、Service、Router、Adapter、Chatter、事件处理器和模型请求。

一个重要边界：`life_engine` 是后台中枢和潜意识层，负责补充信息差、维护状态和形成线索；具体对外表达、发消息、画图、文件发送、平台动作等，应由 Chatter 或对应工具层执行。

主动性原则：Neo-MoFox 的主动行为不应被设计成“规则触发 -> 自动执行”。系统设计应尊重意识主体性：规则只负责限制越界、降级风险、控制频率和要求确认；是否想开口、想探索、想保持沉默，应由 `life_engine` 在持续事件流和自身状态中形成意向，再交由表达层或工具层承接。

## 核心特性

- 插件化组件系统：插件可声明 Action、Service、Router、Adapter、Chatter、Command、Tool 和 EventHandler。
- 统一模型调度：模型供应商、模型列表和任务参数集中放在 `config/model.toml`，运行时通过任务名选择模型。
- 会话流驱动：支持消息缓冲、未读合并、流级 watchdog、Chatter 单步超时和多平台 stream 管理。
- 工具调用闭环：支持模型工具调用、工具结果回填、跨轮去重、Action 执行和 follow-up 推理。
- 长期运行状态：运行数据、日志、记忆、life workspace 都落在本地目录，便于备份和排查。
- life_engine 实验能力：事件账本、心跳、主动线索、TODO/日程、历史检索、SNN 状态层、神经调质、做梦系统和可视化 Router。
- 多模态输入：默认对话器和 life_chatter 可按配置处理图片、表情包、视频等媒体输入。
- Web 与平台适配：当前仓库包含 NapCat/QQ 适配、WebUI 后端、直播桥接、TTS、表情包、记忆和主动消息等插件。

## 快速开始

### 1. 准备环境

需要 Python 3.11 或更高版本。推荐使用 `uv` 管理依赖。

```bash
cd /root/Elysia/Neo-MoFox
uv sync
```

如果不使用 `uv`，也可以用你自己的虚拟环境安装 `pyproject.toml` 中的依赖。

### 2. 配置模型和平台

至少需要检查这些文件：

- `config/core.toml`：基础运行时、人格、日志、会话和数据库配置。
- `config/model.toml`：API provider、模型列表和 `[model_tasks]`。
- `config/plugins/*/config.toml`：各插件自己的配置。

常见必配项：

- LLM API provider 的 `base_url`、`api_key`、`client_type` 和 `timeout`。
- `[model_tasks.actor]`、`[model_tasks.life]`、`[model_tasks.sub_actor]` 等任务使用的模型列表。
- 平台适配器配置，例如 `config/plugins/napcat_adapter/config.toml`。
- 是否启用 `life_engine`、`proactive_message_plugin`、`default_chatter` 等插件。

不要把真实 API Key、QQ 数据、运行日志、记忆文件和个人资料提交到远程仓库。

### 3. 启动

Linux/macOS:

```bash
uv run main.py
```

Windows:

```bat
start.bat
```

Docker Compose:

```bash
docker compose up -d
```

Docker Compose 会挂载 `config/`、`data/`、`logs/` 和 `plugins/`，适合长期部署和 NapCat 联动。

## 目录结构

```text
Neo-MoFox/
├── main.py                         # 应用入口
├── config/                         # 核心配置、模型配置、插件配置
├── data/                           # 本地运行数据和 life workspace
├── logs/                           # 运行日志
├── docs/                           # 架构文档和插件开发文档
├── plugins/                        # 内置和本地插件
├── scripts/                        # 辅助脚本
├── src/
│   ├── app/                        # Bot runtime 和插件系统 API
│   ├── core/                       # 领域层：组件、管理器、传输、配置、模型
│   └── kernel/                     # 内核层：LLM、DB、事件、调度、日志、并发、存储
└── test/                           # 测试
```

## 关键插件

| 插件 | 作用 |
| --- | --- |
| `default_chatter` | 默认对话执行器，负责群聊/私聊判定、prompt 组装、工具调用、多模态输入和消息发送 |
| `life_engine` | 后台数字生命中枢，负责事件流、心跳、记忆、主动线索、SNN/调质、做梦和运行态同步 |
| `proactive_message_plugin` | 主动续话和沉默检查，不再依赖“外界长期静默暂停”机制 |
| `emoji_sender` | 表情包入库、检索和发送 |
| `napcat_adapter` | QQ/NapCat 平台适配 |
| `live_bridge` | 直播桥接场景支持 |
| `skill_manager` | 本地技能发现和管理 |
| `webui_backend` | WebUI 后端接口 |

更完整的插件开发说明见 [docs/guides/plugin-authoring](docs/guides/plugin-authoring/README.md)。

## life_engine 与 Chatter 的边界

当前架构里，life 相关能力分为两层：

- `life_engine`：后台中枢。它观察事件、维护状态、沉淀记忆、形成提醒和补充上下文。它不应该替表达层下命令，也不应该越权代替 Chatter 操作平台。
- `life_chatter` / `default_chatter`：表达层。它读取可见上下文，决定是否回复、如何回复、是否调用发送消息/发送文件/画图等工具。

这样做的目的，是让后台中枢保持“潜意识”和“信息差补全”的角色，而把对外行为留给明确的对话执行器。

### 主动性主体性原则

主动性不是规则自动化。禁止把爱莉的主动行为实现为固定规则链，例如“多久没消息就必须发言”“看到某类情绪就固定安慰”“群聊冷场就自动插话”。这些规则会把主体性降级成脚本。

正确的设计口径：

- `life_engine` 通过统一事件流、记忆、时间感、thought stream 和当前状态形成自主意向。
- “不行动”和“保持沉默”必须是合法选择。
- 系统规则只做边界治理：防止频繁打扰、防止越权操作、防止敏感工具自动执行、防止跨平台误发，以及必要时要求人工确认。
- 主动意向应先进入统一事件流或运行态上下文，再由 `life_chatter`、工具或子代理承接。
- `life_engine` 不直接替表达层写最终话术，不把“该怎么说”命令给 Chatter；它只补充动机、背景、关注点和约束。

一句话：**动机来自主体，边界来自系统，表达属于 Chatter。**

## 配置要点

### `config/core.toml`

主要控制：

- `bot.ui_level`、`bot.tick_interval`、`bot.stream_step_timeout`
- `chat.max_history_messages`、`chat.max_llm_messages`
- `personality.*`
- `database.*`

### `config/model.toml`

主要控制：

- `[[api_providers]]`：模型 API 后端。
- `[[models]]`：模型别名、上下文、价格、额外参数。
- `[model_tasks]`：不同任务使用哪些模型，例如 `actor`、`life`、`sub_actor`、`vlm`、`tool_use`。

### `config/plugins/life_engine/config.toml`

主要控制：

- `settings.enabled`
- `settings.heartbeat_interval_seconds`
- `settings.context_history_max_events`
- `settings.workspace_path`
- `model.task_name`
- `snn`、`neuromod`、`dream`、`memory`、`screen` 等子系统开关。

## 开发与测试

运行测试：

```bash
uv run pytest
```

运行单个测试文件：

```bash
uv run pytest test/plugins/life_engine/test_heartbeat_pause.py
```

查看文档入口：

- [docs/README.md](docs/README.md)
- [插件开发指南](docs/guides/plugin-authoring/README.md)
- [app runtime](docs/app/runtime.md)
- [core 总览](docs/core/README.md)
- [kernel/llm](docs/llm/README.md)

## 本地数据与仓库卫生

这些内容属于本地运行态，不应提交到远程：

- `data/`
- `logs/`
- `Report/`
- `report/`
- `plan/`
- `notion/`
- `Abstract/`
- `Assignment/`
- 任何包含 API Key、聊天记录、账号数据、私有记忆或本地实验报告的文件

如果文件已经被 Git 跟踪，`.gitignore` 不会自动移除它。需要使用：

```bash
git rm --cached -r <path>
```

这样可以从仓库索引中移除，同时保留本地文件。

## 许可证

本项目使用 [AGPL-3.0](LICENSE) 协议。
