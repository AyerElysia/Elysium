# Elysium Console Stage 0 Prototype

这是架构批准后的高保真交互原型，不是正式 Console，也不会调用 Elysium API。

包含四个状态：

- `#home`：此刻首页；
- `#live-ready`：Voice Live 启动前检查；
- `#live-call`：通话中；
- `#unavailable`：插件能力消失或未加载。

原型复用 `docs/assets/banner.png` 与 `docs/assets/elysia_cg.png`，不包含真实密钥、token 或用户数据。正式实现时页面由 `/console/` 提供，导航内容来自运行时能力注册表。

在仓库根目录可用任意静态文件服务器预览，例如：

```bash
python -m http.server 18991 --directory docs
```

然后打开 `http://127.0.0.1:18991/prototypes/elysium_console/`。该预览服务与 Elysium 主进程无关。
