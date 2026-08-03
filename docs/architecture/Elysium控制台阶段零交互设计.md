# Elysium Console Stage 0 交互设计基线

> 状态：已验收的交互原型，尚未连接生产 API
> 日期：2026-08-02
> 原型入口：[`../prototypes/elysium_console/index.html`](../prototypes/elysium_console/index.html)

## 1. 产品定义

Elysium Console 不是传统后台，也不是插件列表。它是用户进入爱莉生活、共同体验和运行状态的统一入口。首屏先回答“她此刻怎么样、我们现在能一起做什么”，系统诊断和管理信息后置。

稳定部分由 Console Shell 负责：品牌、导航、身份、权限、错误边界、状态中心和页面布局。变化部分由运行时能力注册表提供：体验入口、页面类型、可用性、准备检查和安全操作。这样新增或移除插件时，导航不会靠人工同步，也不会留下白屏和死链接。

## 2. Stage 0 的四个关键状态

### 2.1 此刻（Home）

- 用“存在感”而不是指标墙作为第一视觉；
- 显示当前意识状态、最近活动和共同体验入口；
- 体验卡片同时表达可用性和下一步，不把内部栈暴露给普通用户；
- 主页内容未来完全由 `/console/api/v1/bootstrap`、`/capabilities`、`/readiness` 和生命事件摘要驱动。

### 2.2 Live 准备（Live Ready）

- 在请求麦克风权限之前先显示设备与链路检查；
- 阻断项明确到“缺什么”和“为什么不能开始”；
- 密钥只显示 `configured: true/false`，绝不回显值；
- 设置需要重启时，只给出说明，Console 不重启 Elysium；
- 原型中的“预览通话界面”仅用于确认交互，不会建立真实会话。

### 2.3 通话中（Live Call）

- 主视觉只保留爱莉当前是在听、在想还是在说；
- 提供静音、打断和结束三个高频动作；
- 诊断信息收进抽屉，不侵占交流中心；
- 状态控件具备 `aria-pressed`、`aria-expanded` 等可读状态；
- 将来由实时事件流驱动 RTT、SVC 延迟、意识实例和 OBS observer 状态。

### 2.4 功能暂不可用（Feature Unavailable）

- 插件停用、加载失败或协议升级时不白屏；
- 展示用户可理解的原因、最后已知状态和安全返回路径；
- 保留数据但不保留失效操作；
- 旧书签可以落到该页面，由 capability id 提供针对性诊断。

## 3. 视觉系统

### 3.1 基调

- 背景：夜空黑紫，保持低亮度和 OBS 友好；
- 主色：爱莉粉，仅用于主动作、生命状态和焦点；
- 辅色：冰蓝/薄荷绿表示健康，琥珀表示需要处理，红色只用于不可逆或中断；
- 图像：复用项目已有 `docs/assets/banner.png` 与 `docs/assets/elysia_cg.png`，不引入新的品牌依赖。

### 3.2 排版与层级

- 中文正文优先使用系统无衬线字体，关键叙事标题使用衬线字体；
- 一级内容是爱莉和共同体验，二级内容是状态，三级内容才是技术诊断；
- 桌面侧栏宽度固定，内容区使用流式网格；小于 900px 时侧栏变成四项底部导航；
- 390px 宽度下不得产生横向滚动，主动作保持至少 44px 可点击高度。

## 4. 交互规则

1. 页面入口来自已验证的 capability contribution；未注册功能不出现在导航。
2. `native` 页面进入 Console 路由；`embedded` 页面受同源和 sandbox 约束；`link` 页面在明确上下文中打开。
3. 任何浏览器权限都由用户手势触发，进入页面不自动请求麦克风、摄像头或全屏。
4. 任何写操作都要有可见的结果状态；高风险操作需要再次确认。
5. 插件退出或健康变化通过事件流增量更新；Shell 永远保留返回、错误边界与状态中心。
6. OBS 页面和普通控制页面分离，overlay 默认无操作能力、无密钥、无管理信息。

## 5. 可访问性基线

- 存在“跳到主要内容”链接；
- 所有图标按钮有可访问名称；
- 当前导航使用 `aria-current="page"`；
- 状态切换使用 `aria-pressed` / `aria-expanded`；
- Toast 使用礼貌级 live region；
- 焦点样式不可被主题隐藏；
- 遵循 `prefers-reduced-motion`，关闭装饰动画；
- 颜色不是唯一状态信号，状态同时具有文字或图标。

## 6. Stage 1 实现切片

1. 建立 Vite + React + TypeScript Shell，并由现有 Python 服务同源托管生产产物。
2. 实现 Console BFF 的只读端点：bootstrap、capabilities、readiness、plugins、config status、events。
3. 首先原生迁移 Home、Feature Unavailable 和 Voice Live Ready；现有插件页面保持原地址。
4. 引入运行时 JSON Schema 校验、权限过滤和独立错误边界。
5. 完成 Voice Live 通话页真实接线后，再进入 Minecraft 与直播控制台迁移。

Stage 0 原型中的数据均为明确的演示状态，不代表当前进程实况，也不会执行启动、停止、重启、配置写入或真实媒体连接。
