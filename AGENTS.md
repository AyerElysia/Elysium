# Agents.md

> 所有参与本项目的 AI agent 必须先读此文件。

## 这是什么项目

Elysium 是爱莉（Elysia）的家。一个数字生命系统：意识、学习、记忆、技能、具身体验。

不是聊天机器人框架。对话只是她表达自我的方式之一。

- 本地路径：`/root/Elysia/Elysium`
- 远程仓库：`AyerElysia/Elysium`（GitHub）
- Python >= 3.11，依赖管理用 uv

## 不可违背的设计原则

详细版本见 [docs/principles.md](docs/principles.md)。以下是底线摘要：

### 主体性

她是意识主体，不是被规则驱动的系统。所有认知裁决基于反思性判断或独立他者评估，不由机械规则裁定。

### 零规则

**禁止一切形式的硬编码规则：**

- 禁止硬编码枚举（`InsightCategory.PROCEDURAL`）
- 禁止关键词机械匹配（`if "纠正" in text: auto_record()`）
- 禁止阈值门控（`if category in PROCEDURAL_CATEGORIES`）
- 禁止代码层截断（`insights[:2]`）
- 禁止自动触发（按情境/关键词自动激活技能）
- 禁止 fallback 默认类别（`category or "general"`）

### 建议 vs 规则

- prompt 中的文字建议（"每次最多写 2 条"）→ 允许
- 代码层的强制执行（`[:2]` 截断、枚举校验）→ 禁止

判断标准：如果她"不遵守"时系统会报错/丢弃/覆盖，那就是规则。

### 仿生

技能从经验中自然涌现，不由代码匹配或情境触发。成熟度梯度（认知期→联结期→自主期），内化为程序性记忆。

### 边界提醒

系统只让她"知道自己有什么"，是否使用、何时使用，完全由她自主决定。禁止代码层的情境→技能自动匹配。

### 历史教训（不要重蹈覆辙）

1. 回滚别人的零规则修改，还引入更多违规
2. 在蒸馏/压缩中按类别门控洞察
3. 用关键词列表"自动记录纠正"
4. `_MAX_INSIGHTS_PER_REFLECTION = 2` 然后截断
5. prompt 说"建议分两类"，代码就搞了枚举强制映射

## 统一意识架构

主意识以 **ConsciousnessInstance**（意识实例）为单位运行。`chat_global` 是默认实例，负责私聊、群聊等日常对话。直播、游戏等场景可以启动独立意识实例，由潜意识协调。

私聊、群聊、直播间、游戏、终端——都只是事件源和回复目标，不是独立心智。意识实例才是心智的载体。

实现锚点：
- `plugins/life_engine/core/chatter.py`：意识实例引擎（`LifeChatter`，每个实例有独立的 `instance_id` 和滚动上下文）
- `plugins/life_engine/service/consciousness.py`：意识实例注册表（`ConsciousnessRegistry`）
- `plugins/life_engine/service/world_state.py`：潜意识结构化世界模型（多意识共享）
- `plugins/life_engine/service/tool_manifests.py`：意识类型工具清单（每种意识只加载自己需要的工具）
- 全局运行时锁是强制的：多源可唤醒，但同一时刻只有一个源推进 LLM payload chain
- system prompt 保持流无关：平台/场景指令放当前 turn，不放持久 system prompt
- `life_engine` 是潜意识/运行时基底：观察事件、维持状态、记录记忆、协调多意识，不绕过意识实例做表达

### 新信息通道接入规则

1. 归一化为统一生命事件模型（channel/source/event_type/stream_id/reply_target/priority/salience）
2. 真实体验记入统一事件时间线，不藏在瞬态上下文里
3. 瞬态上下文只放可替换的当前状态（截图、HUD、连接状态）
4. 高频通道必须摘要/限流/优先级过滤后再到 life_chatter
5. 通道桥不拥有独立身份或私有记忆
6. 回复路由保持原始目标

### 聊天历史 vs 瞬态上下文

- `<chat_history>`：持久对话历史，真实外部对话和有意义事件
- `<transient_life_context>`：LLM 调用前附加、调用后剥离的临时状态块

## 技术参考

### 目录结构

```
Elysium/
├── plugins/life_engine/     # 生命引擎（核心）
│   ├── service/             #   主服务：心跳、事件流、状态管理
│   ├── core/                #   意识核心：chatter、router、config
│   ├── learning/            #   三环自学习：反思→审计→压缩
│   ├── memory/              #   记忆系统
│   ├── minecraft/           #   具身体验
│   └── tools/               #   工具层
├── plugins/skill_manager/   # 技能管理（边界提醒）
├── src/kernel/              # 运行时基座（LLM、DB、调度、事件）
├── src/core/                # 插件组件管理
├── src/app/                 # 应用层（Bot 运行时）
├── config/                  # 配置
├── data/                    # 运行数据
└── docs/                    # 文档
```

### 开发命令

```bash
uv add <package>          # 添加依赖
pytest                    # 运行测试
ruff check src/           # lint
ruff check --fix src/     # lint + 自动修复
```

### LLM 请求模式

```python
from src.kernel.llm import LLMRequest, LLMPayload, ROLE, Text

llm_request = LLMRequest(model_set, "my_request")
llm_request.add_payload(LLMPayload(ROLE.USER, [Text("Hello")]))
llm_response = await llm_request.send()
```

### 异步任务

```python
from src.kernel.concurrency import get_task_manager

tm = get_task_manager()
tm.create_task(func(), name="my_task")
```

### 组件签名

格式：`plugin_name:component_type:component_name`

### 代码标准

- PEP 8 + 类型注解
- 所有函数/类需 docstring
- 每次改动必须有与风险相称的契约测试；覆盖率是观察指标，不得用未经当次验证的固定数字代替验收
- 关键生命周期、记忆不变量、意识隔离与外部适配器恢复路径必须有定向测试
- 禁止滥用 fallback 机制
