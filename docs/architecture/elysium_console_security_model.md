# Elysium Console 安全模型

> 版本：Stage 0 / Console API v1 草案
> 日期：2026-08-02

## 1. 边界与资产

Console 会聚合语音、记忆、Minecraft、直播与系统诊断，因此它不能把“本机页面”误当成天然可信。需要保护的资产包括：人格与记忆数据、实时音频、模型/平台密钥、插件配置、操作权限、OBS 展示内容以及 Elysium 主进程生命周期。

最重要的硬边界：Console 不拥有 Elysium 主进程的启动、停止、重启或自动拉起权限。必须重启才能生效的配置只呈现状态和操作说明，由用户在手工启动终端中完成。

## 2. 威胁与控制

| 威胁 | 主要控制 |
|---|---|
| 恶意或失控插件注入脚本 | UI contribution 仅允许声明式元数据；v1 不接受远程 JS URL；服务端按 JSON Schema 验证 |
| XSS 与跨页面权限提升 | 同源 CSP、模板默认转义、禁止内联脚本、每个页面贡献独立错误边界 |
| 嵌入旧页面逃逸 | 仅允许同源路径；iframe 使用最小化 `sandbox`；不授予 `allow-same-origin` 与脚本组合，除非经过单独审计 |
| CSRF 与跨站操作 | HttpOnly/Secure/SameSite 会话 Cookie；写操作要求 CSRF header；校验 Origin/Host |
| 密钥泄漏 | API 只返回 `configured`；日志、事件和 Problem detail 经过统一脱敏；浏览器永远收不到密钥值 |
| 未授权高风险动作 | RBAC + allow-list action id + 二次确认 + 审计 receipt；不提供任意命令执行接口 |
| SSE 泄漏内部事件 | 按角色投影；事件体限制字段与大小；禁止原始 prompt、音频与 secret 进入 Console stream |
| OBS 泄漏管理信息 | overlay 使用独立只读路由和短期 observer token；不共享 Owner 控制面会话 |
| 浏览器权限被悄然获取 | microphone/camera/fullscreen 必须由用户点击触发；页面加载不自动申请 |
| 插件增删导致旧入口误操作 | capability ETag、路由解析和 unavailable fallback；动作执行前再次验证 capability 仍存在 |

## 3. 部署默认值

- Console 默认只监听 loopback；远程访问必须显式启用反向代理和 TLS；
- 生产环境使用严格 CSP：`default-src 'self'`，并按媒体需求最小化扩展；
- 资源使用内容哈希，静态产物不可被运行时插件覆写；
- `frame-ancestors` 默认 `'self'`，OBS overlay 另设精确策略；
- 所有响应带 `X-Content-Type-Options: nosniff` 与合适的 Referrer Policy；
- 会话短期有效，Owner 会话不持久化到 localStorage；
- 浏览器端只保留非敏感 UI 偏好。

## 4. 页面接入等级

### native

由 Console 仓库构建、类型检查和测试，适合 Voice Live、Minecraft、直播等旗舰体验。共享设计系统和 API 客户端，但每个路由有独立错误边界。

### embedded

仅作为旧页面迁移桥梁。必须同源、声明具体 source path、限制 sandbox 与浏览器能力；不得把 Owner session 注入子页面。安全审计完成前不允许嵌入第三方 URL。

### link / overlay

独立工具和 OBS 页面保留独立 URL。普通 link 继承当前角色但不获得额外权限；overlay 使用只读、最小字段、可撤销的 observer session。

## 5. 写操作协议

每个写操作必须映射到已注册、可审计的 action id。请求包含 CSRF token，服务端重新检查会话角色、capability 当前状态、参数 schema 和并发条件，并返回 request id。危险操作在 UI 上使用明确对象与影响范围二次确认。

Console API v1 不提供：shell 命令、任意文件路径、任意 URL fetch、任意 Python/JS 执行、Elysium 进程 kill/restart/start。

## 6. 日志与隐私

- 安全事件记录 action id、角色、时间、结果、request id，不记录密钥和原始音频；
- 通话原始音频默认不落盘；转写是否进入生命记忆由生命事件策略决定；
- 用户可见错误与内部堆栈分离；前端只得到稳定 error code 和安全摘要；
- 诊断导出前再次脱敏，并清楚列出将包含的字段。

## 7. 上线前安全门

1. Schema 拒绝远程 URL、未知字段和重复 contribution id；
2. CSP、CSRF、Origin、Cookie 属性有自动化测试；
3. iframe 权限和 OBS token 做独立渗透测试；
4. 所有 secret API 的响应快照证明不存在值回显；
5. action allow-list 与角色矩阵覆盖率 100%；
6. 主进程生命周期相关接口扫描结果为零；
7. 插件卸载、崩溃、超时和协议不兼容均落到安全降级页面。
