---
title: 内心独白注入上下文与调试报告
summary: 说明内心独白从生成到写入消息流、被 default_chatter 使用的链路，并给出调试要点
---

## 链路概览
- **生成**：`plugins/proactive_message_plugin/inner_monologue.py::generate_inner_monologue`
  - 构建 LLMRequest（SYSTEM + TOOL schema + USER prompt），注册 `ThinkTool / WaitLonger / SendTextAction`。
  - LLM 返回后解析 tool calls，提取 thought 与决策。
- **写入上下文**：`plugins/proactive_message_plugin/plugin.py::_inject_inner_monologue`
  - 构造 Message（content/processed_plain_text 均为 `[内心独白] ...`，sender_name=Bot（内心独白））。
  - 调用 `get_stream_manager().add_sent_message_to_history(message)`。
- **StreamManager 行为**：`src/core/managers/stream_manager.py::add_sent_message_to_history`
  - 持久化到 messages 表。
  - 同步写入内存：`chat_stream.context.history_messages.append(message)`，并移除同 ID 的 unread，保持上下文实时可见。
- **default_chatter 使用**：
  - 历史构建遍历 `chat_stream.context.history_messages`，未对 `is_inner_monologue` 或 bot 消息做过滤。
  - 格式化时用 `msg.processed_plain_text or content`，因此 `[内心独白] ...` 会原样进入 history 文本。
  - 发送前（已加调试）打印 `history_before_send`，可确认末尾是否含独白。

## 时序注意
- 注入发生在 **内心独白 LLM 响应之后**；只有注入后的 **下一次同 stream_id 的 default_chatter 请求** 才会在 prompt 里出现独白。
- 如果看的是触发独白的那轮 prompt 或换了 stream，自然看不到。

## 调试方法
- 日志查注入：`已注入内心独白到上下文：<stream_id>`（DEBUG）。
- 日志查上下文：`[调试] history_before_send 条数=...`，末尾应含 `[内心独白] ...`。
- 若条数为 0 或末尾无独白，检查：
  1) stream_id 是否一致；
  2) 是否已有下一轮 default_chatter 请求；
  3) 流是否被重建/上下文清空。

## 近期修正
- 修复内心独白解析 fallback 未定义变量的问题，fallback 改为 `(thought_from_think or message or "").strip()`。
- 精简 ThinkTool 返回：仅保留 `thought_recorded` + 简短提醒，不再回显 mood/decision/expected_response/thought_content。
- 在 default_chatter 发送前新增 `history_before_send` 调试打印，便于验证独白是否在上下文中。

## 实际验证
- 23:17:19 日志中 `history_before_send` 显示尾部含 `[内心独白] 小星星可能累了睡着了...`，说明注入链路与上下文读取均正常。
