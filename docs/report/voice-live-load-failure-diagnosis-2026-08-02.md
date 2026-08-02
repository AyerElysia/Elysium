# Voice Live 插件加载失败诊断（2026-08-02）

## 结论

本次失败不是 Qwen Realtime 凭证、网络或 Seed-VC 导致，而是 Elysium 在 Life Engine 并发重构尚未稳定时被手工启动，进程读到了不完整的模块状态。

当前实例中 Life Engine 先加载失败，Voice Live 随后因为拿不到 `ConsciousnessInstance` 而被启动事务回滚。磁盘上的相关模块之后又被继续修改；当前独立导入检查已经成功，但运行中的进程不会热重载启动阶段失败的插件，因此仍需在并发修复完成后由用户手工重启 Elysium。

## 证据时间线

| 时间 | 证据 |
|---|---|
| 23:18:24 | 当前 Elysium 进程 PID 285659 由用户终端启动：`.venv/bin/python main.py` |
| 23:18:30.871 | `life_engine` 加载失败：`No module named 'life_engine.service.consciousness'` |
| 23:18:34.105 | `Voice-Live` 加载失败：`LifeEngine attribute is unavailable: service.consciousness.ConsciousnessInstance` |
| 23:22:07 | `plugins/life_engine/service/core.py` 在进程启动后继续被修改 |
| 23:23:03 | `plugins/life_engine/service/consciousness.py` 在进程启动后继续被修改 |
| 23:23:43 | 使用项目同一虚拟环境独立导入 `life_engine.service.consciousness.ConsciousnessInstance` 成功 |

结构化日志会话为 `20260802_231828_ce019efa`，日志数据库为 `data/logs.db`。

## 当前运行状态

- PID 285659 仍在监听 `127.0.0.1:18000`；
- `/voice-live/` 返回 HTTP 404，证明 Voice Live Router 没有在本次进程中挂载；
- 当前磁盘代码的只读导入检查通过，不等于正在运行的进程已经恢复；
- 诊断过程中没有停止、重启、发送信号或自动拉起 Elysium。

## 恢复方式

1. 等待正在进行的 Life Engine 生命周期修复完成并确认工作区不再继续变动；
2. 由用户在原终端手工停止并重新启动 Elysium；
3. 新进程启动后确认日志依次出现：
   - `插件加载成功: life_engine`；
   - `插件加载成功: Voice-Live v2.0.0`；
   - `Voice Live 路由已启动`；
   - `Router 已挂载: Voice-Live:router:voice_live -> /voice-live`；
4. 只读请求 `/voice-live/`，预期不再返回 404；随后再做浏览器麦克风与真实通话验证。

如果新进程仍然出现相同错误，需要保存该次新 session 的完整 traceback，再检查插件加载器的模块缓存与包加载顺序；本次证据更符合“启动撞上文件修改中间态”，不是一个当前仍可稳定复现的导入错误。
