# Elysium Console Stage 0 原型与契约验收报告

> 日期：2026-08-02
> 范围：高保真交互原型、UI contribution schema、Console API 草案、安全模型与迁移矩阵
> 结论：Stage 0 通过，可进入 Stage 1 Shell 与只读 BFF 实现

## 1. 本次交付

- 可点击静态原型：`docs/prototypes/elysium_console/`；
- 四个核心状态：`#home`、`#live-ready`、`#live-call`、`#unavailable`；
- UI contribution JSON Schema Draft 2020-12；
- Console API OpenAPI 3.1 草案；
- Stage 0 交互设计、安全模型和现有页面迁移矩阵；
- 文档总索引与已批准提案状态同步。

本次没有修改生产代码、没有读写真实密钥、没有连接真实媒体设备，也没有启动、停止、重启或自动拉起 Elysium。浏览器中的系统状态均为明确标注的原型演示数据。

## 2. 浏览器全链路验收

通过本地 HTTP 预览，在真实 Chromium 渲染环境逐页检查：

| 场景 | 结果 | 证据 |
|---|---|---|
| 此刻首页桌面视觉 | 通过 | 品牌图、存在状态、体验卡、最近事件与 readiness 正常渲染 |
| Live Ready | 通过 | 4/5 检查、缺凭证阻断、禁用主动作、修复说明均可读 |
| Live Call | 通过 | 听取状态、转写、静音、打断、结束与诊断抽屉结构正常 |
| Feature Unavailable | 通过 | 插件未注册时有原因、最后状态和安全返回路径，不白屏 |
| 交互状态 | 通过 | 诊断抽屉 `aria-expanded` 在打开后为 `true`；静音后 `aria-pressed` 为 `true` 且标签变为“已静音” |
| 键盘导航 | 通过 | 使用键盘在主导航进入 `#live-ready`，目标标题唯一可见 |
| 390×844 响应式 | 通过 | 侧栏切换为底部四项导航；Home 与 Live Ready 为单列；无横向溢出 |
| 浏览器错误 | 通过 | 四页与交互测试后 console warning/error 数量为 0 |

响应式测量结果：手机测试视口 `innerWidth=390`，文档 `scrollWidth=375`（扣除滚动条后的可用宽度），Home hero 和 Live Ready 均计算为单列；底部导航宽度等于视口且自身无溢出。测试完成后已恢复默认 1280×720 视口。

## 3. 静态与契约验证

| 检查 | 结果 |
|---|---|
| `node --check` 检查原型 JavaScript | 通过 |
| Python `HTMLParser` 解析原型 HTML | 通过 |
| JSON 原始解析 | 通过 |
| `Draft202012Validator.check_schema` | 通过 |
| 合法 native contribution 示例 | 通过 |
| embedded contribution 使用外部 `https://` source | 按预期拒绝 |
| OpenAPI YAML 解析 | 通过 |
| `git diff --check` | 提交前执行，要求零错误 |

## 4. 验收中发现并修复的问题

第一轮 Live Call 浏览器交互显示：静音按钮只弹提示，没有暴露持久状态；诊断按钮也没有向辅助技术声明抽屉是否展开。已修复：

- 静音按钮增加 `aria-pressed`，状态切换时同步按钮文案；
- 诊断开关增加 `aria-controls` 与 `aria-expanded`；
- 打开诊断抽屉后焦点移入关闭按钮；
- 浏览器复验状态值和可见面板均通过。

## 5. 架构决策落地

1. 采用稳定 Shell + 运行时 capability registry；
2. 页面贡献只允许 `native`、`embedded`、`link` 三类；
3. v1 声明协议禁止远程脚本和绝对 source URL；
4. Console API 只返回密钥是否已配置，不返回值；
5. OBS overlay 与 Owner 控制面会话隔离；
6. Console 不持有 Elysium 进程生命周期权限；
7. 插件消失、崩溃或协议不兼容统一进入 Feature Unavailable。

## 6. 下一阶段准入条件

Stage 1 可以开始实现，但生产接线必须继续满足：

- 先实现只读 bootstrap/capabilities/readiness/plugins/config status/events；
- contribution 在服务端和客户端双重校验；
- 先保留现有插件 URL，再逐个迁移；
- Voice Live 的真实连接必须覆盖麦克风拒绝、凭证缺失、provider 断线、SVC 降级和恢复；
- 任意需要重启的变更仅提示用户手工操作；
- 合并前运行生产构建、后端测试、浏览器 E2E、响应式、键盘、CSP 与 secret redaction 测试。
