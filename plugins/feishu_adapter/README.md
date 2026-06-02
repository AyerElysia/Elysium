# Feishu Adapter

把飞书自建应用接入 Neo-MoFox 的统一消息流。

## 当前能力

- HTTP 事件回调：`POST /feishu/events`
- URL 验证：自动返回 `challenge`
- 文本消息入站：转为 `platform=feishu` 的标准消息并进入 CoreSink
- 文本消息出站：通过飞书 IM API 发回私聊或群聊
- 引用回复：存在 `reply_to` 时优先调用飞书 reply API
- 当前不支持加密回调；飞书后台请先不要启用 Encrypt Key

## 配置

配置文件会生成在：

```text
config/plugins/feishu_adapter/config.toml
```

需要填写：

```toml
[app]
app_id = "cli_xxx"
app_secret = "xxx"
verification_token = "xxx"
encrypt_key = ""

[bot]
bot_name = "爱莉"
bot_open_id = ""
```

## 飞书后台接入

1. 飞书开放平台创建“企业自建应用”。
2. 启用机器人能力。
3. 添加消息相关权限：接收消息事件、发送消息、回复消息。
4. 在“事件订阅”里配置请求地址：

```text
https://你的公网域名/feishu/events
```

本地调试可以用 frp、cloudflared 或 ngrok 把 Neo-MoFox 的 HTTP 端口映射到公网。

5. 填写同一个 `Verification Token` 到 Neo-MoFox 配置。
6. 暂时不要启用 Encrypt Key。
7. 订阅 `im.message.receive_v1` 事件。
8. 发布应用，并把机器人添加到飞书群或私聊。

## 本地冒烟测试

Neo-MoFox 启动后：

```bash
curl http://127.0.0.1:<Neo端口>/feishu/api/status
curl -X POST http://127.0.0.1:<Neo端口>/feishu/api/message \
  -H 'Content-Type: application/json' \
  -d '{"content":"爱莉爱莉","open_id":"local_user","sender_name":"本地飞书用户"}'
```

