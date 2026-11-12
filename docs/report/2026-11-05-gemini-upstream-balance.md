# 2026-11-05 Elysium Gemini 故障与 A6 切换验收

## 2026-11-05 的故障与切换记录

本节记录当日的切换与验收时点；后续 CF 线路切换、真实表达与 QQ 登录恢复见同目录 2026-11-06-gemini-qq-recovery.md，不能将这里的阶段状态当作当前验收结果。

本次 Gemini 不可用的根因是原 D1API 上游账号余额不足：
渠道 52 D1API-Gemini 从 2026-11-04 16:58:26（Asia/Shanghai）起返回 HTTP 403，Insufficient account balance。

用户随后明确要求 Gemini 改走 A6。2026-11-05 18:19:07 已切换到新渠道 53 A6API-Gemini，
仅启用 gemini-3.8-flash，使用经 A6 公共接口公布且在 Spark 验证成功的 https://api.a6api.vip。
本地中转的两轮合成工具调用已通过，消费记录确认两轮均命中渠道 53。
Elysium 原 PID 2664093 持续运行，健康接口 HTTP 200、alive=true、state=ready；没有重启或重建会话。

截至 18:20:40，Elysium 当日日志末条仍为 16:56:06，切换后没有自然 Gemini 请求。
因此已验收的是 A6 文本与中转工具链路，真实聊天/心跳自然恢复仍须以之后的应用日志确认。

## 诊断证据与时间线

- 仓库为 Spark /home/ayerelysia/Elysia/Elysium，HEAD aff462d9。
  Python main.py 实例 PID 2664093、PPID 1832819，由既有 tmux 会话启动。
  systemd elysium.service 显示 inactive，不能据此误判 Python 实例未运行。
- config/models.toml 的 Gemini 继续使用 NexusAI http://localhost:3000/v1；
  expression 等任务的 Gemini 候选、model id、session、历史和主体文件均未修改。
- 诊断时 /v1/models 返回 200 且列出 Gemini，但实际调用日志报 403。
  近期消费归属的本地用户 2 配额为正、token 2 有效且不限额度；
  错误来自 channel error，不能将上游余额不足误认为本地认证/额度不足。
- 2026-11-03 23:18:18：现存近七天容器日志中切换前最后一条 Gemini 成功消费，
  渠道 52，67444 输入 token、500 输出 token，23 秒。
- 2026-11-04 16:58:26：首次观察到渠道 52 余额拒绝；
  request id 202611040858225727282838268d9d6Tr5Qr8qm。
- 2026-11-05 16:52:13：诊断时最新余额拒绝仍为 403；
  request id 202611050852101380624528268d9d6bpNcH0DW。
- 容器近七天日志中，11 月 4 日、5 日分别出现 30、72 条渠道 52 余额拒绝；
  按中转尝试计，一次请求可能对应多次重试。
- 应用 logs/elysium-2026-11-04.log:264、logs/elysium-2026-11-05.log:1033
  记录 Gemini 403，后续模型路由记录冷却跳过。更早的 503 与当前余额拒绝分别保留。
- A6 旧 channel 30 指向 https://a6api.com；Spark 对该旧调用地址实测 HTTP 200 text/html，
  返回网页脚本而非模型 JSON，未执行该脚本，也未将密钥转发至脚本引用站点。
  所以没有直接启用原渠道。
- A6 发布的接入说明 https://www.v2ex.com/t/1252676 指向 api.a6api.com；
  本机通过 HTTPS GET https://api.a6api.com/api/status 取得官方 api_info，
  公布 api.a6api.vip、cf.a6api.net 和 media.a6api.net。
  Spark 实测 api.a6api.vip 的同一公共状态接口正常且站点名为 A6api-Token聚合站；
  cf.a6api.net 在 Spark 返回 403，不纳入此次配置。
- 18:15:59 发起 A6 新入口直接合成文本测试，约 8.29 秒 HTTP 200、model=gemini-3.8-flash、正文 OK。

## 已执行变更与验证

18:19:07，在校验目标行、配置与缓存模式后，用单个 SQLite 事务：

1. 从禁用的 channel 30 复制出 53，沿用其既有 A6 密钥；仅保存在服务器，未输出密钥。
2. 新渠道仅选择 default/gemini-3.8-flash，status=1、priority=10、weight=10，
   清空无关模型映射，关闭自动增模，base_url 使用 api.a6api.vip。
3. 为 53 建立唯一 Gemini ability；将 52 status 改为 2、对应 abilities 禁用。
4. 事务内逐行确认其他原渠道和 abilities 未变；原 channel 30 及其其他模型仍保持禁用。

没有启用全渠道 A6，也没有更改其他模型选路、Elysium 配置、主体文件、历史或持久化会话。
本次没有向聊天平台发送测试消息；合成测试不以爱莉身份执行。

本地中转合成工具测试使用与 expression 相同的 OpenAI 兼容入口及 parallel_tool_calls=true：

- 第一轮：强制选择无外部副作用的 diagnostic_echo，HTTP 200，finish_reason=tool_calls；
  86 输入/16 输出 token，约 7.21 秒。
  New API request id 202611051019495420087498268d9d6tLpimWVp。
- 第二轮：回传对应 tool_call_id 的合成工具结果，HTTP 200，正文 OK；
  104 输入/1 输出 token，约 15.29 秒。
  request id 202611051019567386328408268d9d6weEpLcg1。
- New API logs 行 171544、171545 的 channel_id 均为 53，model_name 均为 gemini-3.8-flash。
  首次即时读取容器日志时异步消费记录尚未出现；随后 tail 和只读数据库均已确认两条成功记录，
  没有因此重复发起测试。
- /api/v1/health 为 200、alive=true、state=ready；PID 2664093、cwd 与原实例相同。

## 热生效依据、备份与回滚

容器 elysium-new-api 未开启 MEMORY_CACHE_ENABLED 或 REDIS_CONN_STRING，
挂载 /data 下无额外 .env。因此渠道查询直读数据库，无需重启或等待周期同步。
本机对应源码位于 /home/ayerelysia/.local/share/elysium-migration-20260908/new-api-build：
common/init.go:83、common/redis.go:25、model/channel_cache.go:108、
model/ability.go:94，以及 middleware/distributor.go:106 的现存 affinity 状态复核。

数据库位于 data/new_api_relay/one-api.db。
变更前使用 SQLite 在线 backup 取得一致快照，未复制运行中的裸 db 文件。
私有备份目录为：
data/new_api_relay/backups/gemini-a6-switch-20261105-181906/
目录权限 0700，数据库与审计文件 0600；该路径被 Git 忽略，不得提交或分享。
目录内 relay-before.db 为在线快照，switch.json 为状态/ability 回滚元数据，
gateway-tool-probe.json 为合成测试元数据，diagnosis-before-update.md 保留最初只读诊断记录。

回滚使用补偿事务：先核对 53 的名称和唯一 Gemini 能力、52 的身份与当前禁用状态，
禁用 53 及其 abilities，再按 switch.json 恢复 52 原 status=1 和其 default/Gemini ability enabled=1。
保留新渠道及其已发生的日志/计费记录；不要把整库备份覆盖回线上，避免抹去切换后的并发消费。
D1 余额未补充时回滚会恢复到原故障状态。

Elysium 模型冷却最长 300 秒，过期后由自然请求再探测；无需重启、清库或重建 session。
来源 src/kernel/llm/policy/failover.py:29、68、101、113。
23:20 的真实聊天已遇到 A6 HTTP/2/EOF 与表达链超时，真实聊天恢复尚未通过；详见同目录 2026-11-05-qq-expression-upstream-timeout.md。
