# Elysium 进程管理边界

Elysium 不提供 systemd、Windows 服务、cron、计划任务、登录启动项、Docker restart policy 或其他无人值守拉起方式。旧 `elysium.service`、根目录 `Dockerfile` 与 `docker-compose.yml` 已退役，因为它们会绕过用户手工前台启动、锁定依赖、主体恢复和端口 owner 检查。

唯一规范入口：

```bash
./deploy.sh bootstrap
./deploy.sh doctor
./deploy.sh run
```

PowerShell 使用 `deploy.ps1` 的同名子命令。`run` 必须由用户在可观察终端中主动执行；正常停止时按一次 `Ctrl+C` 并等待有序关闭。脚本不会停止、重启或替换既有实例。

NapCat/QQNT 可以由各自明确的生命周期 owner 自动启动或恢复；本地 New API 必须由其独立 owner 保持自动启动。不得把 Elysium 纳入任何 restart loop。完整合同见[安全部署脚本](./docs/operations/deployment_scripts.md)。
