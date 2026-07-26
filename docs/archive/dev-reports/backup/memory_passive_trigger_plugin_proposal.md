# Memory Passive Trigger Plugin 方案书（历史归档）

> 状态：历史方案，已废弃。原记忆后端已退役，本文不描述当前可用的插件、服务或 API，仅保留设计思路供重新实现时参考。

## 1. 背景与动机

### 1.1 问题陈述

历史记忆插件的随机闪回机制存在以下局限；该实现现已退役，以下内容仅保留为设计背景：

| 维度 | 当前实现 | 人类记忆特征 |
|------|---------|-------------|
| 触发方式 | 随机概率触发（5%） | 情境触发（看到关键词/语义自动唤起） |
| 触发时机 | 构建 prompt 时 | 接收外界刺激时 |
| 关联性 | 与用户输入无关 | 与当前话语强相关 |
| 体验 | "莫名想起" | "触景生情" |

### 1.2 认知科学依据

人类记忆检索分为两种模式：

1. **主动检索（Active Retrieval）**：有意识地回忆特定信息
   - 例："等等，我之前说过这个吗？让我想想..."
   - 对应：LLM 调用记忆读取 Agent（具体接口需重新设计）

2. **被动浮现（Passive Trigger）**：外界刺激自动唤起相关记忆，不受意识控制
   - 例：听到"蛋糕"突然想起某人说过对芒果过敏
   - **当前系统缺失**

---

## 2. 设计目标

### 2.1 核心目标

- [ ] 实现基于语义匹配的被动记忆浮现机制
- [ ] 与现有主动检索系统完全解耦
- [ ] 独立插件形式，可选加载/卸载
- [ ] 可配置的触发阈值和行为参数

### 2.2 非目标

- [ ] 不修改既有记忆存储实现
- [ ] 不与其他主动检索路径耦合
- [ ] 不在本插件中新增持久化存储层

---

## 3. 技术方案（简化版）

### 3.1 插件架构

```
plugins/
└── memory_passive_trigger/
    ├── __init__.py              # 插件元数据（版本、名称）
    ├── plugin.py                # 插件入口类
    ├── config.py                # Pydantic 配置定义
    ├── manifest.json            # 插件清单文件
    └── handler.py               # 核心事件处理器
```

### 3.2 工作流程

```
┌─────────────────────────────────────────────────────────────────┐
│                      用户发送消息                                │
│                      "我想买个蛋糕"                               │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
         ┌───────────────────────────────────┐
         │ on_message_received 事件触发       │
         │ MemoryPassiveTriggerHandler 订阅   │
         └───────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 1. 提取用户最新消息文本                                  │
    │    query_text = "我想买个蛋糕"                           │
    └────────────────────────────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 2. 调用记忆检索服务（具体实现待重新设计）               │
    │    result = memory_retriever.search(...)               │
    └────────────────────────────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 3. 获取检索结果，检查匹配度                              │
    │    - 最高相似度分数 = 0.82                              │
    │    - 阈值 = 0.75 → 超过阈值，触发                       │
    └────────────────────────────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 4. 注入到 conversation context                          │
    │    添加到 params["context"]["passive_memories"]        │
    └────────────────────────────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 5. LLM 在回复前被动接收到记忆                             │
    │    "对了，我记得你说过对芒果过敏..."                      │
    └────────────────────────────────────────────────────────┘
```

### 3.3 配置设计

```toml
# config/plugins/memory_passive_trigger/config.toml

[plugin]
enabled = true

[trigger]
# 是否启用被动浮现
enabled = true

# 语义匹配阈值（0-1）
# 越高越严格，建议范围 0.70-0.85
similarity_threshold = 0.75

# 最多浮现几条记忆
max_flash_count = 2

# 冷却时间（秒）- 同一条记忆多久内不能再次浮现
cooldown_seconds = 300

# 优先触发哪些 folder 的记忆
priority_folders = ["facts", "preferences", "relations"]

[retrieval]
# 是否检索归档层
include_archived = true

# 是否检索知识库
include_knowledge = false

# 检索时最多加载多少候选
candidate_limit = 20

[debug]
# 是否打印详细日志
verbose = false
```

### 3.4 注入格式

被动浮现的记忆将以以下格式注入到 prompt context：

```markdown
## 记忆浮现
听到这句话时，你脑海中无征兆地浮现出一些相关的记忆：

- 「小爱莉对芒果过敏，吃芒果蛋糕会起疹子」（来自偏好记忆）

注：这是你记忆系统自动关联出来的，不是刻意检索的结果。
你可以自然地提及，也可以选择忽视。
```

### 3.5 检索策略说明

**第一阶段使用简化方案**：仅基于用户**最新发送的一条消息**进行检索。

原因：
- 实现简单，快速验证被动浮现机制是否有效
- 大部分触发场景下，单条消息已包含足够语义

