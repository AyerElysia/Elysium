# 飞书 reaction processor not found 诊断（2026-08-02）

## 结论

`im.message.reaction.created_v1` 与 `im.message.reaction.deleted_v1` 已在飞书应用侧订阅并通过长连接送达，但 Elysium 的 `FeishuAdapter` 只向 Lark SDK Dispatcher 注册了 `im.message.receive_v1`。SDK 收到没有处理器的表情回应事件后记录 `processor not found`，随后丢弃该事件。

这不是连接断开，也与 Voice Live 无关。普通文字消息仍然正常进入 `FeishuAdapter`；当前影响仅是爱莉无法感知用户添加或删除的表情回应，同时 Lark SDK 会产生 ERROR 级日志噪音。

## 证据

- `2026-08-02T23:25:04.155`：`FeishuAdapter` 正常记录一条群聊消息；
- 随后长连接连续收到 reaction created/deleted 事件；
- 每条 reaction 事件均报 `err: processor not found`，连接 ID 保持不变；
- `plugins/feishu_adapter/adapter.py::_build_lark_event_handler()` 仅调用 `register_p2_im_message_receive_v1(on_message)`；
- 当前安装的 Lark SDK 同时提供：
  - `register_p2_im_message_reaction_created_v1`；
  - `register_p2_im_message_reaction_deleted_v1`。

## 风险判断

- 普通消息收发：不受该错误直接影响；
- 飞书长连接：没有因该错误退出；
- 表情回应语义：当前丢失；
- 日志质量：会产生误导性的 ERROR 噪音。

## 后续处理选项

1. 如果不需要让爱莉感知表情回应：在飞书开放平台取消订阅 reaction created/deleted；
2. 如果需要感知：为两个事件注册处理器，将 message id、reaction type、operator 与 created/deleted 动作转换为统一生命事件；
3. 不建议只做空处理器长期吞掉事件；如短期降噪，至少应以 DEBUG 记录并明确 `ignored_by_policy`。

本次仅做只读诊断，没有修改飞书适配器，也没有停止或重启 Elysium。
