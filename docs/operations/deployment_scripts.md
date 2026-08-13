# Elysium 安全部署脚本

本页是新机器部署的规范入口。脚本负责安装、配置、只读检查和备份；Elysium 主进程始终由用户在可观察终端中手工、前台启动。脚本不会创建 systemd、cron、计划任务、登录项、Docker restart policy，也不会启动或重启 NapCat、New API、MySQL 或 Elysium。

## 1. 支持范围

基础 profile 使用项目根目录 `.venv`、本地 SQLite 和内嵌 Chroma 投影。Windows PowerShell、Linux、WSL 和 Git Bash 共用 [`scripts/deployment.py`](../../scripts/deployment.py) 的同一套安全逻辑：

- Windows：[`deploy.ps1`](../../deploy.ps1)
- Linux / WSL / Git Bash：[`deploy.sh`](../../deploy.sh)

Python 必须不低于 3.11，依赖和命令统一由 `uv` 管理。MySQL、NapCat、Ayla、Voice Live、直播、Minecraft、独立 Router 和视觉 Embedding 都是显式可选能力，不在基础部署中自动拉起。

## 2. 首次准备

在仓库根目录执行：

```bash
./deploy.sh bootstrap
```

PowerShell：

```powershell
.\deploy.ps1 bootstrap
```

开发机需要测试依赖时增加 `--with-dev`。`bootstrap` 固定执行 `uv sync --locked` 和 `uv pip check`；生产 profile 不安装 dev 组。它只会原子创建此前不存在的工程配置，已有普通文件逐字节保留，符号链接、目录或其他异常目标会令操作失败。新建配置在 POSIX 上为 `0600`。

创建范围包括：

- `config/core.toml`、`config/models.toml`、`config/mcp.toml`；
- Life Engine 的工程开关；
- 默认关闭的可选平台、语音、直播和游戏配置；
- `data/runtime/` 与 `logs/` 等工程目录。

脚本不会创建、复制、补全或改写 `SOUL.md`、`USER.md`、`MEMORY.md`，也不会创建 `.env`。如果只需在已经同步好的环境中补齐缺失工程配置，可显式使用 `bootstrap --config-only`。

## 3. 密钥与模型配置

`config/models.toml` 使用环境变量引用，真实 token 不进入 TOML、命令行或日志。基础示例要求当前终端提供：

```bash
export ELYSIUM_NEXUS_API_KEY='...'
```

PowerShell：

```powershell
$env:ELYSIUM_NEXUS_API_KEY = "..."
```

只配置实际使用的 Provider；本地 Router 等可选侧车应在单独验收后再增加 Provider 和 model。若删除某个 Provider，也必须同步删除引用它的 model 和 task route。正式加载会拒绝空密钥、未解析的 `${VAR}` 和示例占位符，部署 doctor 还要求密钥来自完整的 `${ENV_VAR}` 引用。部署脚本只报告变量名和存在性，不显示值。

MySQL 密码使用 `ELYSIUM_MYSQL_PASSWORD`。MySQL 逻辑备份使用完整 URL 环境变量 `ELYSIUM_MYSQL_URL`，该 URL 只传给受控 Python 进程，`mysqldump` 凭据通过临时 `0600` defaults 文件传递，不进入进程参数。

不要创建仓库根 `.env` 来保存密钥。若由外部 secret manager 注入环境变量，确保其生命周期 owner、权限和审计范围明确。

## 4. 主体权威恢复

本地 profile 在启动前要求以下三个权威文件已经从可信版本历史或备份逐字节恢复：

```text
data/life_engine_workspace/SOUL.md
data/life_engine_workspace/USER.md
data/life_engine_workspace/MEMORY.md
```

三个文件必须是仓库内普通文件、可读取且为 UTF-8；`SOUL.md` 还必须非空。部署工具只验证这些条件，不读取到输出、不规范化内容、不从模板生成内容。若没有可信备份，只能把部署标记为“基础设施已准备，等待主体恢复”，不能创造新的第一人称内容后宣称恢复完成。

POSIX 上本地配置与三个主体文件必须归当前用户所有且为 `0600`。Windows 上 doctor 会用只读 `Get-Acl` 验证 owner、继承状态与 allow ACE；无法证明为当前用户独占时会 fail-closed，不是警告后继续。从备份恢复后可由数据保管者只收紧 ACL（不改文件内容）：

```powershell
$identity = whoami
$paths = @(
  "config/core.toml", "config/models.toml", "config/mcp.toml",
  "data/life_engine_workspace/SOUL.md",
  "data/life_engine_workspace/USER.md",
  "data/life_engine_workspace/MEMORY.md"
)
foreach ($path in $paths) {
  icacls $path /setowner $identity
  icacls $path /inheritance:r /grant:r "${identity}:(F)"
}
```

MySQL profile 的主体权威由绑定的 verified generation 提供；离线检查不会连接远端或把远端内容回填本地。

## 5. 启动前检查

```bash
./deploy.sh doctor
```

PowerShell：

```powershell
.\deploy.ps1 doctor
```

机器可读输出使用 `doctor --json`。检查完全只读，不导入会自动生成或重写配置的运行时单例。它验证：

- Python、`uv`、`.venv` 与 `uv.lock`；
- Core/模型 TOML、12 个正式模型任务和引用闭合；
- MySQL 连接池回收时间小于服务端空闲超时（基准值 `120 < 180`）；
- 密钥引用是否已解析，但不显示密钥；
- 本地主体文件存在、UTF-8 与 `SOUL.md` 非空；
- 已有 Elysium PID、父 PID、工作目录及 Core HTTP 监听端口 owner；
- MySQL generation、App API 条件密钥和被显式禁用的能力。

