# Default Chatter 缓存友好改造与 Payload 策略报告

日期：2026-04-05

## 1. 目标

这次改造的目标有三个：

- 把 `default_chatter` 的提示词前缀尽量做稳定，提升缓存命中率。
- 保留 `thinking_plugin` 已经改成 `action-think` 的行为约束，同时减少它对上下文前缀的扰动。
- 处理 `payloads` 上下文持续膨胀的问题，不只是“超了再硬砍”，而是给出可持续的压缩策略。

## 2. 修改前的真实链路

这一步是本次改造的前提。先把链路梳理清楚，才能知道该在哪里动。

### 2.1 system prompt 的构建链

`default_chatter` 的 system prompt 主要经过以下路径：

1. `plugins/default_chatter/runners.py`
   - 在 `run_enhanced()` / `run_classical()` 中调用 `chatter.create_request("actor", with_reminder="actor")`
   - 然后再调用 `chatter._build_system_prompt(chat_stream)`，把 system prompt 作为 `ROLE.SYSTEM` payload 追加进去

2. `plugins/default_chatter/plugin.py`
   - `_build_system_prompt()` 调到 `DefaultChatterPromptBuilder.build_system_prompt()`

3. `plugins/default_chatter/prompt_builder.py`
   - 读取 `default_chatter_system_prompt` 模板
   - 注入人格、身份、背景、场景引导等字段

### 2.2 actor reminder 的真实注入位置

这是这次最关键的发现。

`with_reminder="actor"` 并不是把 actor bucket 里的内容加到 `ROLE.SYSTEM`，而是：

1. `src/core/components/base/chatter.py`
   - `create_request()` 里从 `SystemReminderStore` 取整桶 `actor`
   - 调 `context_manager.reminder(...)`

2. `src/kernel/llm/context.py`
   - reminder 不会立刻生成独立 payload
   - 它会在 `_apply_reminders()` 中，被插入到“首个 USER payload 的最前面”

所以，`actor` bucket 里任何会变化的内容，都会进入请求非常靠前的位置，直接破坏前缀稳定性。

### 2.3 user prompt 的构建链

`default_chatter_user_prompt` 的构建链是：

1. `plugins/default_chatter/prompt_builder.py`
   - `build_user_prompt()` 负责把 `history`、`unreads`、`extra` 填进模板

2. `src/core/prompt/template.py`
   - `PromptTemplate.build()` 会先发布 `on_prompt_build`

3. 各种插件在 `on_prompt_build` 上继续做二次注入
   - `diary_plugin` 的一次性摘要注入 `extra`
   - `self_narrative_plugin` / `drive_core_plugin` 还在改 system prompt 的尾部字段

这说明，“动态信息后移到 user extra”是顺着现有架构做的，不是另起一套旁路。

## 3. 修改前的主要问题

### 3.1 actor bucket 混杂了稳定规则和动态状态

原来 `actor` bucket 里混在一起的内容很多：

- 静态规则
  - `thinking_plugin` 的提示词
  - `emoji_sender` 的表情包使用引导
  - `diary_plugin` 的写日记引导

- 动态状态
  - `time_awareness_plugin` 注入的 `current_datetime`
  - `life_engine` 注入的 `subconscious`
  - `life_engine` 注入的 `生命中枢唤醒上下文`
  - `thinking_plugin` 的临时 `think_trigger_temp`

这些东西全部通过 `with_reminder="actor"` 一次性整桶拿走，前缀稳定性很差。

### 3.2 default_chatter 自己还在随机改 system prompt

`config/plugins/default_chatter/config.toml` 里原来开着：

- `probabilistic_persona_injection_enabled = true`

这意味着每轮 system prompt 都可能随机缺字段。就算 reminder 不变，system prompt 本身也不稳定。

### 3.3 payload 的膨胀不是单纯 max_payloads 能解决

底层 `LLMContextManager` 已经有两种裁剪逻辑：

- 常驻 `max_payloads` 裁剪
- 发送时按 token budget 做临时裁剪

但 `default_chatter` 没有真正接上 `compression_hook`，所以超限时主要还是“丢早期轮次”，没有结构化摘要承接。

