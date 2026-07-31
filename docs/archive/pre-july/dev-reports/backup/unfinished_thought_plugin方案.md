# unfinished_thought_plugin 方案

## 目标

`unfinished_thought_plugin` 的目标，是为角色维护一份持续更新的“未完成念头池”。

它记录的不是事实，也不是意图，而是那些仍然悬而未决、在后台继续运行的内部片段：

- 被打断的话题
- 没说完的想法
- 暂时压下去的情绪
- 没处理完的小目标

这层能力的核心价值，是让角色在之后的对话里能够自然“想起来”，从而形成更强的连续思维感。

## 落地参数

第一版只保留最关键的运行参数：

- `trigger_every_n_messages`：每隔多少次有效对话触发一次扫描
- `history_window_size`：本次扫描时给念头模型看的历史记录条数
- `max_thoughts`：当前聊天流允许维护的未完成念头上限
- `prompt_inject_min` / `prompt_inject_max`：主模型随机注入的条数范围，默认 1 到 3

## 为什么需要它

旧的 `intent_plugin` 已经不适合这条路线，后续会直接移除，不再作为新插件的基础。

未完成念头和意图不是一回事：

- 意图回答“我接下来想做什么”
- 未完成念头回答“我刚才还有什么没想完”

人类的连续感很大一部分，不是来自明确目标，而是来自大量未完结的内部状态持续挂在后台。这个插件就是要模拟这一层。

## 核心定位

这个插件只负责“未完成状态”的维护。

它不做：

- 原始对话归档
- 长期事实记忆
- 自我叙事总结
- 情绪数值建模
- 长期关系评分

它负责：

- 发现对话中未完成的思维片段
- 以低频、稳定、可回收的方式保存它们
- 在合适时机重新浮现这些片段
- 让主模型表现出“我刚刚其实还在想这个”的连续性

## 设计原则

1. 未完成念头必须是“未完成”的，不是普通笔记。
2. 只记录少量高价值片段，避免后台噪音过多。
3. 每条记录必须可追踪来源、状态和触发原因。
4. 必须持久化，重启后可恢复。
5. 必须支持自动过期和人工清理。
6. 注入主模型时必须短、轻、按相关性选择，不可全量轰炸。

## 数据结构建议

建议把每条未完成念头设计成一条独立记录，而不是一大段文本。

### 基础字段

- `thought_id`
- `stream_id`
- `chat_type`
- `platform`
- `title`
- `content`
- `status`
- `priority`
- `reason`
- `created_at`
- `updated_at`
- `last_mentioned_at`
- `source_event`

### 字段说明

- `title`：短标题，便于列表展示，例如“刚才那个话题”“要补一句的看法”
- `content`：具体内容，建议 1 到 3 句
- `status`：状态，建议取值为 `open`、`paused`、`resolved`、`expired`
- `priority`：优先级，决定是否进入 prompt
- `reason`：为什么被记录，例如“被打断”“情绪暂压”“目标未完成”
- `source_event`：来源事件，例如 `on_chatter_step`、`on_prompt_build`、`on_diary_compact`

## 状态机建议

建议最少保留四种状态：

- `open`：当前仍然活跃，等待后续延续
- `paused`：暂时不处理，但仍保留
- `resolved`：已经被回收或补完
- `expired`：过期自动清理

状态转换建议：

- `open -> paused`：当前不再相关，但仍值得保留
- `open -> resolved`：后续已经自然完成
- `paused -> open`：再次被唤起
- `open/paused -> expired`：超过 TTL 或被手动清理

## 触发来源

这个插件建议采用“规则先筛、LLM 再归纳”的方式，不要完全依赖模型现编。

### 1. 对话中断

典型场景：

- 用户突然换话题
- 模型自己话说到一半被新的刺激打断
- 当前思路有明显延续性，但尚未展开完

### 2. 内部状态

典型场景：

- 有情绪但还没处理完
- 想表达但暂时压住了
- 有一个小目标刚刚形成，还没进入执行

### 3. 回收信号

典型场景：

- `self_narrative_plugin` 的 `open_loops` 可以落到这里
- 日记压缩后发现反复出现但尚未完成的主题
- 睡眠/苏醒切换后，角色重新回看后台挂起内容

## 更新机制

### 推荐触发方式

1. 每个聊天流独立计数
2. 每累计 `trigger_every_n_messages` 条有效对话，自动触发一次扫描
3. 扫描时只把最近 `history_window_size` 条历史记录交给念头模型

### 更新流程

