# 🌐 跨服务器 AI 链路维护手册 (AyerElysia ↔ 4090_med)

## 1. 架构概览
本系统采用“分布式架构”，将不同服务器上的算力整合到了统一的中转站中。

*   **控制枢纽 (AyerElysia)**: 运行 `Elysia API` (中转站) 和 `Neo-MoFox` 机器人。
*   **算力节点 (4090_med)**: 运行 `Qwen3.6-27B` 本地大模型。
*   **连接桥梁**: 通过 SSH 隧道 (Reverse Tunnel) 实现加密互通。

---

## 2. 关键连接信息

### A. 算力节点 (4090_med)
*   **IP 地址**: `119.145.35.48`
*   **SSH 端口**: `2223`
*   **模型 API 端口**: `8000` (仅限本地访问)
*   **API Key**: `sk-qwen-local-elysia`
*   **模型标识**: `qwen36-27b`

### B. 连接隧道 (AyerElysia 侧)
我们在 AyerElysia 上配置了一个守护进程来维持与 4090 机器的连接：
*   **服务名称**: `qwen-tunnel.service`
*   **功能**: 将 4090 机器的 `8000` 端口映射到 AyerElysia 的本地 `8000` 端口。

---

## 3. 常用维护命令

### 检查隧道状态 (在 AyerElysia 上)
如果发现 Qwen 模型不可用，首先检查隧道是否断开：
```bash
# 查看隧道是否在运行
systemctl status qwen-tunnel

# 重启隧道
systemctl restart qwen-tunnel

# 测试本地 8000 端口是否通畅
curl http://127.0.0.1:8000/health
```

### 检查中转站状态 (在 AyerElysia 上)
```bash
# 查看中转站服务
systemctl status new-api

# 查看实时日志（排查 404/503 错误）
journalctl -u new-api -f
```

### 检查 4090 上的模型服务 (远程操作)
```bash
# 在 AyerElysia 上远程查看 4090 的服务状态
ssh -p 2223 medteam@119.145.35.48 "systemctl --user status qwen36-27b"
```

---

## 4. 配置修改指南

1.  **增加新模型**:
    *   在 4090 机器上启动新服务后。
    *   在 AyerElysia 的 `Elysia API` (端口 3000) 后台添加新渠道。
    *   渠道 Base URL 填 `http://127.0.0.1:8000`。

2.  **修改机器人配置**:
    *   修改 `/root/Elysia/Neo-MoFox/config/model.toml`。
    *   所有模型均统一指向 `api_provider = "NexusAI"`。

---

## 5. 故障排查
*   **503 Service Unavailable**: 检查 `qwen-tunnel` 服务。
*   **401 Unauthorized**: 检查 API Key 是否为 `sk-qwen-local-elysia`。
*   **无法打开 3000 网页**: 检查你个人电脑上的 SSH 隧道 `ssh -L 3000:localhost:3000 root@AyerElysia` 是否开启。

---
*文档生成日期: 2026-05-07*
*维护者: Antigravity AI Assistant*
