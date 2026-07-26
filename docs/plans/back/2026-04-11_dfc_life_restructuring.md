# DFC-Life 协作架构重构方案

> 日期: 2026-04-11
> 配套分析文档: notion/DFC与Life系统协作架构深度分析.md

## 目标

将 DFC 从"独立聊天机器人"改造为"Life 的表达层"，让 Life 的丰富理解能实时流入 DFC 的对话决策。

## Phase 1: 修复 Reminder 陈旧问题 (Quick Win)

### 问题
DFC enhanced runner 只在初始化时读取一次 SystemReminderStore，之后 while True 循环中 reminder 永远是旧的。

### 方案
在 `runners.py` 的 `_enhanced_runner` 中，每次 WAIT_USER 阶段开始处理新消息前，刷新 reminder：

**文件**: `plugins/default_chatter/runners.py`
```python
# 在 WAIT_USER 阶段，处理消息之前刷新 reminder
def _refresh_reminder(request: LLMRequest, bucket: str = "actor"):
    """从 SystemReminderStore 刷新最新的潜意识状态到上下文。"""
    from src.core.prompt import get_system_reminder_store
    text = get_system_reminder_store().get(bucket)
    if text:
        request.context_manager.update_reminder(text)
```

**文件**: `src/kernel/llm/context.py`
新增 `update_reminder()` 方法：
```python
def update_reminder(self, text: str, wrap_with_system_tag: bool = True):
    """替换现有 reminder 内容（保持注入位置不变）。"""
    self._reminders.clear()
    self.reminder(text, wrap_with_system_tag=wrap_with_system_tag)
```

### 验证
- [ ] 在 life_engine 心跳后，下一条 DFC 消息应包含最新的潜意识状态
- [ ] 日志可观测到 reminder 刷新

---

## Phase 2: 丰富 Life Briefing

### 问题
当前潜意识同步只有 ~200 tokens 的独白/工具/摘要，缺少结构化指导。

### 方案
重构 `_sync_subconscious_state()` 输出为结构化 Briefing：

**文件**: `plugins/life_engine/service.py` — `_sync_subconscious_state()`

新格式：
```
【内心状态】开心、有些好奇 (SNN探索=0.72, 社交=0.65)
【最近在做】正在写关于星空的笔记 (3分钟前)
【活跃 TODO】"学吉他" (进行中), "写信给小明" (待定)
【最近日记】今天和Ayer聊了很多关于音乐的话题...
【对话引导】最近对音乐话题很感兴趣；语气保持活泼好奇；如果Ayer提到学习，可以分享自己的学习规划
```

这需要整合多个数据源：
- SNN drives → 内心状态
- 最近工具调用 → 在做什么
- TODO 列表 → 活跃 TODO
- Diary → 最近日记摘要
- 心跳独白 → 对话引导

**预计增加到 ~300-500 tokens**，但信息密度显著提高。

### 关键：对话引导

当前 Life 的心跳是"自我反思"模式，没有明确给 DFC 下达"对话指令"。
改为在心跳 prompt 中增加一个输出段：

```
你的独白之后，请附上一段给聊天系统的简要引导（不超过2句话）：
- 最近对什么话题感兴趣？
- 建议的语气/态度是什么？
- 有什么想主动和用户分享的吗？
```

**文件**: `plugins/life_engine/service.py` — `_build_heartbeat_model_prompt()`

### 验证
- [ ] Briefing 包含完整的5个段落
- [ ] DFC prompt 中可见结构化 Briefing
- [ ] 对话引导段非空

---

## Phase 3: 缓存优化 Prompt 布局

### 问题
Reminder 注入到 USER#1 **最前面**，破坏 history 的缓存前缀。

### 方案A：Reminder 后置（推荐）

修改 `_apply_reminders()` 逻辑：将 reminder 追加到第一个 USER block 的**末尾**而非开头。

**文件**: `src/kernel/llm/context.py` — `_apply_reminders()`

```python
# 修改前：reminder_parts + original_content
# 修改后：original_content + reminder_parts
```

这样 USER#1 的结构变为：
```
[USER#1]
  # 历史消息（半静态）    ← prefix cache 可命中
  ...
  <system_reminder>       ← 动态内容在后面
  潜意识状态
  </system_reminder>
  # 新消息（动态）
  ...
```

### 方案B：独立 Briefing Payload（备选）

