---
title: 内心独白实时注入消息流实现报告
summary: 通过 assistant 伪装注入方式，让 proactive_message_plugin 生成的内心独白在当前活跃的 default_chatter 会话中即时可见，同时保留 history 持久化
---

## 背景
此前内心独白已经能够正确写入 `history_messages` 和数据库，但 default_chatter（DFC）对历史上下文的读取是“构建 prompt 时读取一次”。
因此，运行时新产生的内心独白虽然进入了 history，却不会自动进入当前已经构建好的 DFC 消息流，只能等下一轮重建时生效。

## 本次实现
采用“双轨注入”方案：

- **持久化轨道**：保留原有 `add_sent_message_to_history`，确保独白写入数据库与 `chat_stream.context.history_messages`
- **运行时轨道**：新增按 `stream_id` 管理的实时 assistant 注入队列，在 DFC 每次 `send()` 前消费并追加到当前 payloads 中

## 具体改动

### 1. default_chatter 新增运行时 assistant 注入队列
文件：`plugins/default_chatter/plugin.py`

新增：
- `_RUNTIME_ASSISTANT_INJECTIONS: dict[str, list[str]]`
- `push_runtime_assistant_injection(stream_id, content)`
- `consume_runtime_assistant_injections(stream_id)`

作用：
- 供其他插件按 `stream_id` 推入一条“实时 assistant 上下文”
- 在 DFC 发送前一次性消费，避免重复注入

### 2. default_chatter 在每次发送前追加实时 assistant 注入
文件：`plugins/default_chatter/runners.py`

新增：
- `_apply_runtime_assistant_injections(response, stream_id)`

行为：
- 在 enhanced/classical 两种模式下，每次 `response.send()` 前：
  - 读取当前 `stream_id` 的待注入文本
  - 以 `ROLE.ASSISTANT` + `Text(content)` 的形式追加到当前 `payloads`
- 若存在注入，会记录调试日志：
  - `[调试] 本轮实时注入 assistant 上下文 X 条`

### 3. proactive_message_plugin 在写 history 后同步推送 runtime 注入
文件：`plugins/proactive_message_plugin/plugin.py`

在 `_inject_inner_monologue()` 中：
- 先构造 `[内心独白] ...` 消息并写入 history
- 再调用 `default_chatter.plugin.push_runtime_assistant_injection(chat_stream.stream_id, history_text)`

效果：
- 内心独白既会出现在历史消息中，供未来轮次使用
- 也会实时进入当前活跃 DFC 消息流，供本轮 follow-up / 下次 send 立即可见

## 设计选择
本次选择将独白 **伪装为 assistant 消息** 注入 runtime，而不是 unread 或额外 system note，原因是：

- 更符合“自然堆叠在消息流里”的目标
- 与当前 DFC 的 payload 追加机制最兼容
- 不污染 unread 语义，不把 bot 自己的内部状态当作用户新消息处理

注入文本仍保留 `[内心独白]` 前缀，以便区分它不是对用户显式说出的话，而是内部状态投影进消息流。

## 当前结果
修复后，链路变为：

1. proactive 生成独白
2. 写入 history / DB
3. 推入 runtime assistant injection queue
4. DFC 在下次 `send()` 前消费该注入并追加到当前 payloads
5. 独白可以在当前活跃会话中即时生效，而无需等待重建 prompt 或重启

## 备注
如果后续希望进一步扩展，可将这套机制推广为通用的 runtime injection 能力，用于时间感知、记忆检索结果、notice 注入等场景。

## 新增：等待任务重置逻辑
- 需求：收到用户新消息时，应清零上次等待任务，重新从首轮等待时间开始内心独白流程。
- 改动：在 `_on_user_message` 中重置状态并调用 `_restart_waiting`，重新按 `first_check_minutes` 调度；`_restart_waiting` 内部直接调度新的等待任务。
