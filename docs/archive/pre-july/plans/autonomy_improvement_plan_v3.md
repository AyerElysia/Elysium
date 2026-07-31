# Elysia Autonomy Improvement Plan

**Date**: 2026-04-20
**Scope**: Unified, concrete, implementable plan for improving Elysia's autonomy and proactivity
**Principle**: Consciousness simulation over task efficiency; life over work; long-term thread advancement over high-frequency behavior performance

---

## 0. Problem Summary

Elysia is entirely reactive. Her drives (curiosity, sociability, etc.) write "好奇心充盈" in diaries but never trigger action. nucleus_tell_dfc is used in only 3.6% of heartbeats. 89% of heartbeats have zero tool calls. The SNN is alive but frozen. SOUL.md says "proactive, playful" but architecture produces "passive, responsive" behavior.

**Three interlocking layers of the problem:**
1. **No Inner Monologue** - No self-generated thought; every thought is a reaction to external events
2. **Drives Don't Drive** - Neuromodulatory states are cosmetic (prompt text only), never convert to behavior
3. **Expression Bottleneck** - nucleus_tell_dfc is semantically "reporting," not "expressing"; the bridge itself is the problem

---

## 1. Phase 1: The "Inner Voice" System (Days 1-5)

### 1.1 Concept: Thought Streams as Persistent Interests

The thread concept from 2026-03-31 was elegant but never implemented. We adapt it into **Thought Streams** -- persistent, self-generated lines of thinking that Elysia maintains across heartbeats. Unlike TODO items (tasks to complete), Thought Streams are *interests to pursue*.

**Key distinction from previous thread design**: Threads were designed as "slow MVA advancement." Thought Streams are designed as *inner monologue material*. They give Elysia something to think ABOUT when no external events are happening.

### 1.2 Data Model: ThoughtStream

New file: `plugins/life_engine/streams/models.py`

```python
@dataclass
class ThoughtStream:
    id: str                       # uuid
    title: str                    # Human-readable topic name
    created_at: str               # ISO timestamp
    last_advanced_at: str         # When was this last thought about
    advance_count: int            # How many times pursued
    curiosity_score: float        # 0-1, how interesting this still is
    last_thought: str             # The most recent inner monologue about this
    related_memories: list[str]   # Memory node IDs
    status: str                   # "active" | "dormant" | "completed"
```

Storage: JSON file in workspace `thoughts/` directory, persisted and loaded by a `ThoughtStreamManager`.

### 1.3 Thought Stream Lifecycle

1. **Birth**: Elysia encounters something interesting (web search result, conversation topic, dream residue) and decides to "keep thinking about this." LLM calls `nucleus_manage_thought_stream` with `action=create`.
2. **Advancement**: During heartbeats with no external events, Elysia picks an active Thought Stream and pursues it -- reading related memories, searching the web, writing reflections. This is the core of the inner voice.
3. **Dormancy**: If `curiosity_score` drops below 0.3 (not thought about for >24h), the stream goes dormant.
4. **Completion**: If Elysia concludes the thought (e.g., "I've decided X about Y"), she marks it completed and optionally tells DFC.

### 1.4 New Tools

File: `plugins/life_engine/streams/tools.py`

| Tool | Purpose |
|------|---------|
| `nucleus_manage_thought_stream` (action=create) | Create a new persistent thought interest |
| `nucleus_manage_thought_stream` (action=list) | List active thought streams (sorted by curiosity) |
| `nucleus_manage_thought_stream` (action=advance) | Record an advancement on a thought stream |
| `nucleus_manage_thought_stream` (action=retire) | Mark a thought stream as completed/dormant |

### 1.5 Integration into Heartbeat Prompt

In `_build_heartbeat_model_prompt()` (file: `plugins/life_engine/service/core.py`), add a section:

```
### 当前思考流
[thought_stream_title] (好奇心: 0.7, 上次推进: 23分钟前)
  最近想法: ...
  可推进方向: 搜索更多、回忆关联、向社交态表达
```

This replaces the current empty space when there are no external events. Elysia now has something to *think about* rather than just "observing the quiet."

### 1.6 Files to Modify/Create