将 Briefing 从 USER#1 中拆出来，放在独立的 USER payload 中：
```
[SYSTEM] 人格 → 静态
[TOOL] ...    → 静态
[USER#0] Life Briefing → 动态但与聊天历史分离
[USER#1] History + Unreads → 半静态
```

方案A更简单且不需要修改框架核心结构，优先采用。

### 验证
- [ ] 在支持 prefix caching 的场景下，检查 token usage 是否有改善
- [ ] 确认 model 仍然能正确读取 reminder 内容

---

## Phase 4: DFC "询问中枢" 工具

### 问题
DFC 只有固定注入，无法按需查询 Life 的记忆、日记、状态。

### 方案
新增 DFC 工具 `action-consult_nucleus`，允许 DFC 在回复前查询 Life：

**文件**: `plugins/default_chatter/tools/consult_nucleus.py` (新文件)

```python
class ConsultNucleusTool(BaseTool):
    """向生命中枢查询信息。"""
    
    name = "action-consult_nucleus"
    description = "向你的生命中枢/内心查询信息，比如最近在想什么、有没有相关的记忆、日记里写了什么、TODO进度等。"
    
    parameters = {
        "query": {
            "type": "string",
            "description": "你想问中枢的问题，比如'我最近有没有关于XX的记忆？'、'我的TODO列表里有什么？'"
        }
    }
    
    async def execute(self, query: str) -> str:
        # 调用 life_engine 的内部查询接口
        service = get_service("life_engine:service:life_engine_service")
        result = await service.handle_dfc_query(query)
        return result
```

**文件**: `plugins/life_engine/service.py` — 新增 `handle_dfc_query()`

```python
async def handle_dfc_query(self, query: str) -> str:
    """
    处理来自 DFC 的信息查询。
    不需要调 LLM，直接检索本地数据源。
    """
    results = []
    
    # 1. 记忆搜索
    if memory_service:
        memories = await memory_service.search(query, top_k=3)
        if memories:
            results.append("【记忆检索】\n" + format_memories(memories))
    
    # 2. 最近日记
    diary_context = self._get_recent_diary_snippet(query)
    if diary_context:
        results.append("【相关日记】\n" + diary_context)
    
    # 3. TODO 状态
    todos = self._get_active_todos()
    if todos:
        results.append("【活跃TODO】\n" + format_todos(todos))
    
    # 4. SNN/调质状态
    state = self._get_current_state_summary()
    results.append("【当前状态】\n" + state)
    
    return "\n\n".join(results) if results else "暂时没有找到相关信息"
```

### 注册到 DFC 工具列表
**文件**: `plugins/default_chatter/plugin.py` — 工具注册处

### 验证
- [ ] DFC 在对话中能调用 consult_nucleus
- [ ] 返回结果包含记忆/日记/TODO/状态
- [ ] 查询不影响 Life 的正常心跳流程

---

## Phase 5: DFC Prompt 精简

### 方案
在 Phase 2 生效后，部分 DFC system prompt 中的内容可以精简：

1. **行为限制列表**：Life Briefing 中的"对话引导"已涵盖语气/态度/话题
2. **负面行为提醒**：保留但精简
3. **主题引导**：由 Life Briefing 动态提供，不再需要静态模板

**文件**: `plugins/default_chatter/prompt_builder.py`

预计精简 ~200-400 tokens system prompt。

---

## 实施顺序

```
Phase 1 (Reminder 刷新)   ← 最高优先级，修复根本 Bug
    ↓
Phase 2 (丰富 Briefing)    ← 核心改进，显著提升 DFC 信息质量
    ↓
Phase 3 (缓存优化)         ← 性能优化
    ↓
Phase 4 (询问中枢工具)     ← 新功能
    ↓
Phase 5 (Prompt 精简)      ← 可选优化
```

Phase 1-3 可以在同一个 commit 中完成（都是现有代码修改）。
Phase 4 需要新增文件和跨插件注册。
Phase 5 在 Phase 2 验证效果后再决定精简范围。

---

## 风险与回退

| 风险 | 缓解措施 |
|------|---------|
| Reminder 后置导致模型忽略 | 用 `<system_reminder>` 标签保证可见性 |
| Enriched Briefing 过长 | 设置硬上限 500 tokens |
| consult_nucleus 延迟 | 不调 LLM，纯本地检索，<100ms |
| DFC精简过度丢失人格 | Phase 5 保守操作，人格核心不动 |

