# 本地 FileAuthority 续租与写 fence 锁耦合（2026-09-02）

## 现象

17:50–17:51 同一进程连续出现：

- `subject workspace projection worker retrying after StaleAuthorityToken`
- `主体主动线索技术投递暂未完成`
- `消息慢阶段后台持久化失败: authority token lease has expired`
- Router 活动落账失败、Chatter 重置 `_GLOBAL_RUNTIME`

这不是 LLM 500 本身，也不是第二个 writer 抢租约。日志里没有
`storage authority was conclusively lost` / `exiting for supervised restart`，
说明续租任务仍卡在拿锁，进程带着过期租约继续把每条写路径打成失败。

## 时间线

| 时间 | 证据 | 来源 |
|---|---|---|
| 17:50:58 起 | projection worker 对 `StaleAuthorityToken` 退避重试 | 用户粘贴的 life_engine 日志 |
| 17:51:04 | 主动线索投递未完成；heartbeat LLM 500 后改走 terra | 同上 |
| 17:51:19–21 | 入站消息慢阶段持久化失败，lease has expired | 同上，message_id 已给出，无正文 |
| 17:51:32–38 | Router/Chatter/会话循环同一异常并重置全局 runtime | 同上 |

未改正式数据、未重启 Elysium、未读取私人消息正文。

## 根因

本地 `FileAuthorityRegistry` 用**同一把** advisory lock 做两件互斥的事：

1. 写事务 `fenced()` / `AsyncUnitOfWork.scope` 全程持有 **共享锁**（含 SQLite 事务和 after-commit）；
2. `renew()` 取 **独占锁**。

Linux `flock` 下，本进程重叠的共享写会让本进程的续租一直排队。租约（现场曾为 60s）一旦过期：

- `validate` / `fenced` 正确 fail-closed；
- 旧 `_renew_sync` 在续租前也调用 `_assert_token_unlocked`，过期后**连补续都失败**；
- 续租若仍堵在独占锁上，则不会走到 `os._exit(30)`，于是出现“反复 StaleAuthorityToken、进程不退出”。

2026-09-01 的验链缓存修复了全量 JSONL 扫描拖死续租的那一层，但没有拆开这两把锁。把问题写成“只是没复用审计头”是不完整的，已在该报告中改写。

加长 `authority_lease_seconds` 只能掩盖饥饿，不能消除同锁互斥。

## 修复

1. **Cutover 锁**（原 `authority.json.lock`）：仅隔离 epoch 变更。写 fence 持共享；activate/revoke 持独占。
2. **Audit 锁**（`authority.json.audit.lock`）：只覆盖验链与 state/audit 追加，不得跨 SQLite UoW 持有。
3. **在位者续租只拿 audit 锁**。墙钟过期但 epoch/owner/fencing hash 仍匹配时允许补续；真正被抢元的 token 仍是 `StaleAuthorityToken`。
4. **keepalive 线程**在 `open_storage_backend` 打开 file authority 后启动，按 renew interval 在事件循环外续租；revoke/close 先停线程。`interval<=0` 的测试不启动线程。

lease 时长、fencing 语义、cutover 必须等写 fence 结束，均未放宽。

## 验证

定向测试：

- 既有 cutover 仍会等到 fence 退出才 activate；
- 持有 fence 时 incumbent `renew` 必须在 2s 内返回；
- 租约墙钟过期后 validate 失败、同一 token renew 成功；
- keepalive 在 fence 持有期间把租约续过过期点。

命令：

```bash
uv run --group dev python -m pytest test/plugins/life_engine/test_storage_authority.py -q --no-cov -n 0
```

未与运行中的 Elysium 并行跑全仓或会争用正式 authority 的测试。

## 运行验收门

修复只在用户手动启动的新 Elysium 进程中生效。agent 不得代启。重启后应看到：

- 单一主进程；
- file authority health 中 `incumbent_keepalive=running`（local selectable）；
- 续租不再出现接近整个 lease 的空档；
- 写路径不再把 `StaleAuthorityToken` 当成可靠的 Chatter 重建信号刷屏。

若重启后仍过期，先查事件循环是否长时间阻塞，以及 keepalive 线程是否存活；不要先把 lease 加到数分钟当作修复。
