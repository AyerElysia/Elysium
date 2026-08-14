# Feishu Adapter

把飞书自建应用接入 Elysium 的统一消息流。

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
[plugin]
enabled = true

[app]
app_id = "cli_xxx"
app_secret = "xxx"
verification_token = "xxx"
encrypt_key = ""

[connection]
subscription_mode = "long_connection"
auto_start_long_connection = true
long_connection_log_level = "WARNING"

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
9. 先执行 `./deploy.sh doctor`，通过后再由用户在可观察终端执行 `./deploy.sh run`。看到 `飞书长连接后台线程已启动` 后，再验收文本与图片。

长连接模式不需要公网域名，不需要回调地址，也不需要 frp/cloudflared/ngrok。
飞书 SDK 的瞬时断线由 SDK 自动恢复，正常的连接、断开和逐次重试日志会被聚合，
不会再打印带 `access_key` / `ticket` 的完整连接地址。持续连接失败仍会保留首条诊断，
相同错误最多每 5 分钟输出一次。

长连接健康由当前 Lark SDK 客户端自己的连接、帧收发和重连状态证明。没有新聊天消息
不代表连接失活；SDK ping/pong 仍属于有效传输活动。监控线程只会在唯一的长连接 owner
线程已经退出后创建替代客户端，不扫描或关闭进程内其他 HTTPS `CLOSE-WAIT` socket，
也不会从外部强停 SDK 事件循环。关闭适配器时，owner loop 先断开客户端、取消其任务并
正常返回，避免产生 `Event loop stopped before Future completed`。

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

本地调试可以用 frp、cloudflared 或 ngrok 把 Elysium 的 HTTP 端口映射到公网。

填写同一个 `Verification Token` 到 Elysium 配置，暂时不要启用 Encrypt Key。

## 本地冒烟测试

Elysium 启动后：

```bash
curl http://127.0.0.1:<Neo端口>/feishu/api/status
curl -X POST http://127.0.0.1:<Neo端口>/feishu/api/message \
  -H 'Content-Type: application/json' \
  -d '{"content":"爱莉爱莉","open_id":"local_user","sender_name":"本地飞书用户"}'
```
