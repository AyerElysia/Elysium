# 2026-03-23 proactive_message_plugin 延迟续话改造报告（DFC 接管版）

## 1. 目标与结论

本次改造的核心目标是：

- 延迟续话到时后，不再由 `proactive_message_plugin` 自己跑一轮“续话内心独白判断”
- 而是把这次机会交回 `default_chatter`（DFC）主链路，让 DFC 按正常动作流决定是否继续说

已完成并验证通过。当前行为是：

- 模型通过 `action-schedule_followup_message` 登记续话机会
- 到点后插件注入一条“续话机会触发消息”到 unread，并解除流等待锁
- DFC 正常接手该轮处理，决定 `send_text / pass_and_wait / 再次 schedule_followup_message`

也就是：延迟续话本质已变成一次 DFC 主动决策机会，而不是插件私有分叉推理。

---

## 2. 架构变化（相对旧实现）

### 2.1 移除旧路径

移除了“延迟续话专用 prompt + 本地续话决策函数”这条路径，不再出现：

- 插件内部单独判断“补一句/取消”
- 延迟续话阶段再次运行插件私有思考链

### 2.2 新路径（当前）

1. DFC 在正常回复中调用 `action-schedule_followup_message`
2. `proactive_message_plugin` 记录 `PendingFollowup` 并调度定时任务
3. 到时后执行 `_on_followup_timeout`：
   - 校验任务有效性（避免过期任务误触发）
   - 标记本轮 followup trigger 活跃
   - 注入 synthetic unread（“续话机会”系统消息）
   - 清除 `StreamLoopManager._wait_states[stream_id]`，让 DFC 能继续跑
4. DFC 消费该 unread，按常规动作流出招
5. 插件通过 `ON_MESSAGE_SENT` + `ON_CHATTER_STEP_RESULT` 回收本轮状态、统计链计数、冷却控制

---

## 3. 主要改动文件

### 3.1 `plugins/proactive_message_plugin/plugin.py`

- 新增/完善延迟续话调度入口：`schedule_followup_for_stream`
- 新增/完善延迟续话回调：`_on_followup_timeout`
- 新增 DFC 唤醒实现：`_wake_stream_for_followup`
- 通过 `ON_MESSAGE_SENT` 记录本轮是否真实发出了消息，并在 followup trigger 中更新链计数
- 在 `ON_CHATTER_STEP_RESULT` 中结束 trigger 轮次，执行冷却与下一轮普通等待调度
- 清理了无用导入与旧残留变量，避免后续维护误导

### 3.2 `plugins/proactive_message_plugin/service.py`

- 扩展状态结构 `StreamState`：
  - `pending_followup`
  - `followup_chain_count`
  - `followup_cooldown_until`
  - `followup_trigger_active`
  - `followup_trigger_sent_message`
  - `active_check_kind`
- 新增方法：
  - `start_followup_wait`
  - `mark_followup_trigger_active`
  - `mark_followup_trigger_sent`
  - `clear_followup_trigger`
  - `enter_followup_cooldown`
  - `is_followup_cooldown_active`
  - `prepare_post_send_state`

### 3.3 `plugins/proactive_message_plugin/actions/schedule_followup_message.py`

- 新增动作 `schedule_followup_message`
- 作用是“登记稍后再判断”，不是“立刻发第二条”

### 3.4 配置与清单

- `plugins/proactive_message_plugin/config.py`
- `config/plugins/proactive_message_plugin/config.toml`
- `plugins/proactive_message_plugin/manifest.json`

已包含 followup 相关配置项和组件注册。

### 3.5 文档

- `plugins/proactive_message_plugin/README.md`
- `report/2026-03-23_proactive_message_plugin_followup_chain_report.md`（本文件）

README 已明确说明：延迟续话由 DFC 接管，不再走插件本地续话 prompt。

---

## 4. 稳定性与防误触发设计

为避免“任务已过期却被执行”，超时入口增加了状态守卫：

- `state.is_waiting == True`
- `state.active_check_kind` 与当前任务类型匹配
- `pending_followup` 存在性检查（针对 followup）

这可以防止：

- 用户已回复后的旧任务误触发
- 普通沉默检查和 followup 检查串台
- 调度覆盖后旧 callback 迟到执行

---

## 5. 验证结果

### 5.1 语法编译

执行：

```bash
python -m py_compile \
  /root/Elysia/Neo-MoFox/plugins/proactive_message_plugin/plugin.py \
  /root/Elysia/Neo-MoFox/plugins/proactive_message_plugin/service.py \
  /root/Elysia/Neo-MoFox/plugins/proactive_message_plugin/inner_monologue.py \
  /root/Elysia/Neo-MoFox/plugins/proactive_message_plugin/actions/schedule_followup_message.py \
  /root/Elysia/Neo-MoFox/plugins/proactive_message_plugin/config.py
```

结果：通过（无语法错误）。

### 5.2 单元测试

执行：

```bash
pytest -q -o addopts='' /root/Elysia/Neo-MoFox/test/plugins/test_proactive_message_service.py
```

结果：

- `3 passed`

覆盖点包括：

- 发送后状态清理不丢失最近 Bot 消息摘要
- 用户新消息会清空 followup 链状态
- `mark_followup_trigger_sent` 同轮只计数一次

---

## 6. 现在你应该看到的日志特征

当触发延迟续话时，关键日志应类似：

1. `已登记延迟续话 ...`
2. `延迟续话到时，唤醒 DFC 自主判断是否继续说`
3. `已注入 DFC 续话机会触发消息 ...`

并且不应再出现“延迟续话内心独白决策（插件本地）”这类旧路径日志。

---

## 7. 当前边界

- 续话是否真正发出，最终取决于 DFC 当轮决策（这是设计目标）
- 如果需要更强“续话倾向”，建议调 prompt 与阈值，不建议回退到插件本地硬判断

---

## 8. 总结

这次改造把“延迟续话”从插件私有分叉逻辑，收敛为 DFC 主链路上的一次标准主动机会。  
结果是：行为更统一、上下文更一致、后续维护成本更低，也更符合你要求的“本质走 DFC”。

