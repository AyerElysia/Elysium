# 第 5 章 · 主意识–潜意识同步：LifeChatter 与 Life Engine

> *"主意识不是孤立的嘴，潜意识也不是沉默的后台任务。系统真正有生命感的地方，在于二者以不同节律异步运行，却能在表达时同步成同一个主体。"*

---

## 5.1 为什么旧 DFC 接口不再是核心

早期 Neo-MoFox-Soul 使用 `default_chatter` / DFC 作为对外对话入口，再通过 `consult_nucleus`、`nucleus_tell_dfc`、runtime injection 等桥接机制与 Life Engine 通信。这个设计解决了“后台状态如何影响对话”的第一版问题，但它有一个根本缺陷：**对话系统与生命中枢仍然是两个外置模块，信息传递像跨系统通信，而不是同一主体的内部同步**。

当前架构已经迁移到 **LifeChatter + Life Engine**：

- **LifeChatter** 是当前社交主意识，注册为 `life_chatter`，负责用户可见的对话、工具调用、行动输出和内心独白记录。
- **Life Engine** 是潜意识系统，负责心跳、SNN、调质、记忆、做梦、事件流和 ThoughtStream。
- 二者位于同一 `life_engine` 插件边界内，减少了旧 DFC 桥接中的插件间裸依赖和状态损耗。

因此，本章不再讨论“DFC 如何查询中枢”，而讨论“同一主体的主意识与潜意识如何同步”。

![Figure F13 · Life Engine → LifeChatter 异步同步层](/root/Elysia/Neo-MoFox/Report/04_figures/F13_consciousness_sync.png)

*Figure F13 · 潜意识侧的梦境残影、状态摘要、内心独白和 ThoughtStream 经异步同步层进入 LifeChatter 的表达上下文。*

## 5.2 同步目标：不是传命令，而是传状态

Life Engine 向 LifeChatter 同步的信息，不应该被理解成“后台命令前台说什么”。更准确的理解是：潜意识提供状态，主意识保留表达决策。

同步层至少要满足四个目标：

1. **状态可见**：LifeChatter 能知道当前调质、记忆、思考流和最近内心独白，而不是只依赖聊天历史。
2. **表达自主**：Life Engine 可以提供“我最近在想什么”，但最终是否说、怎么说，由 LifeChatter 在当前对话语境中决定。
3. **异步解耦**：Life Engine 不因等待用户回复而阻塞，LifeChatter 也不因每次消息都执行完整后台推理而拖慢响应。
4. **可审计**：同步内容必须能在事件流、日志、状态文件或 prompt 快照中被检查，避免变成不可解释的提示词魔法。

这个设计比“让 LLM 假装有内心”更强：内心独白、思考流和梦境残影都有独立的生成、存储和同步路径。

## 5.3 同步通道一：运行态快照

LifeChatter 每次构建上下文时，会读取 Life Engine 提供的同源运行态快照。该快照可以包含：

- 最近 Life Engine 心跳产生的内部状态；
- 调质层的离散状态摘要；
- SNN / drive 的可读化输出；
- 最近事件高水位之后的新事件；
- 做梦或小憩产生的残影；
- ThoughtStream 的当前焦点与背景在意；
- runtime assistant injection 队列中的内心独白片段。

运行态快照的意义在于：LifeChatter 不需要重新计算潜意识，只需要读取潜意识已经维护好的“当前我是什么状态”。这类似无人系统中的状态估计模块：控制器不必每次从原始传感器重新推导完整世界模型，而是读取已经融合过的状态估计。

## 5.4 同步通道二：内心独白记录

LifeChatter 本身也会向潜意识回写。当前实现中，`action-record_inner_monologue` 用于记录对话器视角下的内心独白。它的作用不是发给用户，而是把“主意识此刻如何理解局面”写回 Life Engine 运行态。

这形成一个闭环：

```text
Life Engine 状态
  -> LifeChatter 上下文
  -> LifeChatter 内心独白
  -> Life Engine 事件流
  -> 后续心跳 / 记忆 / ThoughtStream
```

这个闭环很关键。否则主意识只是读取潜意识，却不会把自己的理解反馈回去；系统就会退化成“后台状态注入前台 prompt”。内心独白让 LifeChatter 的判断成为潜意识后续演化的输入。

## 5.5 同步通道三：主动机会与 pass-and-wait

LifeChatter 不只在用户明确发消息时运行。系统还存在主动机会和续话机会：当 Life Engine 判断某个时刻可能适合表达，LifeChatter 会被触发进入一轮决策。

这一轮不必一定发消息。LifeChatter 可以：

