# Elysium 双实例 MySQL 模式报错汇总（2026-08-12）

> 环境：双实例（elysium-windows-primary + elysium-linux-primary）共享远端 MySQL（frp-one.com:65429，库 elysium），multi_writer_enabled=true
> 用途：转发给合作者排查。均为今日日志 elysium-2026-08-12.log 真实记录。

## 一、SingletonWriterClaimLost（学习维护熔断）— 今日 382 次，最严重

典型日志（每 ~16 秒一次，持续不断）：
```
14:53:32.339 WARNING life_engine.learning.scheduler | 学习维护阶段无法记录开始证据，已拒绝执行 reflection: SingletonWriterClaimLost
14:53:48.507 WARNING life_engine.learning.scheduler | 学习维护阶段无法记录开始证据，已拒绝执行 reflection: SingletonWriterClaimLost
14:54:04.216 WARNING life_engine.learning.scheduler | 学习维护阶段无法记录开始证据，已拒绝执行 reflection: SingletonWriterClaimLost
...（14:50 起每 16 秒一条，至 14:57+ 仍在持续，全天累计 382 次）
```
伴随：`learning maintenance worker cycle failed: SingletonWriterClaimLost` / `RuntimeError`

首条完整 traceback（line 5124）：
```
File ".../life_engine/storage/contracts.py", line 316 ...
  （租约校验失败路径：ManagedSingletonWriterClaimLost / 写入前 validate 被拒）
```

## 二、BoundedContinuationError（续读游标熔断）— 今日 3 次

```
06:42:05  WARNING life_engine.event_grep | 搜索 life 事件流失败: bounded-result continuation does not match query/task/frontier
09:07:17  WARNING life_engine.event_grep | 搜索 life 事件流失败: bounded-result continuation does not match query/task/frontier
09:17:27  WARNING life_engine.tools | 列出目录续读游标已拒绝: error_type=BoundedContinuationError
11:13:07  WARNING life_engine.event_grep | 搜索 life 事件流失败: bounded-result continuation does not match query/task/frontier
11:16:23  WARNING life_engine.event_grep | 搜索 life 事件流失败: bounded-result continuation does not match query/task/frontier
14:40:51  WARNING life_engine.tools | 读取文件续读游标已拒绝: error_type=BoundedContinuationError
15:25:37  WARNING life_engine.event_grep | 搜索 life 事件流失败: bounded-result continuation does not match query/task/frontier
16:09:20  WARNING life_engine.event_grep | 搜索 life 事件流失败: bounded-result continuation does not match query/task/frontier
16:10:55  WARNING life_engine.tools | 读取文件续读游标已拒绝: error_type=BoundedContinuationError
16:13:15  WARNING life_engine.event_grep | 搜索 life 事件流失败: bounded-result continuation does not match query/task/frontier
```
> 机制：bounded_projection.py 的游标绑定 frontier/binding 的 HMAC 校验和，续读时参数与首查不一致即拒绝。模型（爱莉希雅）拿"读取文件失败: bounded-result continuation does not match query/task/frontier"后原地换参重试 → 表现为反复读同一文件/反复搜索。

## 三、PresenceRevisionConflict（presence 竞争）

```
latest: presence revision conflict for 'memory_witness': expected 1858, actual 1861
```
此前：expected 1814, actual 1815；expected 1752, actual 1764 等。双实例对同一 presence 行 CAS 竞争。

## 四、PerceptionCursorConflict — 今日 2 次

```
stale perception cursor for 'memory_witness': expected (105355, 1133), actual (105397, 1134)
```

## 五、RuntimeStateRevisionConflict（rolling_context 竞争）

```
RuntimeStateRevisionConflict:life_chatter.rolling_context:chat_global:expected=444:actual=445
```

## 六、2013 Lost connection — 今日 7 次

```
12:12:22  心跳模型异常: (2013, 'Lost connection to MySQL server during query')
12:31:46  插件 'life_engine' 加载失败: 插件启动事务失败: (2013, ...)
（另有 05:02、07:17、10:20 等）
```
> 已定位：会话 wait_timeout=180s（引擎硬编码 idle_session_timeout）与池 recycle=1800s 失配——服务端杀空闲连接后池仍复用死连接。recycle 已改 120s（配置不入库）。

## 七、3024 查询超时 — 今日 4 次

```
memory_index_jobs claim: (3024, 'Query execution was interrupted, maximum statement execution time exceeded')
```
> 应用 max_execution_time=10s（mysql_query_timeout_seconds=10）触发。

## 八、heartbeat 工具续轮熔断

```
16:04:32 reason=consecutive_tool_stalls model_turns=2 consecutive_no_progress=2 consecutive_protocol_failures=0
16:05:39 reason=consecutive_tool_stalls model_turns=2 consecutive_no_progress=2 consecutive_protocol_failures=2
16:11:26 reason=max_model_turns model_turns=5 consecutive_no_progress=0
16:13:16 reason=max_model_turns model_turns=5 consecutive_no_progress=1 consecutive_protocol_failures=1
```

## 九、memory_witness 运行失败 — 今日 8 次

```
13:08:06 / 13:13:07 / 13:48:50 / 13:5x / 14:0x / 14:1x / 15:0x / 15:3x 记忆见证意识运行失败
（PresenceRevisionConflict / PerceptionCursorConflict / 2013 混合）
```

## 已知修复（本机已提交 8e96bd2 / a462ce1）

1. rolling_context 保存加单写者租约仲裁（chatter.py acquire_singleton_writer）
2. presence_store 导入统一绝对路径（修重复类导致 except 失效，a462ce1）
3. pool_recycle 1800→120（配置，不入库）
4. Clash DIRECT + fake-ip-filter（frp-one.com 不再走代理）

## 待合作者判断

- SingletonWriterClaimLost 全天 382 次是否正常降级？租约为何长期被 Windows 实例持有（或续租失败）？
- learning maintenance 每 16 秒重试是否应加退避？
- BoundedContinuationError 的 frontier 校验在双实例下是否过严（frontier 被另一实例推进即失效）？
