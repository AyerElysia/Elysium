# Console Go 缺少 `x-opencode-session`（2026-09-07）

## 结论

23:16 的 `life_learning_reflection` 400 不是反思内容坏了，也不是密钥错了。请求经本地 New API（`localhost:3000`）转到 **OpenCode Console Go**。该通道从约 **2026-09-06** 起要求 HTTP 头 `x-opencode-session` 做会话亲和路由。Elysium 的 OpenAI 客户端**从不发送**这个头；仓库里没有 `x-opencode-session` / `MissingSessionID`。上游返回：

```text
type=MissingSessionID
Request is missing x-opencode-session and cannot be routed efficiently
```

同晚 `mimo-v2.5` 的 `router` 请求也是同一错误。会落到 Console Go 的 MiMo 首跳因此必败；重试链改走 gemini / gpt-5.6-* 后，聊天和路由往往仍能完成。学习反思会从 `mimo-v2.5-pro` 掉到 `gpt-5.6-terra` 再 `gpt-5.6-sol`。

## 证据

- 23:00:58、23:01:07、23:01:18 `request=router` `model=mimo-v2.5` 同一 400，然后 `next_model=gemini-3.7-flash`
- 23:03:41 `request=life_learning_reflection` `model=mimo-v2.5-pro` 同一 400，然后 `next_model=gpt-5.6-terra`
- 23:05:05 terra 也失败，再切 `gpt-5.6-sol`
- `src/kernel/llm/model_client/openai_client.py` 无 `extra_headers`；未知 extra 进 `extra_body`，不会变成 HTTP 头

公开对照：OpenCode Go 从 Sep 6 起要求该头；缺少时部分模型 400（错误文案可能是 MissingSessionID 或 Model is unavailable）。

## 待决（操作者）

未改代码、未改 `models.toml`、未重启。可选修复：

1. New API 在转发 Console Go 时注入稳定 `x-opencode-session`（中转站知道上游是谁，最合适）。
2. Elysium 对走该通道的请求带不透明会话头（按 request/stream 稳定，不泄露到无关上游）。

不要用这个错误当认知失败或关掉学习环。
