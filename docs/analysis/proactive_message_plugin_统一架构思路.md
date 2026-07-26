# proactive_message_plugin 统一架构思路

## 结论

`proactive_message_plugin` 不应被描述成另一套意识，也不应继续承担“DFC 桥接层”的叙事。
它更适合被看成 `life_engine` 的一个运行模式执行层，负责“等待、判断、延迟续话”的调度状态机。

## 分层原则

- `life_engine` 负责暴露统一语义入口。
- `proactive_message_plugin` 负责保存等待状态、定时触发、续话冷却、链式重试。
- 对外工具名尽量中性化，避免再出现 DFC/bridge 这种旧叙事。

## 这次优先做的事

- 在 `life_engine` 里补出 `schedule_followup_message` 兼容动作。
- 让该动作在底层复用 `proactive_message_plugin` 的现有调度实现。
- 把 `proactive_message_plugin` 内部日志和 README 里的 DFC 说法改成“当前运行模式 / 生命对话器 / 主动续话”。

## 暂时不动的部分

- 不重写主动续话的调度状态机。
- 不把群聊/私聊路由强行混成一条线。
- 不删除旧入口，先保留兼容，等稳定后再收口。

## 风险提示

- 如果直接把调度层并进 `life_engine`，很容易把现有等待任务、冷却链、触发锁搞乱。
- 所以当前阶段最稳的是“统一语义入口 + 保留执行层”。
