# unfinished_thought_plugin 修改草案

## 目标

把 `unfinished_thought_plugin` 从“对话复盘器”调整成“未完成念头采集器”。

当前问题不是单点 bug，而是输出风格被系统性推向了总结：

- 扫描提示词天然鼓励 `title + content + reason` 的完整解释结构
- 输入状态里带了过多整理信息，包含 `reason`、历史记录、时间戳、统计项
- 使用的模型任务偏叙事，容易把碎片整理成复盘句
- prompt 注入块也使用了“标题：内容”的展示方式，进一步强化了说明文风格

这个草案的核心目标是：

1. 保留原有功能，不破坏自动扫描、持久化、命令管理、prompt 注入。
2. 让新写入的内容更短、更碎、更像“后台挂着的念头”。
3. 让模型更容易输出“未竟片段”，而不是“总结结论”。

## 现状判断

### 1. 扫描提示词过于“可解释”

当前扫描系统提示词要求输出：

- `new_thoughts`
- `updates`
- `resolved_ids`
- `paused_ids`

并且示例里明确写了：

- `content`: “我刚刚其实还没把那个话题想完”
- `reason`: “话题被切走了”

这类结构不是错，但会让模型倾向于“补全解释”，而不是保留悬而未决感。

相关位置：

- [prompts.py](/root/Elysia/Neo-MoFox_Deployment/Neo-MoFox/plugins/unfinished_thought_plugin/prompts.py#L19)
- [prompts.py](/root/Elysia/Neo-MoFox_Deployment/Neo-MoFox/plugins/unfinished_thought_plugin/prompts.py#L75)

### 2. 喂给扫描模型的状态太完整

`snapshot()` 当前会输出完整 `thoughts`，而 `thoughts` 本身又包含：

- `reason`
- `created_at`
- `updated_at`
- `last_mentioned_at`
- `mention_count`

这会让模型看到一份“整理过的知识卡片”，不是“心理片段”。

相关位置：

- [service.py](/root/Elysia/Neo-MoFox_Deployment/Neo-MoFox/plugins/unfinished_thought_plugin/service.py#L227)
- [service.py](/root/Elysia/Neo-MoFox_Deployment/Neo-MoFox/plugins/unfinished_thought_plugin/service.py#L532)

### 3. 实际数据已经向总结风格漂移

现有数据里不少条目已经长成：

- “某人发来某句话，所以这说明什么”
- “这是在回应什么，所以话题已结束”
- “对话已有新进展，所以该标记为 resolved”

这不是“念头碎片”，而是“事件说明书”。

相关样例：

- [private 样例](/root/Elysia/Neo-MoFox_Deployment/Neo-MoFox/data/unfinished_thoughts/private/5750ede86191a7126b731cc03325ba9c520f8bf00fba38775b73259621bff177.json#L11)

## 修改策略

建议把改造拆成三层。

### 第一层：输入层瘦身

给扫描模型的输入，改成“最小必要信息”。

建议新增一个专门给模型看的轻量视图，例如：

- `thought_id`
- `title`
- `content`
- `status`
- `priority`

不要把下面这些字段直接喂给模型：

- `reason`
- `created_at`
- `updated_at`
- `last_mentioned_at`
- `mention_count`
- `history` 里的统计项

这样做的目的不是丢数据，而是避免模型先读到“解释结果”，再反向生成“解释结果”。

建议实现方式：

1. 在 `UnfinishedThoughtState` 里新增一个 `scan_snapshot()`。
2. `snapshot()` 继续保留给命令和调试。
3. 扫描时改为只传 `scan_snapshot()`。

### 第二层：提示词重写

把系统提示词从“整理器”改成“碎片采集器”。

新的提示词应当明确强调：

- 输出的是未完成的想法，不是结论
- 输出应该像“卡住的心声”，不是“事情总结”
- 允许非常短，甚至只有半句
- 禁止写成复盘口吻

建议加入明确禁区：

- 不要写“这是在……”
- 不要写“说明……”
- 不要写“已经结束”
- 不要写“这意味着……”
- 不要写“所以……”

建议输出风格示例改成更碎的形式，例如：

- “刚才那个点还没咬住”
- “这句话有点想回避”
- “想先放一放”
- “还差一点没说出口”

同时，`reason` 字段不建议作为强制解释存在。可以保留，但要把它从“解释原因”改成“触发标签”。

例如：

- 旧：`reason = "话题被切走了"`
- 新：`reason = "cut_off"`

如果你要保留可读性，可以在 UI 里另做中文解释，不直接把解释喂回模型。

### 第三层：后处理约束

为了避免模型偶尔又回到总结模式，建议在 LLM 输出后加一层轻量校验。

校验重点不是语法，而是风格。

建议拦截以下特征：

- 含有过多因果词：`因为`、`所以`、`说明`、`导致`
- 含有收束词：`已经`、`结束`、`完成`、`自然结束`
- 含有复盘词：`这是在`、`这意味着`、`可以看出`
- `content` 过长，明显超过“念头碎片”范围

如果命中太多，可以：

1. 直接丢弃该条
2. 或标记为低可信度，不进入主池
3. 或要求重新扫描一次

这样可以把“偶发总结化输出”挡在存储前。

## 建议的数据结构调整

不建议大改存储格式，优先兼容现有 JSON。

推荐新增一个内部字段视图概念：

### `display_view`

给命令和 UI 看，保留完整字段。

### `scan_view`

只给模型看，保留最少字段。

### `prompt_view`

给主回复模型注入，尽量短，只保留：

- 当前状态
- 一句碎片标题
- 一句碎片内容

这三者职责分开后，系统就不容易在“扫描结果”和“主回复注入”之间互相污染。

## 建议的代码改动点

### 1. `service.py`

建议新增：

- `scan_snapshot()`
- `render_prompt_fragment()` 或沿用现有 `render_prompt_block()` 但改格式
- `is_summary_like(text)` 之类的轻量判定

建议修改：

- `scan_thoughts()` 里传给提示词的状态改为轻量版
- `_apply_scan_result()` 里对 `new_thoughts` 和 `updates` 加风格过滤
- `_make_thought_payloads()` 的抽样内容更短

### 2. `prompts.py`

建议重写：

- `build_unfinished_thought_scan_system_prompt()`
- `build_unfinished_thought_scan_user_prompt()`
- `build_unfinished_thought_prompt_block()`

重点不是增加内容，而是减少“总结暗示”。

### 3. `config.py`

建议增加几个开关，方便调试和回滚：

- `scan.use_scan_view_only`：默认开启，控制是否只喂轻量状态
- `scan.reject_summary_like_output`：默认开启，控制是否过滤总结风格
- `prompt.compact_mode`：默认开启，控制注入是否更短
- `model.task_name`：建议后续切到专门的 `unfinished_thought` 任务

这样你后面要调试时，可以很容易地临时放宽约束。

## 推荐的新提示词方向

建议把扫描目标改成下面这种语义：

```text
你不是总结器，也不是复盘器。
你只负责捕捉那些还悬着、没落地、像半句话一样挂在后台的念头。

输出要短，要碎，要像没说完。
优先保留“还在想”“还没咬住”“想回避”“想继续”的感觉。
不要补全背景，不要解释原因，不要给结论。
```

对于 `new_thoughts.content`，建议明确限制成“1 到 2 句内的碎片表达”。

对于 `updates.content`，建议只允许：

- 微调措辞
- 改短
- 改状态

不要允许模型顺手把内容扩写成一大段解释。

## 预期效果

改完后，理想状态应该是：

- 新条目更像“脑内卡住的一小截”
- 已有条目不会被反复写成复盘句
- 主模型注入时看到的是轻量碎片，而不是说明书
- 扫描结果更稳定，不会一会儿像念头，一会儿像总结

## 风险点

1. 如果过滤太严，可能导致扫描结果过少。
2. 如果输入瘦身过头，模型可能识别不到上下文。
3. 如果 prompt 改得太抽象，可能让模型输出过于空泛。

所以建议采用“先瘦输入，再加约束，再加过滤”的顺序，不要一口气把所有限制都上满。

## 建议的落地顺序

1. 先加 `scan_view`，把扫描输入瘦身。
2. 再重写扫描提示词，明确禁止总结化表达。
3. 再加输出校验，拦截复盘风格。
4. 最后微调 prompt 注入块，让主回复模型看到更短的碎片。

## 结论

这个插件的问题不是“没有生成念头”，而是“生成出来的内容太像整理稿”。

最有效的改法，不是继续加更多规则，而是：

- 减少喂给模型的解释性信息
- 明确把输出拉回碎片状态
- 给总结化输出加一层过滤

这样才能让 `unfinished_thought_plugin` 真正像“未完成念头池”，而不是“聊天复盘池”。