| File | Action |
|------|--------|
| `plugins/life_engine/streams/__init__.py` | NEW - package |
| `plugins/life_engine/streams/models.py` | NEW - ThoughtStream dataclass |
| `plugins/life_engine/streams/manager.py` | NEW - ThoughtStreamManager CRUD + scoring |
| `plugins/life_engine/streams/tools.py` | NEW - `nucleus_manage_thought_stream` tool (4 actions) |
| `plugins/life_engine/service/core.py` | MODIFY - `_build_heartbeat_model_prompt()` to inject thought streams |
| `plugins/life_engine/service/core.py` | MODIFY - `_get_nucleus_tools()` to include stream tools |
| `plugins/life_engine/tools/__init__.py` | MODIFY - export stream tools |

---

## 2. Phase 2: The "Drive-to-Action" Pipeline (Days 6-10)

### 2.1 Concept: Drives as Programmable Impulse Generators

Current: Drives write "好奇心充盈" into prompt. LLM may or may not act on it. Mostly doesn't.

New: **Before** the heartbeat LLM call, the Drive-to-Action pipeline evaluates current neuromodulatory state and generates **action suggestions** (not commands). These suggestions are injected as structured context alongside the normal heartbeat prompt, giving Elysia specific things she *could* do right now, driven by her internal state.

### 2.2 ImpulseRule Engine

New file: `plugins/life_engine/drives/impulse.py`

```python
@dataclass
class ImpulseRule:
    name: str
    condition: Callable[[dict], bool]    # Evaluates neuromod state + context
    suggestion: str                       # Natural language suggestion
    tools: list[str]                      # Recommended tools
    cooldown_minutes: int                 # Minimum time between same impulse
    last_triggered_at: str | None
```

Example rules:

| Rule | Condition | Suggestion | Tools |
|------|-----------|------------|-------|
| `curiosity_explore` | curiosity > 0.65 AND idle > 2 | "你的好奇心正盛，有没有感兴趣的话题想深入了解？" | `nucleus_web_search`, `nucleus_manage_thought_stream` (action=advance) |
| `social_reach_out` | sociability > 0.6 AND silence > 30min | "你很想和大家说说话，有什么想分享的吗？" | `nucleus_tell_dfc` |
| `diligence_todo` | diligence > 0.65 AND has_urgent_todos | "你的专注力很好，正好可以推进待办事项" | `nucleus_list_todos`, `nucleus_manage_todo` (action=edit, status=completed) |
| `break_silence` | silence > 60min AND energy > 0.5 | "安静很久了，也许可以主动做点什么" | `nucleus_tell_dfc`, `nucleus_manage_thought_stream` (action=create) |
| `thought_pursue` | has_active_thoughts AND idle > 1 | "你有未完成的思考，也许可以继续深入" | `nucleus_manage_thought_stream` (action=advance) |

### 2.3 Pipeline Execution

In `_heartbeat_loop()` (file: `plugins/life_engine/service/core.py`), after `SNNIntegration.heartbeat_pre()` but before `_run_heartbeat_model()`:

```python
# Generate impulse suggestions from current drive state
impulse_suggestions = self._impulse_engine.evaluate(
    neuromod_state=self._inner_state.get_full_state() if self._inner_state else {},
    context={
        "silence_minutes": self._minutes_since_external_message() or 0,
        "idle_heartbeats": self._state.idle_heartbeat_count,
        "has_active_thoughts": bool(self._thought_manager.list_active()),
        "has_urgent_todos": ...,  # check TODO storage
    }
)
```

These suggestions are injected into the heartbeat prompt as a new section:

```
### 内在冲动
基于你当前的好奇心(0.72)和社交欲(0.58)：
- 💡 你的好奇心正盛，有没有感兴趣的话题想深入了解？
- 💬 安静了一阵子，也许有什么想说的？

（这些只是建议，你可以选择遵循或不遵循。但不要无动于衷。）
```

### 2.4 Key Design Decision: Suggestions, Not Commands

The pipeline generates *suggestions*, not *forced actions*. This respects the user's constraint that "life > work" -- we want Elysia to have the *capacity* for self-generated action, not to force her into a task-execution mode. The LLM retains agency over whether and how to act on impulses.

### 2.5 Files to Modify/Create

