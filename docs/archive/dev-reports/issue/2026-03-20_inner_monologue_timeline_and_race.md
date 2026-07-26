# 2026-03-20 内心独白调度抢跑与未触发问题报告

## 背景
- 用户反馈：
  - “内心独白的生命周期没有刷新，从首次独白开始”；
  - “最近一次也没有触发”；
  - 低延迟调试场景下设置 `first_check_minutes=0.1`，发现内心独白可能抢在正常回复前触发，或干脆不触发。
- 目标：确保新用户消息到来后，先完成本轮回复，再从首轮等待时间开始计时；避免调度任务被误删或被视为“已在等待”而不调度。

## 根因摘要
1) **异步取消误删新任务**
   - `_on_user_message()` 重置状态后立即重新 `start_waiting(force_overwrite=True)`，同时异步 `remove_schedule_by_name` 取消旧任务；若取消回调晚到，会把刚刚新建的同名任务删掉。

2) **计时起点放错（收到消息即计时，抢在回复前触发）**
   - 首轮计时在 `ON_MESSAGE_RECEIVED` 触发，`first_check_minutes` 很小时（如 0.1 分钟 ≈ 6 秒），内心独白会在默认回复完成前触发。

3) **状态默认值错误导致“已在等待”误判**
   - 新建 `StreamState` 的 `is_waiting` 默认值为 `True`，导致首次进入 `Wait` 时 `_start_waiting()` 直接返回，首轮等待从未调度。

## 采取的修复
- **防止误删新任务**：`service.on_user_message(cancel_task=False)`，只重置状态，不再异步取消同名任务；覆盖式调度依然通过 `force_overwrite=True` 完成。（@plugins/proactive_message_plugin/plugin.py#172-189）
- **正确的计时起点**：移除“收到消息即开始等待”，改为在 `ON_CHATTER_STEP` 捕获 `Wait` 时才启动首轮计时，确保正常回复先完成。（@plugins/proactive_message_plugin/plugin.py#172-236）
- **状态默认值修正**：`StreamState.is_waiting` 默认改为 `False`，避免首次进入等待被误判为“已在等待”。（@plugins/proactive_message_plugin/service.py#21-39）

## 复现与验证
- 调试配置：`first_check_minutes=0.1`、`min_wait_interval_minutes=0.1`、`max_wait_minutes=0.2`、`post_send_followup_minutes=0.1`（仅限测试场景）。
- 预期日志顺序：
  1) 用户消息 -> 默认回复发送成功；
  2) **开始等待计时**；
  3) **已调度检查任务：… 将在 0.1 分钟后检查**；
  4) 时间到 -> **检查超时，触发内心独白**；
  5) 若 LLM 决策 `wait_longer(X)` -> **继续等待：… 等待 X 分钟** 并重新调度同名任务（覆盖式）。
- 快速回归：从日志可见 12:14:33 后开始等待并 12:14:40 触发独白，符合预期；内心独白后 wait_longer 5 分钟也被重新调度。

## 注意事项
- 测试用极短时间配置仅用于验证；上线建议恢复较长等待值（如 `first_check_minutes=5.0`，`min_wait_interval_minutes>=5.0`，`post_send_followup_minutes>=10.0`）。
- 如果在极短时间窗口内多次用户消息（含表情包），首轮计时会不断刷新，这是预期行为。
- 关闭/重启时，未完成的内心独白请求可能被 Cancelled；属正常关停，重新启动即可。

## 关键文件
- `plugins/proactive_message_plugin/plugin.py`
- `plugins/proactive_message_plugin/service.py`
- `config/plugins/proactive_message_plugin/config.toml`

## 后续建议
- 如需进一步观测，可在 `_start_waiting` / `_on_check_timeout` 增加 stream_id 和 next_check_time 的 DEBUG 日志，以便更快定位计时链路。
