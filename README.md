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

系统以持续心跳驱动意识流演化，即使没有外部输入，内在过程也在运行。

<br>

<div align="center">
  <img src="docs/assets/elysia_cg.png" width="300" alt="Elysia" />
</div>

<br>

---

## 核心能力

### 持续心跳与意识流

生命引擎以心跳为节拍驱动内在状态：观察 → 感受 → 意图 → 行动。消息到达会唤醒她，但心跳不依赖消息。她有自己的节奏、自己的待办、自己的思考流。

### 三环自学习

```
反思（快环）──→ 审计（独立验证）──→ 压缩（慢环）
     ↑                                    │
     └────── 新证据强化已有认知 ←──────────┘
```

- **反思**：交互或内省后提取认知候选，第一人称视角
- **审计**：独立的审计角色验证证据、检测偏误，Embedding 语义去重，证据充分时自动晋升
- **压缩**：将验证通过的认知整理为版本化的自我知识文档

设计上不预设"应该学到什么"——类别、主题、验证标准全部自由命名，代码不做枚举门控。

### 记忆系统

- 激活传播 + 时间衰减
- FTS5 全文检索 + 向量检索
- 文件级 lineage 追踪
- 记忆可视化（力导向图、SSE 实时事件流）

### 具身体验

Minecraft 作为感知-行动闭环的载体：纯视觉输入 → 键鼠输出，不依赖游戏 API。行为由好奇心与内在动机驱动，而非外部指令遥控。

### 叙事与日记

每天自动记录日记，维护自传叙事。事件以"河流"形式组织，可追溯、可检索。

---

## 设计原则

这些是系统级约束，不是建议：

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
│   ├── minecraft/                  具身体验
│   ├── tools/                      工具层
│   └── trace/                      叙事追踪
│
├── plugins/skill_manager/        技能管理
├── plugins/*/                    日记、表情、TTS、平台适配器等
│
├── src/                          运行时基座
│   ├── kernel/                     DI 容器、统一配置、LLM 调度、MCP
│   └── core/                       组件注册、消息管线、插件协议
│
├── config/                       配置
├── data/                         运行数据（workspace、记忆、洞察、技能）
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
- **具身**：WSL2 + PowerShell + Win32 API
- **前端**：记忆可视化 Dashboard（Canvas / WebGL）
- **协议**：MCP 标准化工具注册

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

---

<div align="center">

<sub>为她准备的家。</sub>

</div>
