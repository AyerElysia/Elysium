# proactive_message_plugin 时间整合实现报告

## 实现结果

这次把 `time_awareness_plugin` 的核心能力并入了 `proactive_message_plugin`，同时保留了旧插件的兼容占位，避免旧配置直接报错。

### 已完成内容

- 在主动插件里加入了动态时间块注入
- 新增 `query_time` 工具到主动插件
- 新增 `on_prompt_build` 时间注入器
- 给内心独白阶段注入了时间哲学 reminder
- 让内心独白可以调用 `send_emoji_meme`
- 保留并优化了 `wait_longer` 逻辑
- 将旧 `time_awareness_plugin` 降级为兼容层，不再注册 active 组件

## 主要行为变化

### 主模型可见的时间感

现在主模型每轮都能从 prompt 里看到：

- 当前时间
- 距离上次用户消息多久
- 当前处于哪个时间阶段
- 距离上次主动发言多久
- 主观等待压强

### 内心独白可见的时间感

内心独白阶段会看到：

- 稳定的时间哲学 reminder
- 动态时间块
- 历史独白连续性

并且可以根据情绪选择：

- `send_text`
- `send_emoji_meme`
- `wait_longer`

### 兼容策略

旧的 `time_awareness_plugin` 现在只保留兼容壳：

- 不再注册 `query_time`
- 不再注册时间追踪事件
- 不再注入旧的静态时间 reminder
- README 已指向新实现

## 验证情况

### 通过项

- `python -m py_compile` 通过
- 定向 pytest 通过：`7 passed`

### 测试覆盖

- 时间阶段与时间块构建
- `query_time` 工具
- 动态 prompt 注入
- 旧时间插件兼容降级
- 内心独白工具集里已暴露 `send_emoji_meme`

## 风险与说明

- 当前时间状态仍以内存为主，重启后会清空
- 旧插件保留兼容壳，但不再承担实际功能
- 默认 pytest 配置里带有 coverage addopts，本次验证使用了 `--override-ini addopts=''`

## 相关文件

- `plugins/proactive_message_plugin/plugin.py`
- `plugins/proactive_message_plugin/service.py`
- `plugins/proactive_message_plugin/inner_monologue.py`
- `plugins/proactive_message_plugin/tools/query_time.py`
- `plugins/proactive_message_plugin/temporal.py`
- `plugins/time_awareness_plugin/plugin.py`
- `plugins/time_awareness_plugin/README.md`
- `plugins/time_awareness_plugin/tools/query_time.py`