- 调用 `action-record_inner_monologue` 记录此刻真实想法；
- 调用 `action-life_send_text` 向用户表达；
- 调用 `action-life_pass_and_wait` 放弃本次表达，等待更合适的时机。

这比“潜意识直接发话”更自然。潜意识只提供机会和状态，主意识决定是否开口。对课程报告而言，这相当于把无人系统中的“自主决策”拆成两层：后台状态估计与动机生成，前台行动选择与执行。

## 5.6 ThoughtStream：注意力脑区的工程类比

ThoughtStream 是潜意识管理、主意识可见的中期注意力结构。它保存系统近期持续在意的问题，并通过 `revision` 和焦点窗口同步给 LifeChatter。

脑科学中，注意力常被拆成不同网络。例如，Corbetta 与 Shulman 关于 goal-directed / stimulus-driven attention 的区分，把注意力分为目标驱动的背侧系统与显著事件驱动的腹侧系统；Petersen 与 Posner 的注意网络理论也强调警觉、定向和执行控制等功能分工。Neo-MoFox-Soul 不复刻真实脑区，而是借用这一功能划分来设计 ThoughtStream。

对应关系如下：

| 脑科学启发 | ThoughtStream 工程对应 | 作用 |
|---|---|---|
| 目标驱动关注 | 当前焦点 `last_focused_at` | 让系统持续追踪最近真正推进过的问题 |
| 显著事件重定向 | 事件/梦境/记忆触发 reactivate | 让新线索把休眠思考重新拉回 |
| 注意力衰减 | `curiosity_score` 半衰期 lazy decay | 避免旧兴趣永久占用上下文 |
| 执行控制 | `max_active` 与 dormancy | 限制活跃思考流数量，避免主意识过载 |
| 工作记忆同步 | `revision` 游标 | 让 LifeChatter 只看到新增或变化的注意力内容 |

相关脑科学资料可参考 Corbetta & Shulman (2002) 的综述、Petersen & Posner 的注意网络理论，以及 Vossel、Geng 与 Fink 对背侧/腹侧注意系统的综述。本文只采用这些理论的功能启发，不把 ThoughtStream 宣称为神经科学模型。

![Figure F21 · ThoughtStream 与注意力网络的工程类比](/root/Elysia/Neo-MoFox/Report/04_figures/F21_thought_stream_attention.png)

*Figure F21 · ThoughtStream 借用背侧/腹侧注意网络的功能划分来组织“当前焦点 / 背景在意 / 重定向”。图为工程隐喻，不是医学或神经科学等价断言。*

## 5.7 上下文同步的边界

同步层有意避免两种极端。

**第一，避免全量注入。** 如果每轮对话都把所有 SNN 状态、调质浓度、记忆节点、思考流和事件历史塞进 prompt，LLM 会被无关信息淹没。LifeChatter 需要的是经过筛选的运行态摘要，而不是数据库转储。

**第二，避免完全按需查询。** 如果主意识必须显式调用某个工具才知道潜意识状态，那么它很容易因为没有调用工具而“忘记自己有潜意识”。当前实现通过 transient context 和 ThoughtStream 同步，让关键状态自然出现在主意识上下文中。

因此，Neo-MoFox-Soul 采用折中策略：

- 稳定人格与规则放在 system 层；
- 聊天历史和未读消息放在 user 层；
- 潜意识运行态放在 transient context；
- ThoughtStream 按焦点/背景分组裁剪；
- 需要深入细节时再调用检索或工具。

## 5.8 失败模式与约束

这套同步机制仍有明确局限。

1. **信号压缩损耗**：潜意识状态必须被压缩成文本摘要才能进入 LLM，上下文表达可能丢失细节。
2. **表达解释权仍在 LLM**：同一运行态快照可能被不同模型或不同 prompt 解释成不同话语。
3. **ThoughtStream 不是真实注意力脑区**：它只实现了焦点、衰减、重定向和同步游标等工程机制。
4. **主动表达需要约束**：如果主动机会过多，系统会变成打扰用户；如果过少，潜意识状态又难以外化。
5. **旧 DFC 兼容残留**：仓库中仍可能保留 default_chatter 相关代码和历史文档，但报告中的当前架构以 LifeChatter 为准。

这些局限是第 13 章局限性讨论的一部分。它们不削弱双意识架构的价值，反而说明这个设计比“写一段 prompt 让模型装作有内心”更可检查。

## 5.9 小结

本章完成了从旧 DFC 桥接到当前双意识同步的重写。LifeChatter 是社交主意识，Life Engine 是潜意识；ThoughtStream 是潜意识维护、主意识可见的注意力流；内心独白和运行态快照让二者形成闭环。下一章转向 SNN，说明后台快层如何提供时间连续的状态基质。
