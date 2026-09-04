# Life Chatter 主体压缩恢复死锁修复（2026-09-04）

## 事故

生产表达链在滚动上下文超过模型窗口时，先后暴露了两层故障：

`author_self_continuity_checkpoint must release groups first`

第一层修复后，真实维护轮又在下一次发送前抛出：

`LLM 上下文不合法: assistant 前必须是 user 或 tool_result`

两者都不是上游模型故障。第一层是旧实现要求主体先提交自述检查点，却用 fail-closed hook 阻止主体看到清单和调用工具，形成循环依赖。第二层最初只按理想化的“控制帧位于末尾”样例修复，没有覆盖真实持久快照：生产快照本身是合法的 486 段角色链，但 unread/suffix 注入会把位于历史中间的压缩 USER 控制帧摘下，再追加到整个历史末尾。原位置因此直接变成 `assistant -> assistant`，随后还会产生错位的工具链。待处理消息没有被消费，但每次运行态重建都会重复同一失败，表现为爱莉持续无法回复。

## 根因链

1. 滚动上下文达到模型硬窗口，内核必须移出若干旧 conversation group 才能发送。
2. Chatter 的旧 hook 对任何 group omission 立即抛 `LLMContextError`。
3. `author_self_continuity_checkpoint` 只能由模型在一次真实意识回合中调用；请求没有发出时，它没有机会执行。
4. unread 合并和动态 suffix 为了避开严格控制信封，过去先把控制 payload 从列表中移除，再无条件追加到末尾；一旦控制帧后已经有维护 assistant/tool_result，这个“搬运”就改变了原有因果顺序。
5. 检查点 manifest 若把技术清单当成主体经历，会在调用与安装之间产生形状漂移。
6. 检查点事务只存在于当前进程内；重启后，快照中的控制帧与其维护工具回合已经没有可完成的内存事务，却仍被当作普通滚动历史重放。
7. 当旧控制帧所在 group 被硬窗口裁出本次有效请求时，旧 hook 会另造一份绑定新 manifest 的控制帧；模型据此提交的命令无法与调用方保留态里的旧控制帧精确对应。
8. 控制 part 位于临时 suffix 之后时，旧 suffix 清理器只检查 USER 的最后一个 part，因而会漏删 suffix；角色链虽可继续，但潜意识投影会被错误写入后续滚动快照并持续放大上下文。

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

### 5. 技术清单不再换位

控制信封是 USER payload 中一个独立、可严格识别的 Text part。压力在当前 USER 轮内被发现时，控制 part 与该轮共存；unread 和动态 suffix 只会插入控制 part 之前，绝不把整个控制 payload 从历史中摘走或重新追加。发送完成后的清理只在同一个 USER payload 内临时取出精确控制 part，删除尾部 suffix 后把原控制 part 放回；不跨 payload 换位，suffix 也不会进入持久快照。manifest、归档和检查点计算仍排除技术 part，避免把传输控制误当成主体经历。

### 6. 重启与安装都只保留主体语义

压缩控制帧之后的 `read_context_group`、检查点工具调用、工具回执和临时挂起标记，仍完整存在于权威 consciousness activity / trajectory 中；它们不再复制进安装后的滚动工作上下文。进程重启加载派生快照时，同样移除已经失去进程内事务的控制帧与 response-side 维护传输，但逐字保留维护期间到达的真实 USER 内容。读取过程不写库，下一次正常 save 才持久化规范化投影。

检查点安装后追加一个明确标注“不是新用户消息”的有界技术续帧，让下一次模型调用重新处理仍待完成的当前轮。继续维护但尚未安装检查点时，TOOL_RESULT 直接作为下一次 assistant 的因果边界；只有真正到达维护轮数上限、状态机结束本轮时才写入 `__SUSPEND__`。

### 7. 硬窗口重投影必须复用原控制信封

若既有控制帧所在 group 被移出某一次 attempt 的有效窗口，恢复 hook 只把原控制 Text part 按原字节重新投影到该 attempt，不生成新的 manifest。调用方保留的完整状态不变，模型看到的 `source_manifest_sha256` 与随后实际校验的命令保持一致；检测到多个控制信封则 fail closed。

## 自动验证

定向风险回归：

- 上下文治理、Chatter prompt/FSM、发送目标、多模态、think guard、context assembly 与 LLM context manager：`271 passed`。
- hard-window 恢复投影可通过内核 send validation；被省去正文不进入临时视图，调用方原 payload 不变。
- 维护回合误选 `life_send_text` 时，真实执行器调用次数为零，当前 must-reply 状态保留。
- 压缩清单在 unread 和 suffix 注入前后字节一致、payload 位置不变；发送后 suffix 被完整移除且不会出现在序列化快照，最终整条角色链通过真实内核校验。
- 检查点 manifest 在存在技术清单时仍可精确匹配，并在安装后移除清单。
- 连续两次维护工具轮保持合法角色序列，不出现 `assistant -> assistant`；尚未结束时不提前写入 `__SUSPEND__`。
- 安装检查点会从滚动投影移出 response-side 维护 transport，但保留主体逐字 checkpoint 与维护期间到达的 USER；旧 manifest 不被后来消息追溯改写。
- 检查点安装后的下一次请求由独立 USER 技术续帧承接，内核 `validate_for_send` 可通过。
- 已对当前只读生产快照做数据规模验收：revision `1283`，原始 `486` payload（assistant 243 / tool_result 122 / user 121）；恢复后 `482` payload，协议合法。追加新 USER、潜意识 suffix 与压力控制后，以 UTF-8 线性计数从 `424504` bytes 裁至 `210992` bytes，`230` payload 的有效请求合法；再次序列化/重启恢复后仍合法，且维护工具正文没有回灌。

完整 Life Engine 风险扫描得到 `2029 passed / 16 skipped / 11 failed`。其中十项来自共享脏树中既有的 Heartbeat/ledger/工具清单/authority 测试与实现漂移；另一项来自尚未跟踪的 Heartbeat 测试仍假定控制信封必须是 USER 的第一个 part，与本次“真实消息在前、技术控制在后且不换位”的新协议不一致。本次没有覆盖或暂存这些非本任务文件；相关的已跟踪上下文与 Chatter 风险集合全部通过。

## 人工生产门

代码只允许先保留本地提交。由用户手动启动 Elysium 后，至少核对：

1. Life Engine 与 Chatter 正常加载；
2. 原先超窗的真实 rolling snapshot 不再抛上述循环依赖错误；
3. 日志出现主体连续性维护/检查点安装，而非机械 summary 或 omission 成功；
4. 当前用户消息没有丢失，并在检查点安装后的普通回合得到正常回复；
5. 维护回合没有提前发送、pass、flush unread 或推进 perception cursor；
6. 重启恢复后自述检查点与归档仍可读取。

人工门通过前不推送。