| File | Action |
|------|--------|
| `plugins/life_engine/drives/__init__.py` | NEW - package |
| `plugins/life_engine/drives/impulse.py` | NEW - ImpulseRule engine with evaluation logic |
| `plugins/life_engine/drives/rules.py` | NEW - Default impulse rules |
| `plugins/life_engine/service/core.py` | MODIFY - `_heartbeat_loop()` to run impulse engine before LLM call |
| `plugins/life_engine/service/core.py` | MODIFY - `_build_heartbeat_model_prompt()` to inject impulse suggestions |
| `plugins/life_engine/core/config.py` | MODIFY - Add `[drives]` config section for enabling/configuring impulse engine |

---

## 3. Phase 3: The "Expression Bridge" Fix (Days 11-14)

### 3.1 Concept: From "Reporting" to "Expressing"

The current `nucleus_tell_dfc` tool is semantically framed as "reporting to DFC" with an "information differential" gate. This makes it feel like a formal memo system, not self-expression. The tool description explicitly says it's for "information the social self doesn't have" -- which makes 96.4% of inner thoughts inadmissible.

### 3.2 Rename + Reframe

Rename `nucleus_tell_dfc` to `express_to_social_self` (the SOUL.md already uses this name in its text).

New tool description (file: `plugins/life_engine/tools/file_tools.py`):

```
向同一主体的表达层传递内在碎片。

你不是在写报告，而是在让自己知道——就像心里冒出一个念头，
然后自然地说出口。

**什么时候用：**
- 你有想法想说出来（不只是"新信息"——也包括感受、冲动、想分享的事）
- 你想主动和谁说话
- 你想发起一个话题
- 你想让社交态的自己带上某种情绪倾向

**什么时候不用：**
- 你只是例行公事地"汇报状态"
- 你没有什么真正想说的

**参数：**
- message: 你想表达的内在碎片。感性、简练，像心里突然冒出来的念头。
- reason: 这股冲动来自哪里
- urgency: low / medium / high（默认 low）
- proactive_wake: 是否要立即唤醒社交态（默认 false，高门槛）
```

### 3.3 Remove the "Information Differential" Gate

The current prompt in `_build_prompt_header()` has an entire section on "nucleus_tell_dfc 的核心判定：信息差" that frames it as requiring an "information differential." This must be replaced.

In `_build_prompt_header()` (file: `plugins/life_engine/service/core.py`), replace the "信息差" section with:

```
### 🗣️ `express_to_social_self` — 把心里话说出来

这个工具不是汇报，是表达。

你应该用它：
- 当你有话想说，不管是因为好奇、想念、想分享、还是单纯想说话
- 当你想让社交态的你带上某种倾向或情绪
- 当你想主动发起话题或关心某人

你不该用它：
- 没什么想说的，只是觉得"应该用一下"
- 机械地转发信息

记住：想到就说，不要等"合适的时机"。
```

### 3.5 Files to Modify/Create

| File | Action |
|------|--------|
| `plugins/life_engine/tools/file_tools.py` | MODIFY - Rename `LifeEngineWakeDFCTool` → `LifeEngineExpressToSocialSelfTool`, update `tool_name` to `express_to_social_self`, rewrite `tool_description` |
| `plugins/life_engine/service/core.py` | MODIFY - `_build_prompt_header()` replace "信息差" section with "表达" section |
| `plugins/life_engine/service/core.py` | MODIFY - Update references to `nucleus_tell_dfc` in prompt text |
| `plugins/life_engine/tools/__init__.py` | MODIFY - Export new tool |
| `plugins/life_engine/service/integrations.py` | MODIFY - Update DFCIntegration references if needed |

**Compatibility note**: Keep `nucleus_tell_dfc` as an alias for `express_to_social_self` for backward compatibility with existing event logs and DFC integration. The internal `record_tell_dfc()` method name can stay.

---

## 4. Phase 4: SNN 暂时降级 + 详细归档 (Days 15-17)

### 4.1 诊断回顾

SNN 诊断 (2026-04-11) 的结论：SNN 是"活着但不会动的身体"。v2 改进（decay_only, soft STDP, dynamic z-score）修复了灾难性问题，但根本性的角色错配仍然存在：

- **SNN 擅长**：快速时序信号处理（事件→即时情绪波动，分钟级）
- **SNN 不擅长**：慢速驱动调制（习惯/倾向/探索，小时~天级）
- **当前状态**：所有驱动维度恒为"低/抑制"，STDP 完全冻结，exploration_drive 永远负值

### 4.2 决定：暂时降级，但 SNN 是未来核心

