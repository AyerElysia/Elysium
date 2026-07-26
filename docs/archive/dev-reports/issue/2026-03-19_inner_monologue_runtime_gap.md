---
title: 内心独白未实时出现在 DFC 提示词的原因与发现
summary: history 注入成功但 DFC 仅在构建时读取一次上下文，导致运行时新增的内心独白无法即时生效
---

## 现象
- 日志显示内心独白已写入 history（`已注入内心独白到上下文：<stream_id>`）。
- 但在同一轮 default_chatter（DFC）会话的后续 prompt 中看不到这条独白。
- 重启或下一轮重建 prompt 后才可见。

## 关键发现
- **history 注入是成功的**：`StreamManager.add_sent_message_to_history` 将独白写入 DB 和 `chat_stream.context.history_messages`。
- **DFC 构建 prompt 是一次性的**：当前运行的 DFC 会话在初次构建时读取了 history，但后续 follow-up 循环不会重新从 history 构建 payloads。
- 因此，运行时生成的内心独白仅存在于持久/内存历史，**不会自动进入当前已构建的 LLM payloads**，除非下一轮重建或重启。

## 根本原因
- 持久上下文（history_messages）与运行时上下文（已构建的 response.payloads / FSM state）分离。
- 我们只做了持久注入，没有做**运行时注入**（即把新增内容并入当前活跃的 DFC 消息流）。

## 结论
- 注入方式“写入 history”本身正确，但**缺少实时运行时注入**机制。
- 要让内心独白在当轮 DFC 内即时生效，需在运行时将独白并入当前的 payloads（或提供 runtime injection queue 在每次 send 前消费）。

## 后续建议（待定，不含实现）
- 保留 history 注入用于持久记录。
- 增加“运行时注入”机制：当独白生成时，向当前 stream 的活跃 DFC 会话注入一条上下文补丁，在下一次 send/follow-up send 前合并进 payloads。
- 或建立 runtime injection queue，在每次发送前消费，避免重建 prompt 才可见。