状态含义：

| 状态 | 含义 |
| --- | --- |
| `PASS` | 离线合同满足 |
| `WARN` | 可继续，但仍需启动时或真实服务验收 |
| `FAIL` | fail-closed，不允许 `run` |
| `OFF` | 能力被明确禁用，不冒充故障 |

端口占用或同仓库实例存在时只报告 owner，不杀进程、不清 lease、不偷偷换端口。MySQL 可达性、Provider 网络和平台真实收发仍属于手工运行验收，不会被离线 doctor 伪装成成功。
进程遍历使用项目锁定环境中的 `psutil`；平台无法提供可信的 PID 探针时必须 FAIL，不得把“不支持探测”当成“没有实例”。

## 6. 手工前台运行

只有用户本人准备好观察日志时才执行：

```bash
./deploy.sh run
```

PowerShell：

```powershell
.\deploy.ps1 run
```

`run` 先执行同一套 doctor；通过后从仓库根目录 `exec uv run --frozen --no-sync python main.py`。进程保持前台，信号直接交给 Elysium。正常停止时按一次 `Ctrl+C` 并等待有序关闭。

脚本不会调用旧 lease 清理、同步任务、迁移或远端数据库 UPDATE。它也不会停止、重启或替换已经由用户启动的实例。

## 7. 备份

本地 profile：

```bash
./deploy.sh backup --output /absolute/path/to/new-snapshot
```

目标必须不存在，其父目录必须已由操作者建立且为 owner-only（POSIX `0700`；Windows 通过 owner/ACL 证明）。入口会先原子创建 owner-only 目标目录，记录其设备与 inode 身份，再让备份子进程写入；子进程返回后若目录变成符号链接、非目录或身份发生替换，整次操作显式失败。因此即使中途失败，不完整候选也不会在 Windows 上短暂继承宽 ACL。本地 Core SQLite 路径从 `core.toml` 的 `database.sqlite_path` 派生，且必须指向 `data/` 内的已存在普通文件；新部署默认为 `data/MoFox.db`。

在线备份是可验证候选，不自动取得可激活 generation 身份。只有操作者已经确认所有 writer 停止时才使用：

```bash
./deploy.sh backup --output /absolute/path/to/new-snapshot --writer-frozen
```

脚本会再次确认没有同仓库 Elysium 进程；目标已存在时拒绝覆盖。MySQL profile 使用：

```bash
ELYSIUM_MYSQL_URL='mysql+asyncmy://...' \
  ./deploy.sh backup --output /absolute/path/to/backup-directory
```

备份不会自动恢复、删除源数据或用投影替换权威历史。快照遍历会拒绝任何数据源符号链接、非普通文件或越出 `data/` 的路径，不会把仓库外内容解引用进备份。本地快照在复制主体与其他权威字节前先收紧输出目录；MySQL 备份则在写入密码、压缩流和 manifest 前分别以排他方式建立空文件并收紧 ACL，密码不进入子进程参数、子进程环境或错误输出。任一安全边界无法证明时都 fail-closed。恢复和 generation 激活必须遵循 [MySQL 迁移、备份与恢复手册](./mysql_migration_and_backup.md) 与 [生命域存储快照运行手册](./life_storage_backend_runbook.md)。

## 8. 可选能力

基础配置默认关闭 Ayla、飞书、KOOK、NapCat adapter、Emoji、N.E.K.O Surface、SkillManager、系统命令、遗留 TTS、Voice Live、直播和狼人杀。AstrBot sister bridge 的 manifest 也默认关闭，因而在导入入口前就会从加载计划排除。SkillManager 即使显式启用，脚本执行仍默认关闭。启用任一能力前：

1. 阅读该组件架构与运行手册；
2. 配置最小权限凭据与固定端口；
3. 核对独立服务 owner 和现有 PID；
4. 将对应 `config/plugins/<name>/config.toml` 的开关改为 `true`；
5. 再运行 doctor，并由用户手工启动 Elysium 做真实链路验收。

NapCat/QQNT 可由各自明确的自动恢复 owner 管理；本地 New API 是模型基础设施，必须由它自己的独立 owner 保持自动启动并核对唯一 PID、监听端口与健康状态；本脚本不获得该外部进程的控制权。两者都不得把 Elysium 纳入 restart loop，详细生命周期边界见[活体记忆迁移与健康检查](./living_memory_migration.md)。可选插件依赖必须先加入 `pyproject.toml` 与 `uv.lock`；生产启动禁止临时 `pip install` 或运行时 `uv pip install`。

## 9. 验收与回退

脚本完成只代表 `offline_validated`，不代表 `runtime_accepted`。用户手工启动后至少核对：

- Core HTTP 确实绑定配置端口，未发生“端口失败但主进程继续”；
- 模型路由快照与配置一致，Provider 真实请求成功；
- Life Engine 主体权威、事件账本、Presence 和选定存储均成功初始化；
- 只验收当前明确启用的平台和能力；
- 一次 `Ctrl+C` 后所有 owned 资源完成有序关闭。

`bootstrap` 不覆盖文件，因此配置回退是保留原文件并人工比较新示例。依赖回退必须恢复匹配的 `pyproject.toml` 与 `uv.lock` 后重新执行 `bootstrap`，禁止单独降级 site-packages。任何主体或数据库恢复都必须从可信快照无损执行并保留审计。
