# Payloads 与连续记忆修复报告

## 1. 这次要解决的问题

本次修改针对两个直接影响 `default_chatter` prompt 质量的问题：

1. 历史消息文本里泄露了内部生成的消息 ID。
   典型表现是 prompt 中出现：

   - `action_send_text_xxx`
   - `inner_monologue_xxx`
   - `api_emoji_xxx`

   这类 ID 对模型几乎没有正向价值，只会污染历史消息文本。

2. `diary_plugin` 的连续记忆仍然注入在 `default_chatter_system_prompt`。
   这会导致 system 前缀不稳定，不利于缓存友好；同时连续记忆本质上属于“慢变化状态”，不该和稳定规则层混在一起。

此外，用户额外强调了一个边界：

- `think` / `send_text` 等 action 的 payload 历史本身是有意义的，不能为了“清 prompt”把它们从上下文里删掉。

本次修复严格遵守这个边界。

## 2. 修复思路

### 2.1 payload 历史不删，只清理“历史消息文本”里的噪音

我保留了 LLM payload 中原有的 action / tool call / tool result 轨迹，不去动模型需要看的调用历史。

我只修改了“消息格式化为历史文本”的环节：

- 当 `message_id` 是内部生成标识时，不再展示
- 外部正常消息 ID 仍然保留

当前被视为内部噪音 ID 的前缀：

- `action_`
- `tool_`
- `inner_monologue_`
- `api_`
- `life_`
- `system_`

这样做的结果是：

- 模型仍然能从 payload 看到先前 `action-think` 的四个参数
- 但不会再在“历史消息文本”里看到一堆 `action_send_text_xxx`

### 2.2 连续记忆从 system 层移到 user 侧的状态层

我把连续记忆的默认注入目标从：

- `default_chatter_system_prompt`

改成了：

- `default_chatter_user_prompt`

同时没有简单粗暴地把它塞进 `extra`，而是做成单独的 `continuous_memory` 区块，并在 `default_chatter` 里做了两步处理：

1. 先从当前轮 user prompt 文本里拆出连续记忆块
2. 再按顺序重新拼回 USER payload：

   - 基础 user prompt
   - 运行时状态块
   - 连续记忆块

这样满足了“连续记忆层放在状态的后面”的要求。

### 2.3 防止跨轮重复堆积

连续记忆改到 USER payload 之后，如果不处理旧轮残留，就会和之前的运行时状态一样，越聊越重复。

因此我把这两类块统一纳入“状态块剥离”逻辑：

- `runtime_state_block`
- `continuous_memory_block`

每一轮都会：

1. 先从旧的 USER payload 中剥掉旧状态块
2. 再挂上本轮最新状态块

这样连续记忆不会在 payloads 里无限累积。

## 3. 具体修改点

涉及文件如下：

- `src/core/components/base/chatter.py`
  - 增加内部消息 ID 过滤逻辑
  - 基础消息格式化不再输出内部 message_id

- `plugins/default_chatter/plugin.py`
  - user prompt 模板新增 `{continuous_memory}`
  - 历史消息格式化复用内部 ID 过滤逻辑
  - 消息格式说明改为“消息 ID（如有）”

- `plugins/default_chatter/prompt_builder.py`
  - 增加连续记忆块标签与拆分逻辑
  - 增加统一的状态块剥离逻辑
  - USER payload 组装顺序调整为“状态 -> 连续记忆”

- `plugins/default_chatter/runners.py`
  - `_prepare_runtime_state_user_content()` 改为：
    - 先剥旧状态块
    - 再拆连续记忆
    - 最后按顺序挂回

- `plugins/diary_plugin/event_handler.py`
  - 对注入到 `default_chatter_user_prompt` 的连续记忆增加块标签包装

- `plugins/diary_plugin/config.py`
- `config/plugins/diary_plugin/config.toml`
  - 连续记忆默认注入目标改为 `default_chatter_user_prompt`

## 4. 验证结果

已执行的验证：

- `python -m py_compile` 验证以下文件通过
  - `plugins/default_chatter/prompt_builder.py`
  - `plugins/default_chatter/runners.py`
  - `plugins/default_chatter/plugin.py`
  - `plugins/diary_plugin/event_handler.py`
  - `plugins/diary_plugin/config.py`
  - `src/core/components/base/chatter.py`

- 最小行为验证
  - 连续记忆块可以从当前 USER 文本中拆出
  - 重新组装后顺序为：
    - 基础文本
    - runtime state
    - continuous memory
  - 再执行 strip 后，只保留基础文本

- 消息格式化验证
  - 普通外部消息 ID 仍显示
  - `action_send_text_*` 等内部 ID 不再显示

## 5. 这次刻意没做的事

这次没有删除 payload 里的 action/tool 历史。

原因很明确：

- `action-think` 的四个参数本来就是给模型下一轮看的
- 如果把这些 payload 历史删掉，会直接削弱角色连续性和行为连续性

所以本次只清理“历史消息文本里的无意义内部 ID”，不清理 action/tool 的真实调用轨迹。

## 6. 当前剩余风险

有一个现实边界需要说明：

- 这次修复对“新构建的 prompt / 新一轮 USER payload”有效
- 如果某个长会话在修复前已经把旧版连续记忆塞进了现存上下文，旧内容不会自动从已经存在的 system payload 里倒流清除

通常重新进入新一轮构建、重启相关会话流程，或让新的 `default_chatter` 请求链重新建立后，就会进入修复后的结构。