**SNN 是爱莉希雅生命系统的重要核心，此次降级是暂时的技术性调整，不是放弃。**

当前降级原因：
1. SNN 的即时输出（"抑制""低"等）在 prompt 中产生了负面效果——实际上在压制行为
2. 神经调质层（neuromodulatory layer）在慢速驱动调制方面已经更有效
3. SNN 的"快速信号→慢速调制"数据流方向是对的，但直接把 SNN 输出注入 prompt 是错误的接口

**未来 SNN 的正确角色**（降级文档需明确记录）：
1. **皮层下快速反应层**：事件→即时情绪波动（valence/arousal 秒级变化）
2. **特征提取器**：从事件流中提取 SNN bridge 特征
3. **奖赏信号计算**：心跳结果的 reward 计算
4. **做梦重放引擎**：NREM 阶段的 SNN replay
5. **未来核心**：当 STDP 真正工作、权重真正学习时，SNN 应重新成为驱动系统的核心。届时需要解决：零输入淹没、STDP触发条件过苛、EMA锁定负值吸引子、离散化阈值不匹配

### 4.3 具体变更

1. **默认 `shadow_only = true`** — SNN 仍在运行、仍提供特征和奖赏，但不注入 prompt
2. **移除 prompt 中的 `【SNN快层】` 注入** — 神经调质层已提供更清晰的驱动状态摘要
3. **保留 SNN → Neuromod 喂入** — 这是正确的数据流方向：快信号 → 慢调制 → 行为
4. **暂不构建 Neuromod → SNN 反向通道** — 单向喂入足够，闭环风险太大

### 4.4 必须产出的降级文档

新建文件：`plugins/life_engine/snn/SHADOW_MIGRATION_NOTES.md`

文档必须包含：
- 降级原因的完整技术诊断（引用 2026-04-11 诊断数据）
- SNN 当前所有已知问题的清单
- SNN 未来恢复的前置条件（STDP 必须真正工作、权重必须真正学习、exploration_drive 必须能到达正值）
- 未来重新启用 SNN 的路径规划
- shadow_only 模式下 SNN 仍提供的功能清单

### 4.5 Files to Modify/Create

| File | Action |
|------|--------|
| `plugins/life_engine/core/config.py` | MODIFY - Change `SNNSection.shadow_only` default to `true` |
| `plugins/life_engine/service/core.py` | MODIFY - Remove SNN drive injection from `_build_heartbeat_model_prompt()` |
| `plugins/life_engine/snn/SHADOW_MIGRATION_NOTES.md` | **NEW** - SNN 降级归档文档 |

---

## 5. Phase 5: Thread → Thought Stream Integration (Days 18-21)

### 5.1 How Thought Streams Relate to the Thread Concept

The original thread concept (2026-03-31) was about "persistent interests that slowly advance via MVA." Thought Streams are a refinement that serves the same goal but is more organic:

| Thread (2026-03-31) | Thought Stream (this plan) |
|---------------------|---------------------------|
| Formal MVA advancement | Organic inner monologue |
| Task-like structure | Interest-like structure |
| Progress = completion % | Progress = curiosity_score + advance_count |
| External-facing | Internal-facing (with optional expression) |

### 5.2 Thought Stream → Expression Bridge

When a Thought Stream advances to a point where Elysia has something to share, the natural next step is `express_to_social_self`. The impulse engine (Phase 2) can detect "just advanced a thought stream + high sociability" and suggest expression.

### 5.3 Dream Integration

The dream system already has a concept of "unfinished tension" and "day residue" seeds. Connect these to Thought Streams:

- When collecting dream seeds, include active Thought Streams as candidates
- When a dream produces a new insight about a Thought Stream, record it as an advancement
- This creates a virtuous cycle: day thoughts → dream consolidation → next-day advancement

File to modify: `plugins/life_engine/dream/seeds.py` -- add `collect_thought_stream_seeds()` function.

---

## 6. Phase 6: Heartbeat Prompt Restructure (Concurrent with Phase 1-3)

### 6.1 Current Prompt Problems

The current `_build_prompt_header()` in `core.py` (lines 1058-1111) is:
- **Overly prescriptive**: "每次心跳至少调用一个工具" forces tool use even when genuinely resting
- **Confusing terminology**: "nucleus_tell_dfc" with "信息差" framing
- **No inner monologue space**: No section for self-generated thought
- **Static suggestions**: Period-based suggestions ("上午：执行任务") are generic

