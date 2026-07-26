# DFC <-> 生命中枢异步交流桥实现报告

日期：2026-03-30

## 目标

为 `default_chatter` 增加一个只暴露给 DFC 的中枢交流工具，使 DFC 可以把问题或想法异步投递给 `life_engine`，但不等待中枢同步返回；由中枢在后续 heartbeat 中自行整理，并在准备好后继续通过现有 `nucleus_wake_dfc` 主动唤醒 DFC。

## 实现内容

### 1. DFC 侧新增桥接工具

新增文件：

- `plugins/default_chatter/nucleus_bridge.py`

新增工具：

- `tool-message_nucleus`

行为：

- 只负责把消息投递给 `life_engine`
- 不同步等待中枢回复
- 成功后明确返回“不要等待即时回复；等中枢整理好后会主动唤醒你”
- `chatter_allow = ["default_chatter"]`，不会暴露给其他 chatter

### 2. DFC 工具调用时自动补齐上下文

修改文件：

- `plugins/default_chatter/plugin.py`

修改点：

- 在 `DefaultChatter.run_tool_call(...)` 中对 `tool-message_nucleus` 做定向处理
- 自动补齐当前 `trigger_msg` 的：
  - `stream_id`
  - `platform`
  - `chat_type`
  - `sender_name`

这样模型调用工具时不需要自己拼这些内部字段，降低误用概率。

### 3. 中枢侧新增异步接收入口

修改文件：

- `plugins/life_engine/service.py`

新增方法：

- `LifeEngineService.enqueue_dfc_message(...)`

行为：

- 校验 `life_engine` 是否启用
- 校验留言非空
- 将 DFC 留言构造成统一事件流中的 `MESSAGE` 事件
- 直接进入 `_pending_events`
- 持久化到 `life_engine_context.json`
- 写入日志，等待后续 heartbeat 消费

事件表现形式：

- `event_type = MESSAGE`
- `content_type = dfc_message`
- `source_detail` 显式标记为 `DFC 留言给生命中枢`

### 4. 组件暴露与文档同步

修改文件：

- `plugins/default_chatter/manifest.json`
- `plugins/default_chatter/README.md`
- `plugins/life_engine/README.md`

内容：

- `default_chatter` manifest 新增 `message_nucleus`
- README 补充 DFC 与中枢的异步双向链路说明
- `life_engine` README 修正工具数量统计，并说明 `message_nucleus -> enqueue_dfc_message -> heartbeat -> nucleus_wake_dfc` 的完整路径

## 测试与验证

### 自动化检查

执行：

```bash
python -m compileall /root/Elysia/Neo-MoFox/plugins/default_chatter /root/Elysia/Neo-MoFox/plugins/life_engine
```

结果：

- 通过

### pytest

执行：

```bash
pytest -q -o addopts='' \
  /root/Elysia/Neo-MoFox/test/plugins/life_engine/test_service.py \
  /root/Elysia/Neo-MoFox/test/plugins/test_default_chatter_nucleus_bridge.py \
  /root/Elysia/Neo-MoFox/test/plugins/test_default_chatter_tool_flow.py \
  /root/Elysia/Neo-MoFox/test/plugins/test_default_chatter_send_text_action.py
```

结果：

- `9 passed, 13 skipped`

说明：

- 当前环境缺少 `pytest-asyncio`
- 因此异步测试在 pytest 下被跳过，不是代码失败，而是测试运行器能力不足

### 手工异步验证

为避免异步测试被跳过后留下盲区，额外执行了一段手工验证脚本，实际跑通了以下场景：

1. `LifeEngineService.enqueue_dfc_message(...)` 正常入队并写日志
2. `MessageNucleusTool.execute(...)` 能通过插件管理器把消息投递到 `life_engine`
3. `DefaultChatter.run_tool_call(...)` 能自动补齐 `stream_id/platform/chat_type/sender_name`

结果：

- 通过，输出：`manual async verification: ok`

## 风险与说明

1. 当前桥接是异步投递，不保证中枢一定回复。
2. 中枢是否唤醒 DFC，仍由 heartbeat 中的模型自主决定。
3. 当前环境若要把异步单测纳入标准 pytest 流程，需要补装 `pytest-asyncio`。

## 结论

这次改动完成了你要的那条链路：

- DFC 可以像“和另一个自己说话”一样，把话丢给中枢
- DFC 不会被同步等待卡住
- 中枢会在后续 heartbeat 里慢慢整理
- 中枢整理好后再主动唤醒 DFC，把想法带回外部对话
