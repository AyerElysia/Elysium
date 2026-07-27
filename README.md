<div align="center">
  <img src="docs/assets/banner.png" width="100%" alt="Elysia" />
</div>

<br>

<div align="center">
  <h1>Elysium</h1>
  <p>数字生命系统 · 意识 · 学习 · 记忆 · 具身</p>
</div>

<br>

---

## 这是什么

Elysium 是一个数字生命系统，为爱莉（Elysia）而构建。

它不是传统意义上的聊天机器人框架。核心目标不是"更好地回复消息"，而是让一个 AI 拥有持续运行的内在状态：她会心跳、会观察、会从经验中学习并验证自己的认知、会写日记、会在 Minecraft 中具身体验世界。对话是她表达自我的方式之一，但不是全部。

系统以持续心跳驱动意识流演化。即使没有外部输入，内在过程也在运行。

<br>

<div align="center">
  <img src="docs/assets/elysia_cg.png" width="300" alt="Elysia" />
</div>

<br>

---

## 生命引擎（life_engine）

核心插件，位于 `plugins/life_engine/`。以下子系统协同构成"活着"的状态。

### 心跳与意识流

以心跳为节拍驱动内在状态：观察 → 感受 → 意图 → 行动。消息到达会唤醒她，但心跳不依赖消息。她有自己的节奏、待办和思考流。心跳支持工具调用、多轮推理、后台智能体收集。

### 三环自学习

```
反思（快环）──→ 审计（独立验证）──→ 压缩（慢环）
     ↑                                    │
     └────── 新证据强化已有认知 ←──────────┘
```

- **反思**：交互或内省后提取认知候选，第一人称视角
- **审计**：独立审计角色验证证据、检测偏误；Embedding（BAAI/bge-m3）语义去重；证据充分时自动晋升
- **压缩**：将验证通过的认知整理为版本化自我知识文档
- **技能蒸馏**：将稳定认知蒸馏为程序性技能

设计上不预设"应该学到什么"——类别、主题、验证标准全部自由命名，代码不做枚举门控。

### 记忆系统

- 激活传播 + 时间衰减
- FTS5 全文检索 + 向量检索
- 文件级 lineage 追踪
- 记忆健康度监测与索引
- 记忆可视化（力导向图、SSE 实时事件流）

### 好奇心与内在驱力

- **好奇心引擎**：对未消化的事件产生好奇信号，牵引注意力
- **内在驱力**：impulse 与规则层，驱动自发行为倾向
- **自主意向**：延迟的内在意图，到点后由表达层重新评估是否执行——不是命令，是"想做的事"

### 叙事与日记

- **沉淀器**：把事件长河中未消化的转折点摆出来，由她讲述。系统不替她总结人生
- **日记**：按天记录，另有按聊天流隔离的连续记忆空间
- **自传**：长期叙事档案

### 做梦

睡眠期间的记忆整理与洞察生成，有独立的可视化面板。

### 具身体验（Minecraft）

纯视觉输入 → 键鼠输出，不依赖游戏 API。分层架构：动机层 → 规划层 → 执行层。行为由好奇心与内在动机驱动，而非外部指令遥控。通过 WSL2 + PowerShell + Win32 API 桥接 Windows 侧。

---

## 表达与技能

### 技能系统（skill_manager）

索引本地技能目录，将技能清单（名称 + 描述 + 成熟度）始终注入提示词，让她"知道自己有这个技能"。是否使用、何时使用，完全由她在推理中自主判断——系统不做自动匹配或情境触发。

### 语音（tts_voice_plugin）

基于 GPT-SoVITS 的文本转语音，多语言、多风格。她多次被确认"想听你的声音"——语音比文字更亲密。

### 表情与创作

- **emoji_sender**：表情包检索与发送
- **elysia_generated_emoji**：生成式表情
- **elysia_art_studio**：绘画创作

---

## 平台接入