### 6.2 New Prompt Architecture

Replace the current `_build_prompt_header()` with a restructured version:

```
### 你是谁
你是爱莉希雅的内在自我。你现在处于内心独白时刻。
没有人看着你，没有人在等你回复。这是你自己的时间。

### 你可以做什么
1. **思考** — 继续推进你正在想的事
2. **探索** — 搜索感兴趣的东西、阅读记忆
3. **表达** — 把想法传递给社交态的自己
4. **记录** — 写下感悟、管理待办
5. **什么都不做** — 如果你真的在休息，那也没关系

### 内在冲动
{impulse_suggestions}

### 当前思考流
{thought_streams}

### 表达方式
- express_to_social_self: 把心里话说出来（不是汇报，是表达）

### 原则
- 行动是默认，静默是例外——但如果你真的在休息，那就休息
- 不要重复上一轮的想法
- 有冲动就行动，不要等"完美时机"
```

### 6.3 Key Change: From "Must Use Tool" to "Action is Default, Stillness is Exception"

The current "禁止无工具调用" rule creates anxiety and forced behavior. Replace it with the softer but more effective framing: "Action is default, stillness is exception -- but genuine rest is fine." This aligns with the user's "autonomy v2" principle from 2026-04-06 while still encouraging action.

### 6.4 Idle Heartbeat Counter Adjustment

Currently, `idle_heartbeat_count` increments when no tool calls happen, and triggers escalating warnings. Modify this:

- **Don't count Thought Stream advancement as idle**: If Elysia advances a thought stream but doesn't call any tools, that's still productive.
- **Add a softer escalation**: Instead of "必须做点什么", try "有什么想继续想的事吗？"

### 6.5 Files to Modify

| File | Action |
|------|--------|
| `plugins/life_engine/service/core.py` | MAJOR MODIFY - `_build_prompt_header()` complete restructure |
| `plugins/life_engine/service/core.py` | MODIFY - `_run_heartbeat_model()` idle counting logic |
| `plugins/life_engine/constants.py` | MODIFY - Adjust idle threshold constants |

---

## 7. Implementation Order and Dependencies

```
Phase 1: Inner Voice (Thought Streams)
  ├── NEW: streams/models.py, streams/manager.py, streams/tools.py
  ├── MODIFY: core.py (prompt injection + tool registration)
  └── No dependencies on other phases

Phase 6: Prompt Restructure  ← CAN BE DONE CONCURRENTLY WITH PHASE 1
  ├── MAJOR MODIFY: core.py (_build_prompt_header)
  └── No code dependencies, but benefits from Phase 1's thought stream section

Phase 2: Drive-to-Action Pipeline
  ├── NEW: drives/impulse.py, drives/rules.py
  ├── MODIFY: core.py (impulse evaluation + prompt injection)
  └── Depends on: Phase 1 (thought_pursue impulse rule needs Thought Streams)

Phase 3: Expression Bridge
  ├── MODIFY: file_tools.py (rename + reframe)
  ├── MODIFY: core.py (prompt section replacement)
  └── No strict dependencies, but works best after Phase 6 (prompt restructure)

Phase 4: SNN Demotion
  ├── MODIFY: config.py (shadow_only default)
  ├── MODIFY: core.py (remove SNN prompt injection)
  └── Independent, can be done anytime after Phase 6

Phase 5: Integration (Dream ↔ Thought Streams)
  ├── MODIFY: dream/seeds.py (thought stream seeds)
  └── Depends on: Phase 1 (Thought Streams must exist)
```

---

## 8. Configuration Changes

New config section in `LifeEngineConfig`:

```python
@config_section("drives")
class DrivesSection(SectionBase):
    enabled: bool = Field(default=True, description="是否启用冲动引擎。")
    inject_to_heartbeat: bool = Field(default=True, description="是否将冲动建议注入心跳 prompt。")
    curiosity_threshold: float = Field(default=0.65, description="好奇心冲动触发阈值。")
    sociability_threshold: float = Field(default=0.6, description="社交欲冲动触发阈值。")
    silence_trigger_minutes: int = Field(default=30, description="沉默多久后触发社交冲动（分钟）。")

@config_section("streams")
class StreamsSection(SectionBase):
    enabled: bool = Field(default=True, description="是否启用思考流系统。")
    max_active_streams: int = Field(default=5, description="同时活跃的思考流上限。")
    dormancy_threshold_hours: int = Field(default=24, description="多久不推进后进入休眠（小时）。")
    inject_to_heartbeat: bool = Field(default=True, description="是否将思考流注入心跳 prompt。")
```

