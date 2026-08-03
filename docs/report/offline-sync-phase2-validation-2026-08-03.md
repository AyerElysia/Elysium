# 离线同步阶段二实现与验证报告（2026-08-03）

## 1. 结论

计划中的阶段二“离线同步内核”已完成实现：Life Event 与本地 Outbox 同事务、持久节点身份与序号、远端 MySQL 幂等账本、服务端事务 Outbox、Inbox/游标、租约恢复、指数退避、冲突记录和健康输出均已落地。

本地 MySQL 8 和朋友的远端 `elysium` 数据库都跑通了真实全链路样例，不是 mock-only 验证。运行中的 Elysium 实例保持不动；本次没有 kill、TERM、重启、nohup 或自动拉起。

## 2. 实现范围

- 新增 `src/kernel/sync/` 通用同步内核；
- `RawEventStore` 在一笔 SQLite 事务内提交权威事件和已授权 Outbox；
- 默认私有、未请求共享的事件不复制到 Outbox；
- 显式共享事件才获得连续 `origin_sequence`；
- MySQL 事件与服务端 Outbox 同事务提交；
- 重复投递返回成功，同身份异内容写入冲突并阻断顺序；
- Inbox 先持久化，应用成功后原子推进本地游标；
- 远端导入事件禁止回声重发；
- worker 纳入 Life Engine 统一任务生命周期；
- 配置默认关闭，密码只从环境变量读取；
- 健康输出不包含凭据或 payload。

## 3. 定向故障验证

`test/kernel/sync/test_offline_sync.py`：10 passed。

覆盖：

1. Life Event 成功但 Outbox 注入失败时整笔回滚；
2. 私有导出请求被 held，未请求导出的私有事件不产生 Outbox 副本；
3. 远端离线时本地写入保留；
4. 恢复连接与新 coordinator 实例后自动补传；
5. 远端已提交、本地确认前崩溃后重放为 duplicate；
6. 同 ID 异内容冲突并阻断后续序号；
7. Inbox 应用失败时游标不前移；
8. 重启后 staged Inbox 可继续应用；
9. Inbox 重复和冲突均有持久证据；
10. 默认配置、健康输出和远端导入防回声。

## 4. 真实 MySQL 8 验证

### 4.1 本地隔离数据库

每轮创建名称受限的独立测试库和临时用户，验证后核对精确目标并删除。结果：1 passed。

最终验收行数：

| 项目 | 行数 |
|---|---:|
| 新共享事件 | 2 |
| 服务端 Outbox | 2 |
| 故意注入的冲突 | 1 |
| schema 版本 | 2 |

验证路径包括：8 路并发投递同一事件只接受一次、重复确认、异内容冲突、本地 Outbox 经 coordinator 推送到真实 MySQL、第二个本地节点经 Inbox 拉取和应用。

### 4.2 远端共享数据库

远端真实测试结果：1 passed。阶段二表已创建，schema 版本为 2；并发幂等、Outbox 推送、远端拉取与本地 Inbox 应用均通过。只保留非私密 `sync.contract_probe` 事件，没有制造远端冲突。

最终只读核对：

| 项目 | 结果 |
|---|---:|
| 阶段二共享探针事件 | 4 |
| 服务端 Outbox | 4 |
| 开放冲突 | 0 |
| 原有 `messages` | 82,575 |

原有 `messages` 行数与阶段二前一致；本次只新增 `elysium_*` 同步命名空间，没有改写 Core、聊天或记忆表。

## 5. 回归与静态检查

- 全仓默认回归：3,216 passed，5 skipped，覆盖率 65.77%（门槛 40%）；
- 同步定向测试：10 passed；
- Event Bus、意识在场与世界投影联合回归：38 passed；
- Life Engine service 与同步受管生命周期回归：25 passed；
- 本地 MySQL 全链路：1 passed；
- 远端 MySQL 全链路：1 passed；
- 新增代码与测试 Ruff：通过；
- Python compileall：通过；
- `git diff --check`：通过；
- 禁止修改的四个生命周期文件：未改动。

项目 pytest 默认对单文件执行仍会套用全仓 40% 覆盖率门槛，因此最初“7 passed”后命令返回覆盖率失败；随后所有定向验收均使用 `--no-cov` 明确区分“用例失败”和“全仓覆盖率策略”，用例本身无失败。

## 6. 环境事实

- `pyproject.toml` 和 `uv.lock` 已声明 `asyncmy>=0.2.11`；实际虚拟环境此前缺少该包；
- 已按锁定版本安装 `asyncmy==0.2.11`，随后真实 MySQL 验证通过；
- 安装依赖没有重启或触碰运行中的 Elysium；
- 配置默认 `shared_sync.enabled = false`，当前实例不会热加载新代码。

## 7. 尚未宣称完成的内容

- 阶段三 API、SSE/WebSocket 和前端实时状态尚未实现；
- 具体哪些记忆版本允许共享仍需由上层主体/授权流程产生显式共享事件；同步层不会自行判断；
- 正式启用要在代码合并后配置环境变量，并由用户手动重启；
- 当前远端账号/TLS/权限收敛与长期备份策略沿用既有后续安全计划，不在阶段二擅自扩权。

架构见 [离线同步内核](../architecture/offline_sync_kernel.md)，上线步骤见 [离线同步运行手册](../operations/offline_sync_runbook.md)。
