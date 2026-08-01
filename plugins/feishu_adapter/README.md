# Feishu Adapter

把飞书自建应用接入 Neo-MoFox 的统一消息流。

## 当前能力

- 长连接事件订阅：Neo 主动连接飞书开放平台，不需要公网域名
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

[connection]
subscription_mode = "long_connection"
auto_start_long_connection = true

[bot]
bot_name = "爱莉"
bot_open_id = ""
```

## 飞书后台接入：长连接

1. 飞书开放平台创建“企业自建应用”。
2. 启用机器人能力。
3. 在“权限管理”中按当前功能开通应用身份权限：
   - 私聊收消息：`im:message.p2p_msg:readonly`
   - 机器人发送/回复消息：`im:message:send_as_bot`
   - 读取消息及下载收到的图片资源：优先使用 `im:message:readonly`
   - 如需群聊 @ 消息：另开 `im:message.group_at_msg:readonly`（当前群聊未验收）
   - 如需从通讯录解析真实昵称：可选 `contact:user.base:readonly`
4. 图片资源接口若返回 `99991672 Access denied`，飞书错误信息允许 `im:message:readonly`、`im:message.history:readonly`、`im:message` 三者之一；本项目建议最小只读权限 `im:message:readonly`，不要为排障直接授予更宽的 `im:message`。
5. 在“事件与回调”里把订阅方式设置为“长连接”。
6. 添加事件：

```text
im.message.receive_v1
```

7. 权限或事件变更后创建新版本，提交管理员审批并发布；只保存权限配置但不发布，运行中的应用不会获得新权限。
8. 确认应用可用范围包含测试用户，并把机器人添加到飞书私聊或目标群。
9. 启动 Elysium。看到 `飞书长连接后台线程已启动` / SDK 连接日志后，再验收文本与图片。

长连接模式不需要公网域名，不需要回调地址，也不需要 frp/cloudflared/ngrok。

## 可选：HTTP 回调接入

如果你有公网 HTTPS 域名，也可以设置：

```toml
[connection]
subscription_mode = "http_callback"
```

然后在“事件订阅”里配置请求地址：

```text
https://你的公网域名/feishu/events
```

本地调试可以用 frp、cloudflared 或 ngrok 把 Neo-MoFox 的 HTTP 端口映射到公网。

填写同一个 `Verification Token` 到 Neo-MoFox 配置，暂时不要启用 Encrypt Key。

## 本地冒烟测试

Neo-MoFox 启动后：

```bash
curl http://127.0.0.1:<Neo端口>/feishu/api/status
curl -X POST http://127.0.0.1:<Neo端口>/feishu/api/message \
  -H 'Content-Type: application/json' \
  -d '{"content":"爱莉爱莉","open_id":"local_user","sender_name":"本地飞书用户"}'
```