1. 读取当前聊天流的全部未完成念头
2. 读取最近 `history_window_size` 条对话作为上下文
3. 让 LLM 基于“当前所有念头 + 最近历史”输出增删改结果
4. 合并结果到当前池子
5. 对超过 `max_thoughts` 的内容做裁剪
6. 将计数清零，等待下一轮触发
7. 如果本轮扫描失败，恢复本次触发前的消息计数，避免丢失下一次自动扫描节奏

### 更新原则

- 允许增删改，不允许一次性推翻整个池子
- 允许把不再相关的念头标记为 resolved
- 允许把暂时不用处理的念头标记为 paused
- 如果本次扫描没有有效结果，就保持现状，不强行写入

## 持久化建议

建议按聊天流隔离持久化，每个聊天流一个独立文件。

建议路径：

- `data/unfinished_thoughts/private/<stream_id>.json`
- `data/unfinished_thoughts/group/<stream_id>.json`
- `data/unfinished_thoughts/discuss/<stream_id>.json`

每个文件建议包含：

- `version`
- `stream_id`
- `chat_type`
- `updated_at`
- `message_count_since_scan`
- `items`
- `history`

这样重启后可以恢复，不同聊天对象之间也不会互相串。

## 对主模型的注入方式

不要把整个池子直接塞给主模型。

建议从当前活跃念头里随机抽 1 到 3 条进行注入，并且尽量短。

推荐格式：

```text
【未完成念头】
- 我刚刚还没把“xxx”想完
- 我对“yyy”还有一点没说清
```

### 注入原则

- 优先从 open / paused 中抽取
- 默认随机抽取 1 到 3 条
- 默认不显示完整历史
- 如果没有相关条目，就不注入

## 命令设计

建议提供一个最小可用命令集：

- `/unfinished_thought add`
- `/unfinished_thought view`
- `/unfinished_thought resolve`
- `/unfinished_thought pause`
- `/unfinished_thought clear`

### 命令语义

- `add`：手动添加一条未完成念头
- `add` 允许直接输入完整文本，带空格内容会按整段处理
- `view`：查看当前聊天流的未完成念头
- `resolve`：标记某条已完成
- `pause`：暂时挂起某条
- `clear`：清空当前聊天流池子，慎用

## 与现有插件的关系

### 与 `diary_plugin`

`diary_plugin` 提供的是原始素材和日记压缩结果。

`unfinished_thought_plugin` 可以从日记里反向提取：

- 没有说完的话
- 反复出现但未收束的主题
- 一直没解决的小目标

### 与 `self_narrative_plugin`

`self_narrative_plugin` 提供的是“我如何理解自己”。

`unfinished_thought_plugin` 提供的是“我还在想什么”。

两者建议联动，但不要合并成一个插件：

- self_narrative 偏身份
- unfinished_thought 偏过程

### 与 `intent_plugin`

`intent_plugin` 作为旧路线已经不再纳入本方案。

新的 `unfinished_thought_plugin` 不继承它的目标管理思路，也不复用它的“意图-目标”模型。

这次实现会直接围绕“未完成状态池”展开，避免再把角色导向任务系统。

## MVP 范围

建议第一版先只做最小闭环：

- 聊天流隔离存储
- 手动添加 / 查看 / 完成 / 暂停 / 清理
- 每隔固定对话数自动扫描一次
- 扫描时基于最近历史做增删改
- prompt 随机注入 1 到 3 条活跃念头
- 存量上限裁剪

先不要做：

- 复杂的长程任务管理
- 大规模语义检索
- 过多的状态层级
- 过重的评分系统

## 风险

### 1. 噪音过多

表现：

- 每轮都冒出很多“没说完的事”

解决：

- 限制数量
- 增加 TTL
- 增加相关性阈值

### 2. 过度拟人化

表现：

- 角色开始对每个小念头都“执念很重”

解决：

- 允许过期
- 允许 pause
- 不要把所有开放项都注入 prompt

### 3. 与自我叙事混淆

表现：

- 未完成念头和自我解释互相污染

解决：

- 严格区分字段和注入块
- self_narrative 只存解释
- unfinished_thought 只存后台片段

## 推荐实现顺序

1. 定义数据结构和持久化格式
2. 实现手动命令
3. 实现 prompt 注入
4. 接入对话事件，捕捉未完成信号
5. 接入日记 / 自我叙事的回收信号
6. 增加自动过期与清理

## 结论

`unfinished_thought_plugin` 适合作为你现在这套“意识模拟”里的下一层。

它比 `intent_plugin` 更轻，比普通记忆更像思维流，也更容易和 `self_narrative_plugin` 形成互补。

如果你要的是“角色真的像在后台继续想”，这个插件值得优先做。
