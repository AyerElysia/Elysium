# Elysium Console 页面迁移矩阵

> 日期：2026-08-02
> 原则：先接入、后原生化；稳定 Shell 不依赖具体插件名；原地址在迁移期保持兼容。

| 当前能力/页面 | 当前形态 | Stage 1 接入 | 目标形态 | 目标阶段 | 关键验收 |
|---|---|---|---|---|---|
| Voice Live `/voice-live/` | 独立原生页面 | Console 原生 Ready + 现有会话桥接 | 全原生旗舰体验 | Stage 2 | 麦克风授权、可打断、Qwen Realtime、Seed-VC、错误恢复、真实通话 |
| Voice Live OBS overlay | 独立 URL | 保持 link/overlay | 独立只读 overlay | Stage 2 | OBS Browser Source、透明背景、observer token、无 Owner 信息 |
| Livestream `/livestream/` | 浏览器默认控件为主 | 受控 embedded | 原生直播控制台 | Stage 3 | OBS、弹幕、画面、音频和意识检查统一 |
| Memory Health `/api/v1/admin/memory` | 正式只读 API | Console 原生状态卡 | 连续性、Witness、Recall 与投影诊断 | Stage 3 | content-free、权限、分页、无旧图写语义 |
| Message Timeline `/message_timeline/` | 独立时间线 | 同源 embedded | 原生 dashboard | Stage 3 | 生命事件语义、分页、时间轴状态、旧 URL 兼容 |
| LLM Inspector | 独立诊断工具 | link/diagnostic | 受控 embedded diagnostic | Stage 3 | Owner-only、脱敏、无 prompt/secret 外泄 |
| Minecraft | Bridge + 多条控制链路 | 原生入口 + readiness；细节页暂保留 | 原生具身体験中心 | Stage 3 | 仿生/Agent 双路径、意图归属、OBS、服务器、安全停机 |
| Werewolf / Art Studio 等后续插件 | 可能动态出现 | contribution 驱动 link/native | 按体验复杂度选择 | Stage 4+ | 安装即出现、移除即降级、无需改 Shell 导航 |

## 迁移约束

1. 迁移不删除现有页面，除非新页面完成等价全链路验收并有回滚方案。
2. 所有入口来自能力注册表，Shell 源码不得追加插件专用导航判断。
3. embedded 是过渡手段，不是免审计通道；同源、sandbox、权限和错误边界必须明确。
4. 旗舰体验优先 native，因为它们需要统一媒体状态、实时事件、错误恢复和无障碍交互。
5. 旧 URL 至少保留一个发布周期，并记录访问量后再决定重定向或移除。
6. 每个迁移项都必须覆盖：正常、缺配置、插件停用、加载失败、权限拒绝、网络中断和恢复。

## Stage 1 最小切片

- Shell：此刻、动态导航、全局状态、Feature Unavailable；
- BFF：bootstrap、capabilities、readiness、plugins、config status、events；
- Voice Live：准备页原生化，真实会话页先复用现有稳定链路；
- 旧页面：保持原 URL，只把经过审计的页面以 embedded/link 注册；
- 测试：contract、路由、错误边界、390/768/1280 响应式、键盘、CSP 与 secret redaction。
