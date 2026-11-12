# NapCat CLOSE-WAIT watchdog

## 默认与授权边界

`napcat_server.close_wait_watchdog_enabled` 默认 `false`。缺失配置、配置读取失败或非布尔开关不会启动监控任务，也不执行诊断或强制断连接。既有未提交改动中的默认关闭开关予以保留。

开启开关还不等于可以清理：必须显式配置 `napcat_server.close_wait_watchdog_process_dir`，值为受管 NapCat 进程 cwd 的绝对路径。未配置目录、没有反向 WebSocket 鉴权令牌、处于 direct 模式、远端或容器网络对端、非当前用户或无法读取进程身份时，均跳过恢复。开关不授予其他服务、其他用户或其他容器的控制权。

## 目标和故障证据

只从本适配器已经接收并完成鉴权的反向 WebSocket 取实际 local/remote 地址，再精确匹配 `ss` 中对应的本机回环连接。不会取“连接到某端口的第一个 PID”；多个 owner 视为不明确。

进程必须匹配显式目录和当前 UID，并记录 PID、父 PID、创建时间、UID、cwd 快照。进程退出、PID 复用或身份改变都会清除旧观察，不能沿用上一进程的 socket 年龄。

一条目标连接必须以相同地址对和 fd 持续观察为 CLOSE-WAIT 至少 90 秒。业务消息安静、建议状态 offline 或单次异常都不会触发清理。持久时间使用单调时钟；停用或卸载清除观察，重新开启不把未观察的间隔算入持续时间。

## 恢复与限制

清理前重新验证 WebSocket 对端 PID、进程快照、当前 CLOSE-WAIT 地址对和 fd。原生 Linux 每条命令同时限定 `state close-wait`、源地址/端口、目的地址/端口；不会批量关闭某进程的全部 fd，也不停止或重启 QQ、NapCat 或 Elysium。

一个适配器生命周期最多尝试三批，每批最多一条目标连接，分别等待至少 300、600、1200 秒才允许后续尝试；进程变更不重置次数。成功和失败都消耗预算并退避。日志保留准确 PID/fd、结果、次数和下次允许时间。命令退出成功只表示执行完成，不冒充消息恢复或平台投递成功。

WSL2 缺少此路径需要的原子 socket 归属保障，不调用旧 pidfd/shutdown 恢复帮助函数；仅保留观察并报告跳过。系统类型诊断失败时也不尝试强制清理。诊断子进程有界，超时或取消会回收该诊断进程，不操作服务进程。

## 验证与运维

隔离验证命令：

```sh
uv run --no-sync --group dev python -m pytest \
  test/plugins/test_napcat_close_wait_watchdog.py \
  test/plugins/test_napcat_adapter_startup_validation.py -q --no-cov -n 0
uv run --no-sync --group dev ruff check --select F,E9 \
  plugins/napcat_adapter/config.py plugins/napcat_adapter/plugin.py \
  test/plugins/test_napcat_close_wait_watchdog.py
```

测试只构造假配置、假进程、假 socket 和 mock 命令，覆盖关闭开关、目录/UID、实际 WS 对端、歧义 owner、PID 复用、连续观察、退避/预算、精确命令、WSL2 禁止清理及诊断取消。测试不读取生产配置、不打开真实网络连接、不执行 `ss -K` 或 pidfd shutdown。

本次没有启用监控、修改生产配置或操作运行实例；真实恢复效果未验收。需要启用时，生命周期 owner 先核对目标目录、账户、反向 WS 端口与进程归属，保留现场证据，再按当前任务授权执行。其他用户的 `napcat-napcat-1` 不属于本适配器的受管目标。撤回恢复能力只需关闭开关；没有数据格式迁移或主体内容改动。

2026-11-09 的隔离核对：上述两份测试共 57 项通过，只有既有 websockets.legacy 弃用警告；F/E9 检查和新增测试完整 Ruff 均通过。完整 plugin.py Ruff 仍有 11 项旧基线问题；对 HEAD 内容以相同配置逐条对照，旧基线为 17 项，本次没有新增诊断。以上是本次源码快照的结果，不代替未来改动后的重新验证。
