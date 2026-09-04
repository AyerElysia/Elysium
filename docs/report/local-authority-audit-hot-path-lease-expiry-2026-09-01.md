# 本地 Authority 审计热路径导致租约过期（2026-09-01）

## 现象

Life Engine 在运行中持久化 life_engine.runtime_context 时出现
StaleAuthorityToken: authority token lease has expired。失败发生在进入本地
selected-storage 事务的 fencing 校验阶段，随后心跳轮以关键状态持久化失败结束。

该异常是 fail-closed：过期 token 对应的写入没有被当作成功提交，也没有放宽
generation、epoch、owner、lease 或 fencing-token 校验。

## 现场证据

- 本地 authority 配置为 60 秒 lease、20 秒 renew interval。
- authority 审计文件约 17,706,209 bytes、32,034 条事件。
- 现场相邻续租实际间隔多次达到 28–58 秒，最终越过 60 秒租约。
- 每个本地 fenced transaction 在持有共享文件锁时：
  1. _verify_audit_unlocked() 从头重放并哈希完整 JSONL；
  2. generation immutable 校验又再次扫描完整 JSONL 查找 registration。
- renew 需要取得同一锁的独占模式。高频共享事务持续做两次全链扫描时，续租线程被
  计算开销和锁竞争拖延，最终失去 authority。

因此根因不是“60 秒租约天生太短”，而是已经写入架构/runbook 的
“同一进程、同一审计头复用验链证明”没有在 FileAuthorityRegistry 落实。

2026-09-02 复发证明：验链缓存是必要的，但**不是充分根因**。续租当时仍对
与写 fence 相同的 cutover 锁取独占模式；本进程重叠的共享写事务会饿死续租。
完整修复见 [本地 FileAuthority 续租与写 fence 锁耦合（2026-09-02）](./file-authority-renew-lock-decouple-2026-09-02.md)。

## 修复

FileAuthorityRegistry 现在保存 content-free 的进程内已验证审计头：

- 文件身份：device、inode、size、mtime_ns、ctime_ns；
- 已验证 head hash 与 event count；
- 完整验链时取得的 generation registration（generation_id、manifest_sha256）。

行为合同：

1. 进程首次观察某个审计头时仍从第一条事件完整重放 hash chain。
2. 只有文件身份和 registry state 的 head hash 均与已验证证明完全一致时，后续
   transaction 才复用证明。
3. 任一文件变化、外部 append、state/head 错配都会使证明失效并重新完整验链。
4. 本进程在独占锁内 fsync append 的事件可原子扩展证明；若后续 state 写入失败，
   下次读取因 state/head 不一致而 fail closed。
5. immutable generation 校验从同一个已验证证明读取 registration，不再第二次扫描。
6. lease 时长、renew 周期、token 校验、cutover locking 和 fencing 语义均未改变。

## 验证

- File authority 定向：8 passed。
- Authority + storage factory + runtime-state storage contract：40 passed。
- 回归证明同一 head 上连续 validate/fenced/health 只做一次完整扫描。
- 另一个 registry 续租并改变审计头后，reader 会且只会新增一次完整扫描。
- 既有 audit tamper 测试继续证明缓存已建立后修改审计文件仍 fail closed。

本轮未运行与正在运行的 Elysium 争用正式端口、进程生命周期或正式 authority 的
高负载/全仓测试；未修改正式数据、配置、租约值或进程。

## 运行验收门

修复只在下一次用户手动启动的新进程中生效。现场旧 tmux/bash 会话在 launcher
改为 one-shot 之前已把自动循环读入内存，因此仍可能继续拉起旧子进程；必须由用户
手动完全结束该旧会话，再从当前原目录手动启动一次。

推送前需要观察至少三个 renew interval，并确认：

- 只有一个 Elysium 主进程；
- authority renew 约按 20 秒推进，不再出现接近 60 秒的空档；
- 没有新的 StaleAuthorityToken；
- Life Engine 与运行上下文持久化持续正常。