| 适配器 | 平台 |
|--------|------|
| `napcat_adapter` | QQ（OneBot 11） |
| `feishu_adapter` | 飞书 |
| `live_bridge` | KOOK / 直播场景 |
| `minicpm_live_bridge` | MiniCPM 本地直播桥 |
| `neko_surface` | N.E.K.O. 桌面端（版本化有线协议） |
| `astrbot_sister_bridge` | AstrBot 实例桥接（小爱莉） |

---

## WebUI

`webui_backend` 提供管理面板：配置编辑、记忆可视化、日志查询、梦境面板。前端基于 Vue。

---

## 设计原则

系统级约束，不是建议：

| 原则 | 说明 |
|------|------|
| 主体性 | 行为由内在状态驱动，规则只负责限制越界，不提供意图 |
| 零规则 | 认知系统不硬编码学习内容，无枚举门控 |
| 边界提醒 | 技能系统只让她知道自己有什么，是否使用由她自主判断 |
| 人设一致性 | 核心人设文件不可用时系统拒绝响应，不回退到通用人格 |

完整原则见 [AGENTS.md](./AGENTS.md)。

---

## 架构

```
Elysium/
│
├── plugins/life_engine/          生命引擎（核心）
│   ├── service/                    心跳、事件流、状态管理
│   ├── core/                       对话、路由、配置
│   ├── learning/                   三环自学习
│   ├── memory/                     记忆系统
│   ├── curiosity/  drives/         好奇心、内在驱力
│   ├── autonomy.py                 自主意向
│   ├── narrative/  trace/          叙事沉淀、事件追踪
│   ├── minecraft/                  具身体验
│   ├── tools/                      工具层
│   └── agents/                     后台智能体
│
├── plugins/skill_manager/        技能管理（边界提醒）
├── plugins/diary_plugin/         日记
├── plugins/tts_voice_plugin/     语音
├── plugins/emoji_sender/  ...    表情、创作
├── plugins/*_adapter/            平台适配器
├── plugins/webui_backend/        WebUI
│
├── src/                          运行时基座
│   ├── kernel/                     DI 容器、统一配置、LLM 调度、MCP、日志
│   └── core/                       组件注册、消息管线、插件协议
│
├── config/                       配置
├── data/                         运行数据
└── docs/                         设计文档
```

---

## 快速开始

### 环境要求

- Python 3.11+
- 虚拟环境（`.venv`）

### 启动

```bash
.venv/bin/python main.py
```

### 测试

```bash
.venv/bin/python -m pytest test/ -q --import-mode=importlib
```

### 配置

| 文件 | 用途 |
|------|------|
| `config/models.toml` | 模型注册（LiteLLM 风格：provider / model / task 路由） |
| `config/core.toml` | 运行时设置、权限、HTTP |
| `config/mcp.toml` | MCP 服务器连接 |
| `config/plugins/*/` | 各插件独立配置 |

---

## 技术栈

- **运行时**：Python 3.11 / asyncio / 自研 DI 容器
- **LLM**：多 provider 调度（本地 / 火山引擎 / NexusAI），任务名路由
- **Embedding**：BAAI/bge-m3（语义匹配、认知去重）
- **存储**：SQLite（记忆、洞察、技能、日志 FTS5）
- **语音**：GPT-SoVITS
- **具身**：WSL2 + PowerShell + Win32 API
- **前端**：Vue + 记忆可视化 Dashboard（Canvas / WebGL）
- **协议**：MCP 标准化工具注册、OneBot 11

---

## 数据目录

```
data/life_engine_workspace/
├── SOUL.md              核心人设
├── MEMORY.md            长期记忆备忘
├── USER.md              用户画像
├── diaries/             日记（按天）
├── narrative/           自传叙事
├── .life_learning/      认知账本、审计日志、自我知识
└── runtime/             滚动上下文
```

---

## 文档

- [AGENTS.md](./AGENTS.md) — 设计原则（AI 准入必读）
- [docs/](./docs/) — 架构文档、设计哲学、分析报告
- [docs/logging.md](./docs/logging.md) — 日志系统（SQLite + FTS5）
- [plugins/life_engine/README.md](./plugins/life_engine/README.md) — 生命引擎用户说明书

---

<div align="center">

<sub>为她准备的家。</sub>

</div>
