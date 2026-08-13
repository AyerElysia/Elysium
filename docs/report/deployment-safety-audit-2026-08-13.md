# 部署安全审计与脚本交付（2026-08-13）

## 范围与结论

本次审计覆盖仓库入口、依赖锁、Core/模型配置、插件默认生命周期、主体权威启动门、SQLite/MySQL 备份、历史启动资产和凭据传递。结论是：Elysium 的合规部署形态只能是“自动准备与只读检查 + 用户手工前台启动”，不能提供无人值守 Elysium 守护进程。

本次没有启动、停止、重启或替换 Elysium，没有连接外部数据库或平台，也没有执行迁移、lease 清理或数据恢复。真实运行验收仍归操作者所有。

## 证据与根因

1. `main.py` 与 `src/app/runtime/bot.py` 以仓库根目录的相对路径读取 `config/core.toml`、`plugins/` 和 `logs/`；部署入口必须固定 cwd。
2. `src/kernel/config/models_loader.py` 的正式初始化要求完整的 12 个 task route；旧实现没有对模型密钥做环境变量展开或占位符拒绝。
3. `plugins/life_engine/service/core.py` 的旧启动链会在主体校验前生成 `USER.md`，与 `AGENTS.md` 的主体执笔权不变量冲突。
4. `pyproject.toml` 与 `uv.lock` 曾固定到存在 asyncmy pre-ping 已知问题的 SQLAlchemy 2.0.46，而运维合同要求不低于 2.0.50。
5. `start.bat` 曾无条件调用 lease 清理；`scripts/cleanup_leases.sh`、`scripts/sync_job.sh` 和 `scripts/sync_local_to_mysql.py` 曾包含已提交数据库凭据、固定生产路径或把密码放入进程参数。该凭据必须视为已经泄露。
6. 根目录 systemd unit 与 Docker Compose 曾包含 Elysium 自动启动或自动重启语义，直接违反当前固定运行策略。
7. 多个可选插件默认启用，部分插件没有在注册有副作用组件前遵守 `enabled`，使新 clone 可能在未授权时尝试连接外部平台。
8. `config/core.toml.example` 曾含退役字段、错误的 `elysium.toml` 权威说明、漂移端口和运行时依赖安装开关。
9. MySQL 模板曾给出 `pool_recycle=1800` 与 `wait_timeout=180`，这既违反运行时硬校验，也会导致服务端先杀死空闲连接后被连接池复用。
10. 旧 doctor 未遍历配置树中的符号链接目录，也未验证主体工作区的全部父路径组件。
11. 系统 Python 在 Windows/macOS 上可能没有 `psutil` 且无 `/proc`；旧探针把这种“无法检查”误报为“没有实例”。
12. Windows 旧 doctor 未证明既有配置与恢复后主体文件的 ACL；MySQL 临时 defaults 文件也只使用了不具等价安全语义的 Windows `chmod`。
13. 本地快照的 Core 库清单仍固定旧 `Elysium.db`，与当前 `core.toml` 默认 `MoFox.db` 冲突；精确文件遍历还会解引用子目录符号链接。
14. 旧备份入口没有在写入权威字节前完整证明输出目录与每个敏感文件的 ACL，也没有在子进程返回后复核目录身份；MySQL 失败输出和继承环境还可能扩大凭据暴露面。

## 已执行变更

- 新增同源的 `deploy.sh`、`deploy.ps1` 与 `scripts/deployment.py`，提供 `bootstrap`、`doctor`、`run`、`backup`。
- 配置以 owner-only、原子、create-if-absent 方式落盘；异常文件类型 fail-closed。
- doctor 不导入会写盘的运行时配置对象，且不输出密钥或完整进程参数。
- run 只在 doctor 通过后以前台 `exec` 启动，不杀进程、不改端口、不清 lease。
- 模型密钥改为环境变量引用并在正式初始化时 fail-closed；SQLAlchemy 声明和 lock 同步升级。
- Life Engine 删除主体模板自动生成路径，校验前不取得会写盘的运行资源。
- 历史危险同步/清理入口改为退役或 owner-only 凭据文件传递；Windows 入口只转发到新 run。
- 可选插件新安装默认关闭，禁用时不注册其外部副作用组件。
- 上下文清理/权限命令也改为显式 opt-in；未完成的 AstrBot sister bridge 保持 manifest 级禁用，在导入前排除。
- Core 示例配置改为当前 schema、默认本地存储、8000 loopback 和禁止运行时装包。
- MySQL 连接池基准修正为 `120 < 180`，doctor 同步验证该不变量；配置树和主体工作区父路径中任何符号链接均 fail-closed。
- doctor/run/backup 先切入项目锁定解释器再执行 PID 探针；探测能力缺失会阻断重复启动和 `writer-frozen` 声明。
- Windows 配置、主体文件和备份目录改为 owner/ACL 可证明的 fail-closed 边界；本地快照会在复制主体及其他权威字节之前保护空输出目录。
- 部署备份要求预先存在的 owner-only 父目录，以排他方式创建目标并记录设备/inode；子进程返回后复核目标仍是同一普通目录，拒绝目录替换和 symlink 竞态。
- MySQL defaults、压缩 `.partial` 与 manifest 都先排他创建为空文件并收紧 ACL，再写入凭据或备份内容；连接参数进行 option-file 转义，密码不进入子进程参数/环境/错误，URL 解析失败也不回显原值。
- 本地备份从已校验的 `database.sqlite_path` 派生 Core 库，保留旧路径显式参数；快照层逐项拒绝符号链接、非普通文件和越界路径。统一归档扫描与隔离恢复的 Core 默认目标同步为 `MoFox.db`。
- 过期的 Elysium systemd/Docker 自动重启资产及 Docker 发布 workflow 从当前部署面移除，文档改为手工前台合同；CI 增加部署脚本静态检查。
- 根仓库现行文档统一为 deploy/uv 入口、环境密钥引用和 New API 独立自动生命周期；历史 handoff 中把密码持久写入 `.bashrc` 的做法被明确退役，不再作为当前操作建议。

