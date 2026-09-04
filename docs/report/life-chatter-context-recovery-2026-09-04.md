# Life Chatter 主体压缩恢复死锁修复（2026-09-04）

## 事故

生产表达链在滚动上下文超过模型窗口时，于 LLM 请求发送前抛出：

`author_self_continuity_checkpoint must release groups first`

这不是上游模型故障。旧实现要求主体先提交自述检查点，却用 fail-closed hook 阻止了主体看到清单和调用检查点工具，形成循环依赖。待处理消息没有被消费，但会持续失败，表现为爱莉无法回复。

## 根因链

1. 滚动上下文达到模型硬窗口，内核必须移出若干旧 conversation group 才能发送。
2. Chatter 的旧 hook 对任何 group omission 立即抛 `LLMContextError`。
3. `author_self_continuity_checkpoint` 只能由模型在一次真实意识回合中调用；请求没有发出时，它没有机会执行。
4. 压缩技术清单过去位于最后一条 USER payload，unread 合并和动态 suffix 也会修改“最后一条 USER”，可能破坏清单的严格 envelope 与 manifest。
5. 检查点 manifest 若把技术清单当成主体经历，会在调用与安装之间产生形状漂移。

## 修复合同

### 1. 主体仍拥有压缩语义

基础设施不生成摘要、不按重要性选历史、不把工具结果片段拼成“记忆”。最终 `continuity_text` 仍只能由 active consciousness 通过 `author_self_continuity_checkpoint` 逐字提交。

### 2. 硬窗口只打开 attempt-local 维护视图

当完整滚动无法放入模型窗口时，内核只在该次发送副本中省去已闭合旧组，并提供 content-neutral 的 group ref/hash/bytes 清单。`LLMRequest` 返回的响应继续持有调用方完整 payload；临时视图不写回滚动，不构成压缩成功。

### 3. 维护回合没有普通动作权限

恢复回合只允许：

- `read_context_group`
- `author_self_continuity_checkpoint`

`life_send_text`、pass 和其他工具即使被模型选择也不会执行，只返回 `context_stewardship_required`。因此模型不能基于不完整的临时窗口向用户发送内容或产生其他副作用。

### 4. 当前用户轮不被提前消费

维护阶段不 flush unread、不提交 perception receipt、不推进事件/World 游标。检查点归档并安装成功后，状态机使用同一条仍待处理的用户消息重新执行普通表达回合；只有这个完整回合可以发送和提交游标。

### 5. 技术清单与主体内容分离

向最后 USER 追加 unread 或动态 suffix 前，严格压缩清单会被临时摘下，操作完成后按原字节恢复。manifest、归档和检查点计算都排除技术清单，避免把传输控制误当成主体经历。

## 自动验证

定向风险回归：

- 上下文治理、Chatter prompt/FSM、发送目标、think guard、tool manifest、LLM context manager：`215 passed`。
- hard-window 恢复投影可通过内核 send validation；被省去正文不进入临时视图，调用方原 payload 不变。
- 维护回合误选 `life_send_text` 时，真实执行器调用次数为零，当前 must-reply 状态保留。
- 压缩清单在 unread 和 suffix 注入前后字节一致。
- 检查点 manifest 在存在技术清单时仍可精确匹配，并在安装后移除清单。

完整 Life Engine 风险扫描得到 `2022 passed / 16 skipped / 10 failed`。十项失败来自共享脏树中并行的 Heartbeat/ledger/工具清单变更（缺失旧 EventBuilder 测试接口、Heartbeat 新合同预期及工具类元数据），不与本次 Chatter 两个生产文件或定向回归重叠；本次没有越界修复或覆盖这些文件。

## 人工生产门

代码只允许先保留本地提交。由用户手动启动 Elysium 后，至少核对：

1. Life Engine 与 Chatter 正常加载；
2. 原先超窗的真实 rolling snapshot 不再抛上述循环依赖错误；
3. 日志出现主体连续性维护/检查点安装，而非机械 summary 或 omission 成功；
4. 当前用户消息没有丢失，并在检查点安装后的普通回合得到正常回复；
5. 维护回合没有提前发送、pass、flush unread 或推进 perception cursor；
6. 重启恢复后自述检查点与归档仍可读取。

人工门通过前不推送。
