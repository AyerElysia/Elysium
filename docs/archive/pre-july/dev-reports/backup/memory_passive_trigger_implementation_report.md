# Memory Passive Trigger Plugin 实施报告（历史归档）

> 状态：历史实现记录。原记忆后端已退役，本文不代表当前可用的服务、插件依赖或 API，仅保留实现思路与验证清单。

## 1. 项目概况

| 项目 | 内容 |
|------|------|
| 插件名称 | `memory_passive_trigger` |
| 版本 | 1.0.0 |
| 类型 | 被动记忆浮现触发器 |
| 依赖 | 可选的记忆检索能力（待重新设计） |
| 状态 | 历史实现，当前不可直接加载 |
| 日期 | 2026-03-19 |

---

## 2. 实施背景

### 2.1 问题陈述

历史记忆插件的闪回机制存在局限；该实现现已退役，以下内容仅用于记录当时的设计背景：

| 维度 | 当前实现 | 人类记忆特征 |
|------|---------|-------------|
| 触发方式 | 随机概率触发（5%） | 情境触发（看到关键词/语义自动唤起） |
| 触发时机 | 构建 prompt 时 | 接收外界刺激时 |
| 关联性 | 与用户输入无关 | 与当前话语强相关 |
| 体验 | "莫名想起" | "触景生情" |

### 2.2 解决方案

创建独立插件，实现**被动记忆浮现**机制：
- 监听用户消息 → 语义检索 → 自动注入相关记忆
- 与主动记忆读取能力并存，互不干扰

---

## 3. 实施内容

### 3.1 插件结构

```
plugins/memory_passive_trigger/
├── __init__.py              # 插件元数据
├── plugin.py                # 插件入口类
├── config.py                # Pydantic 配置定义
├── manifest.json            # 插件清单文件
└── handler.py               # 核心事件处理器

config/plugins/memory_passive_trigger/
└── config.toml              # 配置文件
```

### 3.2 核心组件

#### 3.2.1 插件入口 (`plugin.py`)

```python
@register_plugin
class MemoryPassiveTriggerPlugin(BasePlugin):
    plugin_name = "memory_passive_trigger"
    plugin_description = "被动记忆浮现 - 看到关键词自动唤起相关记忆"

    configs = [MemoryPassiveTriggerConfig]
    # 历史伪代码：依赖抽象的记忆检索能力，不绑定具体插件或服务名
```

#### 3.2.2 事件处理器 (`handler.py`)

```python
class MemoryPassiveTriggerHandler(BaseEventHandler):
    handler_name = "memory_passive_trigger_handler"
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(self, event_name, params):
        # 1. 获取用户消息
        # 2. 调用抽象记忆检索能力
        # 3. 过滤超过阈值的记忆
        # 4. 应用冷却检查
        # 5. 注入到 context
```

### 3.3 配置项

```toml
[plugin]
enabled = true

[trigger]
enabled = true
similarity_threshold = 0.75    # 语义匹配阈值
max_flash_count = 2            # 最多浮现条数
cooldown_seconds = 300         # 冷却时间（秒）
priority_folders = ["facts", "preferences", "relations"]

[retrieval]
include_archived = true
include_knowledge = false
candidate_limit = 20

[debug]
verbose = false
```

### 3.4 注入格式

```markdown
## 记忆浮现
听到这句话时，你脑海中无征兆地浮现出一些相关的记忆：

- 「小爱莉对芒果过敏，吃芒果蛋糕会起疹子」（来自 preferences）
- 「去年生日买过芒果蛋糕」（来自 events）

注：这是你记忆系统自动关联出来的，不是刻意检索的结果。
你可以自然地提及，也可以选择忽视。
```

---

## 4. 工作流程

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
    │ 1. 提取用户消息文本：query_text = "我想买个蛋糕"         │
    └────────────────────────────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 2. 调用抽象记忆检索能力                                  │
    │    result = memory_retriever.search(...)               │
    └────────────────────────────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 3. 过滤超过阈值的记忆                                    │
    │    score >= 0.75 → 保留                                 │
    └────────────────────────────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 4. 冷却检查：同一条记忆 300 秒内不重复触发                  │
    └────────────────────────────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 5. 按优先级排序：priority_folders 优先                    │
    └────────────────────────────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 6. 截取 Top-N：最多 2 条                                   │
    └────────────────────────────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 7. 注入到 params["context"]["extra"]                    │
    └────────────────────────────────────────────────────────┘
                         │
                         ▼
    ┌────────────────────────────────────────────────────────┐
    │ 8. LLM 在回复前看到注入的记忆                              │
    │    → "对了，我记得你说过对芒果过敏..."                   │
    └────────────────────────────────────────────────────────┘