所有工程变更可通过 Git 恢复。配置脚本从不覆盖既有本地配置；主体文件和耐久数据不在本次写入范围内。

## 验证方法

验收覆盖以下合同：

- 部署脚本帮助、shell 语法与静态检查；
- 临时仓库中首次创建、重复运行、外部 symlink、文件权限和并发边界；
- 缺失/空/非法 UTF-8 主体文件 fail-closed，检查前后文件哈希不变；
- 模型环境变量展开、占位符拒绝及错误输出脱敏；
- fake `uv` 只收到 locked sync 与依赖检查，不收到 `main.py`、systemd、cron 或 Docker 命令；
- writer 未冻结时拒绝 `--writer-frozen` 备份声明；
- 备份父目录权限、预创建输出目录、目录身份复核、Windows 写前 ACL、MySQL 临时凭据/partial/manifest 写前保护、描述符异常关闭及 secret 脱敏；
- Life Engine 定向生命周期测试和可选插件禁用测试；
- `ruff`、`compileall`、`git diff --check` 及风险范围 pytest。

任务执行记录中的两批风险范围 pytest 分别为 `98 passed` 与 `62 passed`。拉取并整合远端 `bf6d50c2` 后，最终完整离线回归命令 `uv run --group dev python -m pytest test -q --no-cov -n 0` 得到 `4677 passed, 21 skipped, 2 warnings`；两条 warning 均来自第三方 `websockets` 弃用提示。`uv lock --check`、ShellCheck、Bash 语法、`compileall`、全范围确定性 Ruff `E9/F63/F7/F82`、部署风险文件完整 Ruff 与 `git diff --check` 均通过。当前 Linux 环境没有 `pwsh`，因此 `deploy.ps1` 由共享 Python 合同测试与静态审查覆盖，尚未在真实 PowerShell 解释器执行。

拉取远端变更后的当前 checkout 执行 `./deploy.sh doctor --json` 保持完全只读，返回 `13 passed, 5 failed, 1 disabled` 和退出码 2。现有 Core 与模型配置的访问边界通过；失败项是 NexusAI provider 环境密钥尚未解析、本机配置未全部通过综合 schema，以及 `SOUL.md`、`USER.md`、`MEMORY.md` 尚未从可信历史恢复。检查没有创建或改写配置、主体文件或运行数据。该结果证明当前 checkout 会 fail-closed，不是生产就绪声明。

用户尚未在本次变更后手工启动 Elysium，因此即使后续离线检查全部通过，状态也只能是 `offline_validated`；`runtime_accepted` 仍须由用户手工前台启动并以关键链路日志证明。

## 跨仓库文档归属

根仓库中的 `Ayla` 是 mode `160000` 的独立 Git 子模块，而不是本仓库可直接维护的普通目录。本次只读审计确认子模块工作树无改动；其中部署方案仍有历史 `start_elysium` 表述，需要由 Ayla 子模块 owner 在独立任务中改为根仓库的 `deploy.sh doctor` / `deploy.sh run` 合同，并单独提交、测试和更新 gitlink。本任务没有越过仓库边界修改 Ayla，也不把该待移交项解释为根仓库仍提供旧启动脚本。

## 未决事项与归属

| 事项 | owner | 状态 |
| --- | --- | --- |
| 吊销并轮换历史脚本中出现过的数据库凭据 | 数据库/运维所有者 | 阻断旧凭据继续使用；代码删除不能撤销泄露 |
| 从可信备份逐字节恢复 `SOUL.md`、`USER.md`、`MEMORY.md` | 主体数据保管者 | 部署脚本禁止代写 |
| 配置实际 Provider 环境变量并验证服务条款 | 部署者 | 离线脚本只验证引用 |
| 手工前台启动并保留关键链路日志 | 用户 | 本次未执行 |
| MySQL generation、TLS、schema 与真实备份演练 | 数据库 owner | 仅 MySQL profile 需要 |
| NapCat/New API 等独立服务生命周期验收 | 各服务 owner | 不属于 Elysium run wrapper |

## 凭据事故处置

历史数据库密码应按“已提交并可能已复制”处理：立即在服务端吊销或轮换，审计最近登录与写入记录，更新受控 secret manager，并验证旧值不能再认证。不要把新值写入 Git、shell history、命令参数、工单或本报告。Git 工作树中的删除或改写不等于清除历史对象；若需要历史重写，必须由仓库所有者另行授权、协调所有 clone 并制定回滚方案。
