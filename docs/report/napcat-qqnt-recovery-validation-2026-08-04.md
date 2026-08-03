# NapCat / QQNT 掉线恢复与真实链路验证（2026-08-04）

> 状态：一次真实私聊收发已通过；长期稳定性仍在观察。

## 1. 事件摘要

WSL 部署曾出现两类现象：

- NapCat 反向 WebSocket 意外关闭，随后连接 `127.0.0.1:<OneBot端口>` 被拒绝；
- OneBot 传输层仍可访问，但机器人长时间收不到新的 QQ 消息。

第二类现象不能只靠 WebSocket 已连接或 `get_status.good=true` 排除。QQNT 会话可能已失去真实 QQ 网络连接，而 NapCat 的本地 OneBot 服务仍然存活。

## 2. 本次恢复基线

本次保留 NapCat `4.18.13`，将 Linux QQNT 从 `3.2.25-45758` 回退到 `3.2.23-44343`。使用的 amd64 安装包经过双重校验：

- MD5：`94704804d75a5fac38fb7f752abd039a`
- SHA-256：`a4252719c1beb8adce0da09ebfc310ce50c79ea548f5cce429505765d0bfba84`

变更前完整保留 QQ 会话目录、NapCat 配置以及旧 QQNT 运行目录；生产目录通过同文件系统改名完成切换，未创建 systemd、计划任务、登录启动项或其他自动拉起机制。

## 3. 验收证据

2026-08-04 07:47（Asia/Shanghai）完成一次真实 QQ 私聊端到端验收，且未在文档中记录消息正文：

1. QQNT 保持真实 QQ 服务端 TCP 连接；
2. NapCat `get_status` 返回 `online=true`、`good=true`；
3. NapCat 与 Elysium 的反向 WebSocket 连接已建立；
4. 新消息以 `source=qq`、`channel=chat`、`event_type=text` 写入 `raw_life_events`；
5. 同一消息写入 Elysium `messages`，且文本预处理完成；
6. 同一会话随后产生并执行 `action_life_send_text_*` 回复动作。

因此，本次结论是“真实私聊收发链路已恢复”，而不只是“端口或状态接口可访问”。一次成功验收不能证明长时间运行问题已经根治，仍需继续观察是否再次出现 `KickedOffline`、QQ 外部连接消失或入站事件停滞。

## 4. 排查顺序

遇到“爱莉突然收不到 QQ 消息”时，按以下顺序判断，不要把任一单项当作完整健康证明：

1. 确认只有一个 QQ/NapCat 实例，避免重复启动同一账号；
2. 检查 QQNT 是否仍有真实 QQ 服务端连接；
3. 检查 NapCat OneBot API 的 `online` 与 `good`；
4. 检查 NapCat 到 Elysium 的反向 WebSocket；
5. 发送一条新的真实私聊消息，确认它进入 `raw_life_events`；
6. 确认消息进入统一消息库，并产生对应回复动作。

如果第 2 项失败而第 3、4 项仍正常，应优先判断为 QQNT 会话失活或被踢下线，不应归因于 LLM 超时。LLM 超时会影响思考与回复，但不会解释 QQ 入站事件本身完全消失。

## 5. 重启与回滚边界

- 当前部署只允许用户手动启动 Elysium 与 NapCat；维护脚本和健康检查不得擅自拉起进程。
- 已运行实例健康时不要再次执行 QQNT 启动命令；重复实例会争用同一账号、会话数据和端口。
- 需要重启时，先确认并优雅停止现有 QQ/NapCat 进程，再启动一个实例。
- 回滚时恢复完整旧 QQNT 目录以及配套会话/配置备份，不要只覆盖部分 Electron 资源。
- 任何自动恢复设计都必须使用持续、复合证据，并保留人工授权边界；单次 `online=false` 或单次心跳异常不足以触发自动重启。

## 6. 后续观察

至少持续记录以下信号：

- `KickedOffline` 或登录态变化；
- QQNT 外部连接是否消失；
- OneBot 反向 WebSocket 重连次数；
- 最新 QQ 入站事件时间与消费进度；
- 同一会话回复动作是否成功落账。

若问题再次出现，应先保存上述证据和对应时间窗，再决定继续固定 QQNT `3.2.23-44343`、升级到新的已验证组合，或改造 Elysium 的 NapCat 复合健康检测。