```

---

## 5. 关键技术点

### 5.1 服务调用（历史伪代码）

```python
# 重新实现时由当前记忆层注入，不对应现有运行时 API
result = await memory_retriever.search(
    query_text=message,
    top_k=candidate_limit,
    include_archived=True,
    include_knowledge=False,
)
```

### 5.2 能力声明

```python
required_capabilities = ["memory_retrieval"]
```

具体能力由当前插件系统显式注入；本历史实现不可直接加载。

### 5.3 冷却机制

```python
def _prune_cooldown(self, now: float, cooldown_seconds: int) -> None:
    """清理过期的冷却记录。"""
    if cooldown_seconds <= 0:
        self._cooldown_map.clear()
        return

    expired = [
        memory_id
        for memory_id, expire_time in self._cooldown_map.items()
        if now >= expire_time
    ]
    for memory_id in expired:
        del self._cooldown_map[memory_id]
```

### 5.4 优先级排序

```python
priority_folders = set(trigger.priority_folders)
qualified.sort(key=lambda r: (
    0 if r.get("folder_id") in priority_folders else 1,
    -r.get("score", 0),
))
```

---

## 6. 测试计划

### 6.1 单元测试（待补充）

| 测试用例 | 预期结果 |
|---------|---------|
| 消息为空 | 不触发检索 |
| 检索结果为空 | 不注入记忆 |
| 最高分低于阈值 | 不触发 |
| 最高分高于阈值 | 触发并注入 |
| 冷却期内重复 | 跳过 |
| priority_folder 记忆 | 排序在前 |

### 6.2 集成测试步骤

1. 启动 Bot（加载两个插件）
2. 发送包含已知记忆关键词的消息
3. 检查日志：
   ```bash
   grep "memory_passive_trigger_handler" logs/mofox_*.log
   ```
4. 检查 LLM 回复是否包含相关记忆内容

---

## 7. 已知限制

| 限制 | 说明 | 后续改进方向 |
|------|------|-------------|
| 检索输入 | 仅用最新一条消息 | 第二阶段可加入上下文窗口 |
| 无指代消解 | "那件事"无法关联上文 | 可加 LLM 预处理提取关键词 |
| 单轮触发 | 每次独立判断 | 可加短期记忆关联 |

---

## 8. 文件清单

| 文件 | 行数 | 功能 |
|------|------|------|
| `plugins/memory_passive_trigger/__init__.py` | 6 | 插件元数据 |
| `plugins/memory_passive_trigger/plugin.py` | 42 | 插件入口 |
| `plugins/memory_passive_trigger/config.py` | 88 | 配置定义 |
| `plugins/memory_passive_trigger/manifest.json` | 19 | 插件清单 |
| `plugins/memory_passive_trigger/handler.py` | 224 | 核心逻辑 |
| `config/plugins/memory_passive_trigger/config.toml` | 32 | 配置文件 |
| **合计** | **411** | |

---

## 9. 配置说明

### 9.1 阈值调整

| 场景 | 建议值 |
|------|--------|
| 误触发太多 | 调高到 0.80-0.85 |
| 很少触发 | 调低到 0.65-0.70 |

### 9.2 冷却时间

| 场景 | 建议值 |
|------|--------|
| 希望频繁触发 | 60-120 秒 |
| 避免打扰 | 300-600 秒 |
| 不限制 | 0 秒 |

### 9.3 优先级文件夹

可根据需求调整：
```toml
priority_folders = ["facts", "preferences", "relations", "events"]
```

---

## 10. 日志示例

### 10.1 成功触发

```
[INFO] memory_passive_trigger_handler | 被动浮现已注入：count=2, query=我想买个蛋糕...
```

### 10.2 未达阈值

```
[DEBUG] memory_passive_trigger_handler | 被动浮现：最高分数 0.623 未达阈值 0.75
```

### 10.3 冷却期

```
[DEBUG] memory_passive_trigger_handler | 被动浮现：所有匹配记忆均处于冷却期
```

---

## 11. 下一步计划

### Phase 2（可选扩展）

- [ ] 加入上下文窗口（最近 N 轮对话）
- [ ] 短消息自动扩展上文（<20 字时）
- [ ] LLM 关键词提取（指代消解）
- [ ] 支持负向关键词过滤

---

## 12. 验收标准

- [x] 插件代码完成
- [x] 配置文件完成
- [x] 依赖声明正确
- [ ] Bot 重启后正常加载
- [ ] 能够检测到用户消息
- [ ] 能够检索并注入记忆
- [ ] 冷却机制正常工作
- [ ] 日志输出正常

---

## 13. 总结

这份文档记录了一个已退役的被动记忆浮现实现。它保留了阈值、冷却、优先级和上下文注入等设计要点，但不能作为当前插件状态、依赖关系或可运行入口的说明。

1. **独立触发器**：通过抽象记忆检索能力解耦
2. **配置灵活**：阈值、冷却、优先级均可调
3. **轻量实现**：核心逻辑易于维护
4. **可扩展**：重新实现时需要按当前插件规范补齐

---

**实施人**: Assistant
**完成日期**: 2026-03-19
**版本**: v1.0.0