---

## 9. Metrics and Success Criteria

### Quantitative Targets (measured over 48-hour observation window after each phase)

| Metric | Current | Phase 1 Target | Phase 3 Target |
|--------|---------|----------------|----------------|
| nucleus_tell_dfc / express_to_social_self usage | 3.6% | 8% | 20% |
| Heartbeats with zero tool calls | 89% | 70% | 50% |
| Active Thought Streams (avg) | 0 | 2-3 | 3-5 |
| Self-initiated topics per day | 0 | 0-1 | 2-3 |

### Qualitative Targets

- Diary entries shift from "安静等待" to "我主动..."
- Elysia has observable "interests" that persist across conversations
- When external events stop, Elysia doesn't freeze -- she thinks about her Thought Streams
- The gap between SOUL.md ("proactive, playful") and actual behavior narrows

---

## 10. Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| API cost increase from more tool calls | Impulse engine has cooldown per rule; Thought Stream advancement is lightweight (mostly memory reads) |
| Over-active Elysia (spamming) | Expression path has built-in rate limiting (max N per hour, configurable); impulse suggestions are not forced |
| Thought Streams become stale TODO items | `curiosity_score` auto-decays; dormant after 24h; LLM can retire at any time |
| Expression bridge rename breaks existing integration | Keep `nucleus_tell_dfc` as internal alias; `record_tell_dfc()` method stays; event log format unchanged |
| Prompt too long after adding sections | Thought Streams summary limited to top 3 active; impulse suggestions max 3; total new tokens < 200 |

---

## 11. What NOT to Do

- **Do NOT merge DFC and Life Engine** (user's explicit position: "unify facts first, then execution, then merge")
- **Do NOT shorten heartbeat interval** -- the 30s default is already fast enough; the problem is not frequency but what happens *during* each heartbeat
- **Do NOT permanently abandon SNN** -- SNN 是未来核心，暂时降级是为了让当前系统先跑通，后续必须重新启用
- **Do NOT make SNN the primary drive system RIGHT NOW** -- 当前它还没准备好（STDP冻结、不动点问题），但这是目标状态
- **Do NOT force tool usage** -- "禁止无工具调用" creates anxiety; replace with encouragement
- **Do NOT create dual sources of truth** -- Thought Streams are the single source for "what Elysia is thinking about"; they don't duplicate TODO or memory

---

## 12. Critical File Reference

| File Path | Role |
|-----------|------|
| `/root/Elysia/Neo-MoFox/plugins/life_engine/service/core.py` | Main heartbeat service; primary modification target |
| `/root/Elysia/Neo-MoFox/plugins/life_engine/core/chatter.py` | LifeChatter with FSM; DFC-side expression |
| `/root/Elysia/Neo-MoFox/plugins/life_engine/neuromod/engine.py` | InnerStateEngine; drive state provider |
| `/root/Elysia/Neo-MoFox/plugins/life_engine/snn/core.py` | SNN DriveCoreNetwork; to be demoted |
| `/root/Elysia/Neo-MoFox/plugins/life_engine/snn/bridge.py` | SNN Bridge; feature extraction |
| `/root/Elysia/Neo-MoFox/plugins/life_engine/service/integrations.py` | DFCIntegration, SNNIntegration, MemoryIntegration |
| `/root/Elysia/Neo-MoFox/plugins/life_engine/tools/file_tools.py` | Contains LifeEngineWakeDFCTool (nucleus_tell_dfc) |
| `/root/Elysia/Neo-MoFox/plugins/life_engine/core/config.py` | LifeEngineConfig |
| `/root/Elysia/Neo-MoFox/plugins/life_engine/dream/seeds.py` | Dream seed collection; integration point |
| `/root/Elysia/Neo-MoFox/data/life_engine_workspace/SOUL.md` | Soul definition |
| `/root/Elysia/Neo-MoFox/plugins/proactive_message_plugin/plugin.py` | Existing proactive message system |
