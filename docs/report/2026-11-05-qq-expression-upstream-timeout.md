# 2026-11-05 23:20 QQ 表达链无回复诊断

## 结论

截至原检查时点 23:31，23:20:25 的 QQ 消息已接收，前置 Router 放行后表达链多次失败，尚未进入发送阶段；首个请求在 A6 渠道 53 发生 HTTP/2 PROTOCOL_ERROR 与 EOF，之后候选失败、冷却和重试。后续核对发现 23:34 已有 Gemini 真实成功，随后 QQ 原生发送回执超时，账号又被确认离线；因此持续无回复还涉及 QQ 登录，完整恢复见 2026-11-06-gemini-qq-recovery.md。

这不是“响应 True 就已经回复”：该字段仅表示前置路由允许主体表达链继续。
18 点的 A6 合成文本/工具调用成功只证明当时的基本中转链路，不能代替本次真实聊天验收。
原检查未见工具 schema、上下文长度或 SSE 设置错误；QQ 发送阶段的证据是后续 23:34 才出现，不能因早期未进入发送就排除之后的发送故障。

## 时间线与来源

日期为 2026-11-05，时间均为 Asia/Shanghai。

应用日志：logs/elysium-2026-11-05.log。

| 时间 | 工程证据 |
|---|---|
| 23:20:25.111 | QQ 收到用户消息；不在本文复制聊天正文 |
| 23:20:34.815 | 独立 life_engine 唤醒上下文 count=22、drained=1、high_water=159905、task=core |
| 23:20:34.880 | life_chatter 前置 Router 停用，放行到主体表达链 |
| 23:22:05.155 | life_chatter 的 Gemini 超时，转 DeepSeek |
| 23:23:05.813 | DeepSeek LLMAPIError，转 GPT 6.1 |
| 23:24:35.883 | GPT 6.1 超时，转 GPT 6 Sol |
| 23:25:29 | GPT 6 Sol / GPT 6 Luna 超时，候选耗尽，Chatter Failure |
| 23:25:34–23:25:56 | 驱动约每五秒重试，遇到 all candidate models are cooling down |
| 23:26:01 | 冷却到期后再次进入模型链，选 GPT 6 Sol |
| 23:27:31 | GPT 6 Sol 超时；GPT 6 Luna 返回 503 model_not_found，无可用渠道 |
| 23:27:36 | 表达链再次重试 |
| 23:29:07 | Gemini 再次约 90 秒超时，继续故障转移 |

New API 网关文件：
data/new_api_relay/logs/oneapi-20261102202925.log。

聊天 request id：202611051520352256488018268d9d6lUgiHSh0。

- 23:20:35 启动，使用渠道 53 A6API-Gemini。
- 23:21:37：渠道 53 返回 500，技术错误为 HTTP/2 stream error、stream ID 1、
  PROTOCOL_ERROR、received from peer。
- 23:22:08、23:22:38：两次中转重试均为 EOF，并返回 500。
- 23:22:38：整个网关请求以 HTTP 500 结束，耗时 123.304713946 秒。
- 未见该 request id 晚到 200 或 HTTP 499 的证据；不能解释为“上游已成功但回复被客户端丢弃”。
- 未见该请求的 400、INVALID_ARGUMENT、工具 schema、HTML 或上下文长度错误标记。
- 错误里的 stream 指 HTTP/2 传输流，不等同于开启模型 SSE。

## 独立心跳与流式差异核对

同期另一个请求 202611051523349079966158268d9d6i6Sj1Bnx 在渠道 53 成功：
23:23:34 启动、23:24:09 HTTP 200，耗时 35.040884097 秒；
元数据记录输入 83963 token、输出 152 token、is_stream=0。
它对应独立 core 心跳，不是上述聊天请求。

23:24:12 的心跳退出原因是 self_pause_terminal（模型成功后主动休息），
不能误记为心跳预算耗尽，也不能把它归为聊天未回复的原因。
本文不复制或改写主体休息理由等主观语义。

两条链都显式非流式：
plugins/life_engine/core/chatter.py:6446、plugins/life_engine/service/core.py:11043，
心跳续轮 :11315、:11335 亦为 stream=False。
底层使用 stream or force_stream_mode（src/kernel/llm/request.py:819）；
现行配置未声明后者，schema 默认 False（src/core/config/model_config.py:148）。
因此没有“聊天默认流式而心跳非流式”的源码依据。

## 超时、重试与未读保留

config/models.toml 的 tasks.expression.attempt_timeout_seconds=90；
整个 Chatter 故障转移链还受约 295 秒预算限制（stream_step_timeout=300，留 5 秒回滚余量）。
一次网关内部重试耗时约 123 秒，已超过单个表达模型的 90 秒等待上限；
但其最终确实是 500，仅加长客户端超时并不能保证修复连接错误。

相关源码：

- chatter.py:4190：Router 停用时放行。
- chatter.py:6451：等待模型及候选故障转移链。
- chatter.py:6556、:4862：失败回滚临时上下文、回到 WAIT_USER 并释放 owner。
- chatter.py:6631：仅模型成功后提交 unread flush。
- chatter.py:6646：成功后才转入 TOOL_EXEC。
- src/core/transport/distribution/loop.py:298：Failure 后等待下一 Tick。
- chatter.py:3535、:3595 与 src/kernel/llm/attempt_budget.py:26、:48：整体与单次尝试预算。

后续持续重试的日志与上述代码表明 owner 没有永久锁死，待处理工作仍被保留；
不能把 life_engine 心跳的 drained=1 直接理解为聊天 unread 已成功回复并消费。

## 已执行操作、边界与剩余事项

只读核对进程、健康、现行配置、应用日志、中转文件日志及 SQLite 模型消费元数据；
没有新模型测试请求、没有 QQ 测试发送，没有修改模型/渠道、主体文件、历史或会话，
也没有重启或停止任何进程。仅新增此工程记录，因此无生产状态回滚。

现役 Python PID 2664093 持续运行；
/api/v1/health 实测 HTTP 200、alive=true、state=ready，只证明存活。
渠道 53 仍启用，52/30 仍禁用，A6 路由没有退回欠费 D1。
其他渠道问题（例如 GPT 6 Luna 无可用渠道）独立记录，不能冒充 Gemini 的协议错误根因。

原检查时点尚未恢复，不能把当时的健康接口当作修复完成。后续中转线路与 QQ 登录处置及其验收边界已记录在 2026-11-06-gemini-qq-recovery.md；
不通过删历史、缩写主体正文、强迫回复或伪造成功绕开故障。
当前未找到可直接用于此渠道的现成 HTTP/1 专用配置项，不在本次诊断中盲改全局传输协议。
