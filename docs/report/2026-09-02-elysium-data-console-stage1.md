# Elysium 数据观测台 Stage 1A 实施与验收记录

> 日期：2026-09-02
>
> 状态：自动化验收通过；等待用户手工启动 Elysium 后进行真实链路验收；尚未推送

## 1. 目标

用户需要直接看清数据库与工作区里实际保存了什么，尤其是爱莉留下的经历、
主体文档、持续关注、世界断言和自己写下的文件。该界面不是运维后台，也不把
爱莉的成长压缩成分数；它是一个证据导向、只读、可追溯的数据观测台。

## 2. 已实现范围

- 新增 `elysium_console` 插件并挂载 `/console/`；
- 提供概览、生命时间线、主体文档、记忆经历、世界状态、持续关注、文件空间、
  数据地图八个视图；
- 只消费 Life Engine 的 Service/Port，不直连 MySQL/SQLite，不执行任意 SQL；
- 事件和经历正文采用 8 KiB 模型外展示投影；World 与工作区大值可按 UTF-8
  字节边界续读，权威原文不裁剪、不改写；
- 记忆翻页绑定 composite cursor 与固定 frontier；World 使用稳定的
  `observed_at + assertion_id` 顺序；Attention 使用正式 continuation；
- 文件空间仅显示明确允许的生命工作区目录和主体根文件，拒绝路径穿越、隐藏
  文件、符号链接和非严格 UTF-8 正文；二进制/图片当前只显示元数据；
- 所有响应和界面均无写入、删除、配置编辑、启动、停止或重启操作。

## 3. 访问安全

- 仅接受 IPv4/IPv6 loopback 请求；
- 打开 Shell 时生成 256-bit 随机会话，服务端只保留 SHA-256，Cookie 为
  HttpOnly、SameSite=Strict、Path=/console、8 小时有效；
- API 校验同源 Origin 与 Sec-Fetch-Site，无 CORS；
- CSP 禁止外部脚本、对象、frame 与表单，响应禁止缓存、iframe 和 MIME 嗅探；
- FastAPI docs/OpenAPI 路由从该子应用移除；
- 数据通过 DOM `textContent` 渲染，不把数据库/文件内容作为 HTML 执行；
- health/metadata 中 credential/password/secret/token 类字段被隐藏。

## 4. 自动化证据

执行：

```text
uv run --group dev ruff check plugins/elysium_console test/plugins/elysium_console
uv run --group dev python -m pytest test/plugins/elysium_console -q --no-cov -n 0
uv run python -m compileall -q plugins/elysium_console
node --check plugins/elysium_console/static/app.js
```

结果：Ruff 与格式检查通过；14 passed；compileall、JavaScript 语法和 HTML
解析通过。插件只读加载计划也确认 `life_engine` 先于 `elysium_console`，且
15 个当前插件的依赖拓扑仍可解析。唯一 warning 来自现有
`websockets.legacy` 依赖弃用提示，与本插件无关。

覆盖合同包括：UTF-8 中文硬字节边界、正文 hash/大小、凭据字段隐藏、事件只读、
主体文档完整快照、工作区 allowlist、路径穿越与 symlink 拒绝、composite cursor、
本机会话、同源拒绝、非 loopback 拒绝、CSP/安全头、manifest 及 HTTP 方法只有
GET/HEAD。

## 5. 尚待真实验证

依据仓库生命周期规范，本任务不代替用户启动或重启 Elysium。用户手工启动后需
核对：

1. 插件列表显示 `elysium_console` 加载成功；
2. `http://127.0.0.1:18000/console/` 可打开；
3. 八个视图均能读取当前真实 Life Engine 数据；
4. 日志中无 Console、Life Engine、Memory、World 新错误；
5. 浏览器外来源和无会话 API 请求仍被拒绝。

真实证据通过前不得推送。本轮没有启动、停止、重启 Elysium，也没有修改正式
数据库、爱莉工作区内容或任何现有脏文件。