## 4. 本次实际改了什么

### 4.1 给 reminder 读取加了排除名单能力

修改文件：

- `src/core/prompt/system_reminder.py`
- `src/app/plugin_system/api/prompt_api.py`
- `src/core/components/base/chatter.py`
- `src/core/components/base/agent.py`
- `src/app/plugin_system/api/llm_api.py`

核心变化：

- `SystemReminderStore.get()` 现在支持 `exclude_names`
- `create_request()` / `create_llm_request()` 现在可以指定：
  - `reminder_names`
  - `exclude_reminder_names`

这样就可以继续使用 `with_reminder="actor"`，但不必再整桶无差别注入。

### 4.2 default_chatter 的 actor 请求默认排除动态 reminder

修改文件：

- `plugins/default_chatter/plugin.py`
- `plugins/default_chatter/prompt_builder.py`

做法：

- 在 `DefaultChatter.create_request()` 中覆写基类逻辑
- 当 `task == "actor"` 且 `with_reminder == "actor"` 时，默认排除：
  - `current_datetime`
  - `subconscious`
  - `生命中枢唤醒上下文`
  - `think_trigger_temp`

这一步把最不稳定的几块从首个 user 前缀里剔掉了。

### 4.3 把动态 actor 信息后移到 user extra

修改文件：

- `plugins/default_chatter/prompt_builder.py`

新增逻辑：

- `build_dynamic_actor_extra()`
  - 从 `actor` bucket 单独读取动态块
  - 拼成一个“运行时上下文”区块
  - 再合并进 user prompt 的 `extra`

同时做了长度裁剪，避免动态块本身撑爆输入：

- `current_datetime` 上限 240 字符
- `think_trigger_temp` 上限 240 字符
- `生命中枢唤醒上下文` 上限 1200 字符
- `subconscious` 上限 1600 字符

这一步的意义是：

- 动态信息仍然可见
- 但它不再污染最前面的稳定前缀
- 即使波动，也只影响 user 末端区域

### 4.4 关闭随机人格注入

修改文件：

- `config/plugins/default_chatter/config.toml`

改动：

- `probabilistic_persona_injection_enabled = false`
- 各人格注入概率恢复为 `1.0`

这一步直接让 system prompt 结构稳定下来。

### 4.5 给 default_chatter 接上确定性上下文压缩

新增文件：

- `plugins/default_chatter/context_compression.py`

修改文件：

- `plugins/default_chatter/plugin.py`

做法：

- 在 `DefaultChatter.create_request()` 中，给 `actor` 请求挂 `compression_hook`
- 当 `LLMContextManager` 因 `max_payloads` 或 token budget 裁掉早期对话组时
- 压缩钩子会把被裁掉的组折成一条摘要 `USER` payload

摘要特点：

- 不额外调用 LLM
- 纯本地、确定性生成
- 保留用户说了什么、自己回复了什么、调用了哪些工具、工具结果是什么
- 控制摘要总长度与每条摘要片段长度

这不是 Claude Code 那种“专门开一轮模型做高质量总结”，但在当前架构下更稳：

- 不引入新 tool 链风险
- 不增加额外模型开销
- 可以先解决“早期轮次直接丢失”这个核心问题

### 4.6 收敛 thinking_plugin 的稳定提示块

修改文件：

- `plugins/thinking_plugin/plugin.py`
- `plugins/thinking_plugin/thinker_trigger.py`

做法：

- 原先静态注入 3 块：
  - `thinking_habit`
  - `thinking_fields`
  - `thinking_must_think`

- 现在合并为 1 块：
  - `thinking_contract`

另外：

- 兼容旧配置里的 legacy 文本替换逻辑还保留
- 临时 `think_trigger_temp` 仍然保留，但文案压缩成更短的一句提醒

这样做的目的：

- 稳定规则继续保留
- 但 actor bucket 里少了几段冗余块
- 前缀更短、更稳定

## 5. 这次没有做什么

### 5.1 没有把所有动态插件都一起迁走

目前仍然有两个方向的动态信息还在 system prompt 侧活动：