后续可扩展：
- 第二阶段可加入对话上下文窗口（最近 N 轮）
- 或者检测短消息时自动扩展上文

---

## 4. 与记忆检索层的集成（历史设计）

本文原方案曾依赖一个现已退役的记忆后端。下列内容只描述重新实现时需要定义的抽象能力，不对应当前运行时代码。

### 4.1 抽象检索契约

```python
# 伪代码：重新实现时由当前记忆层提供，不是现有运行时 API
result = await memory_retriever.search(
    query_text=user_message,
    top_k=candidate_limit,
    include_archived=config.trigger.include_archived,
    include_knowledge=config.trigger.include_knowledge,
)
```

### 4.2 能力依赖

被动触发器只应依赖一个可选的记忆检索能力，由插件系统在启动时显式注入；不应依赖已经删除的具体插件名、服务注册名或目录路径。

---

## 5. 实现细节

### 5.1 核心算法

```python
async def execute(self, event_name: str, params: dict[str, Any]):
    # 1. 获取用户消息
    message = params.get("message", "")
    if not message.strip():
        return EventDecision.SUCCESS, params

    # 2. 检索记忆
    result = await self.memory_service.retrieve_memories(
        query_text=message,
        top_k=self.config.retrieval.candidate_limit,
    )

    # 3. 过滤超过阈值的记忆
    qualified = [
        r for r in result.get("results", [])
        if r.get("score", 0) >= self.config.trigger.similarity_threshold
    ]

    # 4. 应用冷却检查
    now = time.time()
    self._prune_cooldown(now)
    qualified = [
        r for r in qualified
        if r["id"] not in self._cooldown_map
    ]

    # 5. 按优先级排序
    priority = self.config.trigger.priority_folders
    qualified.sort(key=lambda r: (
        r["folder_id"] not in priority,  # 优先 folder
        -r["score"],                      # 其次分数
    ))

    # 6. 截取 Top-N
    selected = qualified[:self.config.trigger.max_flash_count]

    # 7. 记录冷却时间
    for r in selected:
        self._cooldown_map[r["id"]] = now + self.config.trigger.cooldown_seconds

    # 8. 注入到 context
    if selected:
        self._inject_to_context(params, selected)

    return EventDecision.SUCCESS, params
```

### 5.2 注入方式

```python
def _inject_to_context(self, params: dict, memories: list):
    # 获取或创建 memory context
    ctx = params.setdefault("context", {})
    existing = ctx.get("passive_memories", [])

    # 格式化记忆内容
    formatted = [
        f"- 「{m['content_snippet']}」（来自 {m['folder_id']}）"
        for m in memories
    ]

    # 追加
    ctx["passive_memories"] = existing + formatted
```

---

## 6. 测试计划

### 6.1 单元测试

| 测试用例 | 预期结果 |
|---------|---------|
| 消息为空时 | 不触发检索 |
| 检索结果为空 | 不注入记忆 |
| 匹配度低于阈值 | 不触发 |
| 匹配度高于阈值 | 触发并注入 |
| 冷却期内重复记忆 | 跳过不触发 |
| 优先 folder 记忆 | 排序在前 |

### 6.2 集成测试

1. 启动 Bot，加载两个插件
2. 发送包含已知记忆关键词的消息
3. 检查 LLM 回复中是否包含相关记忆内容
4. 检查日志中是否有被动触发记录

---

## 7. 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| 误触发（匹配不相关记忆） | 中 | 调高阈值，支持负向关键词 |
| 过度打扰（频繁触发） | 中 | 冷却时间 + 单次最多 N 条 |
| 性能开销（每次消息都检索） | 低 | 由当前记忆层提供限时、可观测的检索能力 |
| 记忆检索能力未加载 | 低 | 声明可选能力，启动时检查并降级 |

---

## 8. 时间估算

| 阶段 | 预计工时 |
|------|---------|
| 插件脚手架创建 | 30 分钟 |
| 配置类定义 | 30 分钟 |
| 核心事件处理器 | 2 小时 |
| 接入记忆检索抽象 | 1 小时 |
| 单元测试 | 1 小时 |
| 调试与优化 | 2 小时 |
| **合计** | **约 7 小时** |

---

## 9. 审批检查清单

- [ ] 方案目标是否清晰
- [ ] 技术方案是否可行
- [ ] 配置参数是否合理
- [ ] 风险评估是否充分
- [ ] 时间估算是否可接受

---

## 10. 附录

### 10.1 相关文件

- 记忆检索抽象接口：待重新实现时定义
- 消息事件系统 API：待按当前插件规范核对
- 插件开发规范：`memory/neo_mofox_plugin_standards.md`

### 10.2 参考资料

- 主动检索 vs 被动浮现：认知心理学研究
- Neo-MoFox 插件系统 API 文档

---

**提案人**: Assistant
**日期**: 2026-03-19
**版本**: v1.0
