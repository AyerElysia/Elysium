# PR #1 集成审查与旧项目退役报告（2026-08-14）

## 范围

- PR 基线：`57776d581ab9dcbf12c73ca62a61dcc788571819`
- 审查时主线：`a3c3c9571659393c46637731d7f3949f8a7c5383`
- 合并方式：在独立 WSL worktree 中把最新 `soul/main` 合入 PR，修复后执行风险回归与全仓回归。
- 本报告中的“退役”只覆盖当前运行代码、依赖、配置、测试和现行文档。`docs/archive/**` 是用户要求保留的历史材料，未删除、未改写。

## 旧项目退役结果

1. 活动代码不再依赖外部旧消息线包；统一消息信封、构建器和 HTTP/WebSocket 适配器合同迁入 `src/core/transport/wire.py`。
2. `pyproject.toml` 与 `uv.lock` 已移除旧消息线依赖，显式声明内部实现直接使用的 `aiohttp` 与 `orjson`。
3. NapCat、飞书、KOOK、Ayla、Neko 及核心消息收发/转换/分发均切换到 Elysium 自有合同。
4. 保持既有 WebSocket 默认帧兼容：二进制 JSON，外层 `type=send`，入站 `type=message` 解包；补充独立协议测试。
5. 当前数据库默认名统一为 `Elysium.db`，PostgreSQL 示例默认库名统一为 `elysium`；不会自动移动、重命名或删除已有数据库文件，显式配置的非默认路径仍按配置读取。
6. 删除两个仅服务旧目录结构、且硬编码失效路径的一次性脚本：`check_live_broadcast.py`、`test_history.py`。
7. 活动范围（排除 `.git`、测试虚拟环境、运行数据、日志和用户保留的 `docs/archive/**`）大小写不敏感扫描旧项目名称为零结果。

## PR 审查中修复的问题

### Memory Witness claim 丢失后无法恢复

显式 `SingletonWriterClaimLost` 原先未进入重新认领分支，Witness 会长期保持 guest/skipped。现在统一识别两个兼容导入路径的 claim-lost 异常，清除陈旧 claim 并重新获取；原始 experience 不删除，修复的是见证延迟和重复积压。

### Trajectory archive 空归档竞态

flush 与历史分区压缩原先使用不同锁，维护线程可能在 append/fsync 完成前复制并删除同一 raw JSONL，生成结构合法但内容不完整的 gzip。现在维护与 flush 共用 flush 临界区；测试预先关闭外部写入，并新增“flush 卡在 fsync 时 maintenance 必须等待”的并发回归。

### Bash 只读沙箱测试使用不可见解释器

测试取得的虚拟环境解释器位于 `/tmp`，而 Bubblewrap 会用隔离 tmpfs 覆盖 `/tmp`，导致命令未执行到只读写入检查。测试改为解析真实解释器目标，仍在真实 Bubblewrap 中尝试修改只读 workspace 并验证失败。

### WatchDog 与 Scheduler 全仓时序不稳定

- WatchDog 的健康判断改用 monotonic 时间，墙钟回拨时展示时间仍保持非递减，避免心跳时间倒退和错误超时判断。
- Scheduler timeout 测试改为等待 callback started、cancelled 与 schedule removed 三个真实生命周期事件，不再用固定 sleep 猜测全仓负载下的完成时机。

## 验证证据

- 消息适配、部署、备份、同步、模型路由、Memory Witness、Life Engine 风险回归：`341 passed`。
- Trajectory/Bash 专项：`26 passed`；归档竞态专项连续 20 轮通过。
- WatchDog/Scheduler 全套：`189 passed`。
- 全仓最终：`4722 passed / 21 skipped / 3 warnings`，覆盖率 `70.96%`。
- `compileall`：通过。
- 变更差异检查：通过。
- 旧项目名称活动范围残留扫描：零结果。

## 启动与推送门

本变更涉及依赖、配置、插件与运行时消息链。依据 `AGENTS.md`，当前只允许保留为本地提交；必须由用户手动启动 Elysium，确认插件加载、NapCat/飞书等关键适配器、Life Engine 与真实收发链路正常后，才允许推送并合并。Agent 未启动、重启或停止任何正式进程。
