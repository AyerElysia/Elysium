# 第 11 章 · LifeChatter Agent 框架工程

> *"让 LifeChatter 有手有脚，是让主意识能够行动；让它能读到潜意识，是让行动不只是即时反射。"*

---

## 11.1 本章定位

前一章解释了 LifeChatter 与 Life Engine 如何形成主意识–潜意识同步。本章转向工程实现：LifeChatter 如何把人格、历史、记忆、运行态快照、ThoughtStream 和工具列表组装成一次可执行的 LLM 请求，并在多轮推理中决定回复、等待、调用工具或记录内心独白。

早期报告把这一层写成 DFC 工程；当前应以 `plugins/life_engine/core/chatter.py` 中的 `LifeChatter` 为准。它不是 default_chatter 的附属壳，而是 Neo-MoFox-Soul 当前社交主意识的执行器。

## 11.2 Tool / Action / Agent 三分类

Neo-MoFox-Soul 仍然沿用框架层的三类可调用组件：

| 类型 | 工程语义 | 是否直接产生外部动作 | LifeChatter 中的典型例子 |
|---|---|---|---|
| Tool | 查询或计算 | 否 | 记忆检索、状态查询、文件/日程/搜索类工具 |
| Action | 对当前 ChatStream 执行动作 | 是 | `action-life_send_text`、`action-life_pass_and_wait`、`action-record_inner_monologue` |
| Agent | 独立推理单元 | 视实现而定 | 深层记忆检索、复杂任务子代理 |

这个分类的价值在于控制副作用。Tool 不应随意向用户发消息；Action 绑定当前对话流；Agent 可以拥有独立推理上下文，但不能污染 LifeChatter 当前轮的上下文闭包。

![Figure F18 · 工具注册与 Schema 生成流程](/root/Elysia/Neo-MoFox/Report/04_figures/F18_tool_schema_pipeline.svg)

*Figure F18 · 工具类通过类型注解生成 Schema，经注册表进入 LLM 可调用组件列表。该机制对 LifeChatter 与其他 chatter 通用。*

## 11.3 LifeChatter 专用 Action

当前主意识最关键的不是“能不能调用工具”，而是“怎样把行动边界收紧”。LifeChatter 专用 Action 提供了这种边界。

### `action-life_send_text`

`life_send_text` 是 LifeChatter 的文本发送动作。它要求输出是纯文本正文，支持分段发送，避免 LLM 把 `reason`、`thought`、`expected_reaction` 等内部元信息泄漏给用户。对社交主意识而言，这相当于“说出口”的动作边界。

### `action-life_pass_and_wait`

`life_pass_and_wait` 表示本轮不回复、等待新消息。它很重要，因为主动机会不等于必须打扰用户。LifeChatter 可以在读取潜意识状态后判断“现在不说更好”，从而保留社交克制。

### `action-record_inner_monologue`

`record_inner_monologue` 把主意识此刻的理解写回 Life Engine。它不是用户可见回复，而是系统内部闭环的一部分。主动机会轮次通常要求先记录内心独白，再决定发送或等待，这能避免 LifeChatter 只做外部动作而不回写内部状态。

## 11.4 上下文组装：主意识看到什么

LifeChatter 每轮 LLM 请求不是简单拼接最近聊天记录，而是把多类上下文压成一个可控输入：

![Figure F19 · LifeChatter 上下文组装层次图](/root/Elysia/Neo-MoFox/Report/04_figures/F19_lifechatter_context_assembly.png)

*Figure F19 · LifeChatter 的上下文由人格、历史、记忆、运行态、ThoughtStream 注意力块等多层共同组成。*

核心层次如下：

| 上下文层 | 来源 | 作用 |
|---|---|---|
| Persona | 人格配置与系统模板 | 定义“我是谁、怎么说话、边界是什么” |
| History / Unreads | ChatStream 历史与未读消息 | 提供当前对话语境 |
| Memory | Life Engine 记忆检索与摘要 | 提供长期上下文 |
| Runtime | Life Engine transient snapshot | 注入调质、事件、梦境残影、内心独白等运行态 |
| ThoughtStream | `ThoughtStreamManager.format_for_prompt()` | 提供当前焦点与背景在意 |
| Usables | Tool / Action / Agent 注册表 | 告诉 LLM 当前能做什么 |

这种组装方式对应第 5 章的同步目标：潜意识提供状态，主意识保留表达决策。

## 11.5 ThoughtStream 的 prompt 经济性

ThoughtStream 不能全量注入，否则会把对话上下文变成思考流数据库。当前实现采用三个约束：

- **数量裁剪**：只展示前若干条活跃思考流；
- **焦点分组**：按 `focus_window_minutes` 分为“当前焦点”和“背景在意”；
- **增量标记**：通过 `revision_cursor` 标记 LifeChatter 上次读取后刚推进的思考流。

这种设计兼顾了连续性和上下文经济性：LifeChatter 能知道潜意识最近在意什么，但不会被全部历史淹没。

## 11.6 多轮推理循环

LifeChatter 的执行不是一次模型调用后直接结束，而是一个受控的多轮状态机。其核心相位包括：

| 相位 | 作用 |
|---|---|
| `WAIT_USER` | 等待用户消息或主动机会 |
| `MODEL_TURN` | 调用 LLM 产生工具/动作决策 |
| `TOOL_EXEC` | 执行 Tool / Action / Agent |
| `FOLLOW_UP` | 根据执行结果继续推理或收敛 |

![Figure F20 · LifeChatter 多轮推理循环状态机](/root/Elysia/Neo-MoFox/Report/04_figures/F20_reasoning_loop_fsm.svg)

*Figure F20 · 多轮推理循环使 LifeChatter 能在同一轮对话中完成思考、工具调用、回复、等待或停止。*

状态机中有几条关键约束：

- **禁止 think-only 空转**：只调用思考动作却不回复也不等待，会被系统提醒重试；
- **必须回复轮次不可 pass**：如果上游判定当前消息必须回应，LifeChatter 不能用 pass 逃避；
- **主动机会先记录内心独白**：主动表达前先把此刻想法写回 Life Engine；
- **分段发送约束**：较长回复应使用数组分段发送，避免一大段文本破坏聊天质感。

这些约束让 LifeChatter 更像一个可控的行动主体，而不是一个任意输出文本的裸 LLM。

## 11.7 与无人系统课程主题的对应

从无人系统视角看，LifeChatter 的工程层相当于“决策与执行控制器”：

- Tool / Agent 提供环境查询和复杂推理能力；
- Action 提供受控执行边界；
- runtime snapshot 相当于状态估计输入；
- ThoughtStream 相当于中期注意力和任务上下文；
- pass-and-wait 相当于“不行动也是一种决策”；
- inner monologue 回写相当于将执行器状态反馈给系统内部模型。

因此，Neo-MoFox-Soul 的社交对话不是单纯聊天功能，而是自主系统闭环中的执行端。

## 11.8 小结

本章把 Agent 工程层从旧 DFC 叙事迁移到了当前 LifeChatter 实现。LifeChatter 通过 Tool / Action / Agent 获得行动能力，通过多层上下文组装获得自我状态，通过多轮状态机控制回复、等待和内心独白回写。它与 Life Engine 的关系不是“前台问后台”，而是同一主体的主意识与潜意识在不同节律下同步运行。
