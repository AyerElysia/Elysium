# message_send 子模块

对应源码目录：src/core/transport/message_send

## 模块目标

message_send 负责将 core 侧 Message 下发到目标适配器，并在发送前后接入事件与历史记录。

## 关键文件

- message_sender.py: MessageSender 主实现。
- converter.py: 复用 message_receive 的 MessageConverter。
- __init__.py: 单例入口。

## send_message 流程

1. 确定目标 adapter_signature（显式传入或按 platform 推断）。
2. 获取 AdapterManager 并查找活跃 adapter 实例。
3. 调用 _apply_bot_sender_info 用 bot 信息覆盖 sender 字段。
4. 使用 MessageConverter.message_to_envelope 转换消息。
5. 发布 ON_MESSAGE_SENT 发送前事件；若 decision=STOP 或 continue_send=False 则中止发送。
6. 对曾发生投递状态未知超时的相同消息执行短窗去重。
7. 调用 adapter._send_platform_message 真正下发。
8. 调用 StreamManager.add_sent_message_to_history 写入发送历史。
9. 适配器成功且历史写入完成后发布 ON_MESSAGE_DELIVERED 确认投递事件。

虚拟发送遵循相同事件契约：先发布 ON_MESSAGE_SENT，允许拦截；历史写入完成后再发布 ON_MESSAGE_DELIVERED。

## 关键约束

- ON_MESSAGE_SENT 始终是发送前可拦截事件，不表示平台已经收到消息。
- ON_MESSAGE_DELIVERED 仅表示适配器调用成功且发送历史写入完成；需要确认送达的观察者应订阅该事件。
- adapter_signature 推断依赖 registry 中 adapter_cls.platform。
- 出站拦截属于预期行为，send_message 返回 True 但不发送，也不发布 ON_MESSAGE_DELIVERED。
- timeout 短窗去重只在 ON_MESSAGE_SENT 允许发送后执行，因此每次尝试仍会经过前置拦截。
- stream_id 缺失时会跳过实际历史写入并记录告警；由于历史未落盘，该分支不发布 ON_MESSAGE_DELIVERED。

## 常见问题

- 找不到 adapter：检查 adapter 是否已启动且平台注册值匹配。
- 消息发出但无历史：检查 stream_id 和 get_or_create_stream 参数。
- 事件拦截导致不发：检查 ON_MESSAGE_SENT 订阅者的 decision 和 continue_send。
- 观察者收到发送前消息：确认它需要的是 ON_MESSAGE_SENT 还是 ON_MESSAGE_DELIVERED。
