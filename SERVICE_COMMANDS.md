# Elysium systemd 服务

> 服务文件位于仓库根目录 `elysium.service`。安装和启用是部署操作，仓库不会自动修改系统服务。

## 安装

```bash
sudo install -m 0644 /root/Elysia/Elysium/elysium.service /etc/systemd/system/elysium.service
sudo systemctl daemon-reload
sudo systemctl enable --now elysium
```

## 日常操作

```bash
systemctl status elysium
journalctl -u elysium -f
systemctl restart elysium
systemctl stop elysium
systemctl start elysium
```

## 更新服务文件

```bash
sudo install -m 0644 /root/Elysia/Elysium/elysium.service /etc/systemd/system/elysium.service
sudo systemctl daemon-reload
sudo systemctl restart elysium
```

## 运行语义

- 标准输入关闭后进入无交互模式，不会因 EOF 退出。
- 关闭终端产生的 `SIGHUP` 不会结束进程；停止服务使用 `SIGTERM` 触发优雅关闭。
- 只有异常退出才会在 10 秒后重启；人工 `systemctl stop` 不会自动拉起。
- stdout/stderr 统一进入 journal，应用自己的结构化日志仍由现有日志系统管理。
- `TimeoutStopSec=60` 给后台任务、数据库和日志清理留下排空时间。

## 边界

当前 unit 按仓库的实际部署位置使用 `User=root` 和固定路径。迁移目录或改用非 root 用户时，必须同时核对工作目录、模型/配置权限、Windows/WSL 桥接与私人工作区访问权限。
