# 2026-11-06 Gemini 线路与 QQ 登录恢复

## 本次处置

用户明确授权修复不回消息的问题。已将 A6 Gemini 渠道 53 的上游从
https://api.a6api.vip 切到同服务公开公布的 https://cf.a6api.net，
保留 gemini-3.8-flash 模型、既有 A6 密钥、分组/优先级和应用入口。
约 9.8 万输入 token 的合成工具调用和结果回传均通过。

另查明 QQ 账号实际离线，尽管 NapCat WebSocket/API 仍存活。
已精确重启属于本用户的 host 网络 NapCat，保留票据挂载；
现有票据未能自动登录，已通过合法 QQLogin 接口取得二维码并由用户手机确认。
最终已核实 isLogin=true、online=true、账户摘要与配置一致；
01:15:08.216 Elysium 工程日志确认 NapCat WebSocket 已连接。未主动发测试消息，新的自然聊天投递仍待实际回执。

## 根因与之前诊断的补充

11 月 5 日 23:20 那次聊天确实先遇到 A6 HTTP/2 PROTOCOL_ERROR/EOF，
单个表达请求 90 秒超时，后续候选多次失败。原诊断检查截止 23:31。
但后续 23:34:18，真实 life_chatter 已通过 Gemini：
约 90094 输入 token、174 输出 token、365 条消息、10.47 秒，并产生一个发送工具调用。
因此不能把之后持续无回复都归因于模型仍失败。

23:34:44 的实际后续失败是 QQ 发送回执超时、投递状态未知：
NapCat 同刻原生 NodeIKernelMsgService/sendMsg 等待
NodeIKernelMsgListener/onMsgInfoListUpdate 超时。
本地未出现 API echo 等待超时，现有证据不支持把这次归因于回执 echo 丢失。

Nov6 处置前只读 API 实测：
- CheckLoginStatus：isLogin=false。
- GetQQLoginInfo：online=false。
- OneBot get_status：retcode=0、good=true，但 online=false。
- get_login_info 的账户摘要与 Elysium 配置账户一致。

故障包含两段：A6 真实请求的连接不稳定造成长延迟；随后 QQ 账号离线导致原生发送无法得到确认。
服务健康或小合成请求成功都不能单独证明实际聊天可用。

## 模型线路修复与验证

A6 官方公共 /api/status api_info 公布 CDN、CF 和媒体三个入口。
Spark 无认证测试确认 api.a6api.vip、cf.a6api.net、media.a6api.net 都能完成 HTTP/1.1 与 HTTP/2 的 TLS 校验；
短 GET 不能复现之前的大请求失败。媒体入口不作为此次文字主线路。
api.a6api.com 在 Spark 仍有 TLS reset，旧 a6api.com 曾返回 HTML。

没有为全部渠道关闭 HTTP/2，也没有引入新的代理后台进程。
当前 New API 没有渠道级 HTTP 版本开关；
service/http_client.go 的默认/代理 Transport 均启用 HTTP/2。
因此此次使用同供应商的已公布 CF 线路，限制修改范围为渠道 53 的 base_url 一列。

00:59:01.580，在在线一致备份及范围断言后事务性更新该列。
原 channel 30、欠费 channel 52 继续禁用，其他模型路由未改变。
当前渠道缓存和 Redis 未启用，更新后续请求直接生效，不需重启 New API 或 Elysium。

测试均使用合成工程文本和无外部副作用的 diagnostic_echo，不复制主体或聊天正文：

| 测试 | 结果 |
|---|---|
| Spark 直连 CF、HTTP/2、约 30 万字节请求 | HTTP 200，TLS 校验通过，98079 输入 token，42.17 秒，正确选择工具 |
| 同一大请求经本地 NexusAI 中转 | HTTP 200，98079 输入 token，6.799 秒，正确选择工具 |
| 大上下文工具结果回传 | HTTP 200，98175 输入 token，17.548 秒，正确返回 OK |

两条本地中转验收 request id：
- 202611051659534755026778268d9d6KNyFtUar
- 202611051700425118233608268d9d6692crtxt

New API 消费表均记录 channel_id=53、type=2。
上游响应 model 字段为 gemini-3.8-flash-n，应用请求模型仍为 gemini-3.8-flash，未做隐藏模型字段改写。
部分重复输入由上游缓存命中，不把 6.799 秒视为所有新请求的固定时延。

首轮验证脚本曾把正常的 tool_calls=null 当可迭代数组，导致验证器 TypeError，
不是模型/服务报错；已修正读取方式并单独重验工具结果，未因此修改生产代码。

## QQ 实例处置与授权边界

准确目标为本用户 host 网络容器 napcat：
6ba65dd5cbb5aae391128dc22ee9a82d48f3ac8a2f1eb701904cb203d6df5760。
原 init PID4066893，QQ 主进程 PID4067135/4067173，cwd /app/napcat；
WebUI6099、到 Elysium8087 的反向连接及账号摘要与目标一致。
另一用户的 bridge 容器 napcat-napcat-1 没有操作。

01:03:25 至 01:03:45 执行一次 docker restart --time 20，
新 init PID627241。保留 /home/ayerelysia/napcat/QQ 与 config 两个原挂载。
没有安装或修改自动恢复策略；Elysium PID2664093 未变，健康 HTTP200、alive=true、state=ready。

重启后账号仍未登录。通过现成 GetQQLoginQrcode API 读取当前二维码，
离线渲染交用户手机扫码，不向第三方发送登录内容。
首次二维码过期后，01:08:15 取得 NapCat 自动更新的新二维码，
确认内容已变化并使用独立图片文件名交付，未再次重启。
01:08:15 的缓存码未完成登录，随后等到 01:13:57.734 原生生成事件，立即交付真正新码。
用户手机确认后核实 isLogin=true、isOffline=false、online=true，账户摘要匹配；
OneBot get_status 为 status=ok、retcode=0、online=true、good=true。
旧 loginError 可能残留，不能在 online=true 时仍把它当当前登录失败。

旧投递状态 unknown 的回复未自动重发，避免潜在重复投递。
未发送人工 QQ 测试消息；未写主体文件、记忆、历史或会话。
另发现的双 WS owner race 没有本次触发证据，不扩大修改到该问题。

## 备份与回退

私有审计目录：
data/new_api_relay/backups/gemini-repair-20261106-005744/

其中 relay-before-cf.db 为 SQLite 在线一致备份，
cf-switch.json 为 base_url 的变更元数据，
cf-http2-probe.json、gateway-large-result-verified.json 为合成验收元数据，
napcat-restart-before.json / after.json 为准确实例操作记录。
目录 0700，备份与审计文件 0600，被 Git 忽略，不得提交或分享。
没有复制运行中的裸数据库文件，也不能将旧整库快照覆盖回线上。

A6 回退只在核对 channel53 身份与当前 CF 地址后，事务性恢复原 base_url；
不恢复账单、日志或其他渠道，不重新启用欠费 D1。
QQ 重启保留登录挂载，原 unknown 的事实与日志保持原样；凭据登录需要账号本人完成，
不能用伪造 token、修改 auth 数据库或重写会话绕开。

## 最终状态

- 模型线路：已切换并通过大请求/工具结果验收。
- QQ 登录：已确认 online=true，账户一致，01:15:08.216 反向 WS 重连；01:15:37 主服务健康正常。
- 真实后续聊天：须在账号恢复后以自然新请求和投递回执确认；
  本次不把健康接口、发送尝试或 unknown 当成已送达。