- `self_narrative_plugin`
- `drive_core_plugin`

它们通过 `on_prompt_build` 修改 system prompt 尾部字段，不走 actor bucket。

这意味着：

- 本次已经明显改善缓存友好性
- 但 system prompt 仍不是“完全静态”

如果后续要继续压缓存命中率，下一步就该把这两个插件也迁到 user extra。

### 5.2 没有改成“额外开一轮 LLM 做上下文摘要”

原因不是做不到，而是当前阶段不值得立刻引入。

直接上 LLM 摘要的问题：

- 会引入新的模型调用成本
- 会增加 tool 链路复杂度
- 如果 summary prompt 自己不稳，收益会被吃掉

所以这次先采用确定性本地压缩，先把架构收住。

## 6. payload 策略的结论

这次的 payload 策略不是“只要超限就压缩”，而是四层一起做：

1. 先稳定前缀
   - 关闭 system prompt 随机化
   - 把动态 actor reminder 从首个 user 前缀里排除

2. 再后移动态信息
   - `subconscious`
   - `wake_context`
   - `current_datetime`
   - `think_trigger_temp`

3. 对动态块本身做长度限制
   - 防止 user extra 失控膨胀

4. 对真正超限的历史轮次做摘要压缩
   - 用 `compression_hook` 把被裁掉的轮次收敛成一条摘要 payload

这个策略比单纯“达到阈值就压缩”更稳，因为它先解决“为什么老是超”的结构问题，再处理“超了之后怎么办”。

## 7. 回退保护是怎么做的

因为当前仓库本身有很多和本次任务无关的脏改动，所以没有直接做统一提交，而是做了三层保护：

### 7.1 git 书签

创建了一个指向当前 HEAD 的书签分支：

- `codex/cache-friendly-pre-20260405`

用途：

- 保留本次动手前的仓库基点

### 7.2 文件级备份

目录：

- `.codex-backups/cache_friendly_20260405/`

内容：

- 本次可能修改的文件原始副本

用途：

- 即使某些目录被 git ignore，也能直接从备份恢复

### 7.3 tracked patch

文件：

- `.codex-backups/cache_friendly_20260405/tracked_changes.patch`

用途：

- 对 tracked 文件可以直接重放或人工审查 patch

## 8. 验证情况

已完成的最小验证：

- `py_compile` 通过
- import 验证通过：
  - `plugins.default_chatter.plugin`
  - `plugins.thinking_plugin.plugin`
- `SystemReminderStore.get(..., exclude_names=...)` 行为验证通过
- 动态 actor 上下文后移到 user extra 的拼装验证通过
- compression hook 输出合法 `USER` payload 的验证通过

## 9. 当前剩余风险

### 9.1 system prompt 仍有动态注入源

如前所述：

- `self_narrative_plugin`
- `drive_core_plugin`

这两块还会继续改 system prompt。

### 9.2 thinking_plugin 目录被 git ignore

这意味着：

- 相关改动不会自然出现在 `git status`
- 主要依赖备份目录做回退

### 9.3 本地压缩摘要质量有限

本地确定性摘要的优点是稳，但它不如专门的 LLM 摘要细腻。

当前取舍是合理的，但如果以后对长会话质量要求更高，可以考虑在后续版本加一层“低频 LLM 总结器”。

## 10. 后续建议

建议按下面顺序继续推进：

1. 把 `self_narrative_plugin` 从 system prompt 迁到 user extra
2. 把 `drive_core_plugin` 从 system prompt 迁到 user extra
3. 观察实际日志里压缩触发频率
4. 如果长会话质量仍然不够，再考虑引入低频 LLM summary

## 11. 总结

这次改造的核心不是“改一句提示词”，而是把 prompt 与 payload 的职责重新分层：

- 稳定规则留在前缀
- 高频动态状态后移到 user extra
- 超限历史不再直接丢弃，而是确定性压缩成摘要

这样做以后，`default_chatter` 的请求会更像一个“稳定骨架 + 轻量动态尾部”的结构，缓存命中、上下文可控性、调试可理解性都会比之前好很多。
