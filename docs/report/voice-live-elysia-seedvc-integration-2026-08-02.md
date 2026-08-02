# Voice Live × Seed-VC 爱莉实时音色集成与实机验收报告

> 日期：2026-08-02
> 范围：Codex 任务 `019fc1a9-9309-7a33-aadb-3b4b3cf662f9` 交付的 Seed-VC 环境、`plugins/voice_live/` 下行音频链路、前端/OBS 可观测性、契约测试与真实音频验收。
> 结论：**可行且已经真实跑通**。Qwen-Audio 的 24 kHz 流式回复现在可以经过 Seed-VC 转换为更接近爱莉参考音色的语音，再由通话页和 OBS observer 播放。当前版本已达到“可集成、可实时、可审计、可打断”的工程基线，但零样本 tiny 模型仍会损伤少量音素，不能把它宣称为最终商业音质。

## 1. 审计对象与许可结论

另一个 Codex 任务准备了以下可用资产：

- Seed-VC 工作区：`cover_work/seedvc_rt/seed-vc`；
- realtime checkpoint：`DiT_uvit_tat_xlsr_ema.pth`，约 142 MB；
- preset：`config_dit_mel_seed_uvit_xlsr_tiny.yml`；
- 爱莉参考音频：`ref_elysia_premium.wav`，32 kHz mono，25.280 秒；
- Windows CUDA Python：Torch 2.8.0 + CUDA 12.8；
- 设备：RTX 5090 Laptop，24 GB VRAM。

本地 Seed-VC `LICENSE` 是 GPLv3，Elysium `LICENSE` 是 AGPLv3。用户已明确同意使用 GPLv3 代码；AGPLv3 与 GPLv3 可以组合，本次没有许可阻碍。仍采用独立进程，理由只是：

1. Seed-VC 已有专用 Windows/CUDA Python 环境，而 Elysium 运行在 WSL；
2. 模型 OOM、CUDA 错误或崩溃不应带走意识主进程；
3. 模型可独立预热、监控、限容和升级；
4. 音频协议比把整套推理依赖导入 Elysium 更容易做超时与故障审计。

仓库内已纳管 `seedvc_stream_service.py` 与 `start_seedvc_stream.ps1`，因此交付不依赖一个未提交的临时脚本。

## 2. 架构选择

转换点位于 Provider 下行与浏览器播放之间：

```text
Qwen-Audio Realtime
  -> 24 kHz mono PCM16 delta
  -> CallSession 有界队列（每次通话独占）
  -> 带 token 的 Seed-VC HTTP/PCM session
       -> 24 kHz 输入缓冲
       -> 300 ms block
       -> XLSR content encoder + Seed-VC DiT（10 steps）
       -> 爱莉 25.28 s reference prompt
       -> SOLA 40 ms crossfade
       -> 22.05 kHz PCM16
  -> Elysium 重采样为 24 kHz
  -> Voice Live protocol v1
  -> 浏览器与只读 OBS observer
```

这保留了 Qwen 原生实时对话的理解、意图、Function Calling 和通话意识，只替换“最后听见的音色”。爱莉仍通过同一个 `ConsciousnessInstance` 决定说什么；SVC 不生成内容、不做关键词判断，也不改变工具意图。

## 3. 商业级失败语义

启用音色转换后采用 fail-closed：

- 服务不可达、token 为空、profile 不匹配或 session 分配失败：通话启动失败；
- 单块转换超时或返回非法协议：会话记录错误并失败；
- 有界队列溢出：显式结束通话，禁止无限堆积造成“几分钟前的爱莉还在说”；
- 不把转换失败后的 Qwen 原声静默播放成“爱莉声音”；
- 用户/Provider 打断：增加 generation、清空待处理旧音频、reset 远端上下文；
- 打断时已经在 GPU 推理的旧 generation 即使稍后返回也会被丢弃；
- Provider 回到 `LISTENING`：flush 最后一个不足 300 ms 的尾块；
- 停止/失败：取消转换 worker、删除远端 session、关闭 HTTP client；
- Seed-VC 服务当前容量为一个 session，与 Voice Live 默认单通话容量一致。

Bearer token 只从 `SEEDVC_STREAM_TOKEN` 读取，配置、前端、报告和 Git 中都不保存真实值。

## 4. 实现清单

### Elysium 音频链路

- `plugins/voice_live/voice_conversion.py`
  - `VoiceConverter` 协议；
  - `HttpVoiceConverter`；
  - health/profile 校验；
  - PCM16 双向重采样；
  - create/audio/flush/reset/delete 生命周期；
  - 环境变量 token 工厂。
- `plugins/voice_live/session.py`
  - 转换服务先于 Provider 就绪；
  - 有界异步队列与独立 worker；
  - generation 隔离打断前后的音频；
  - SVC 指标进入事件账本和浏览器；
  - 清理和显式失败。
- `plugins/voice_live/config.py`
  - `voice_conversion` 配置区；
  - 默认关闭，必须显式启用；
  - service URL、profile、token env、超时与队列容量可配置。

### Seed-VC 辅助服务

- `plugins/voice_live/scripts/seedvc_stream_service.py`
  - 复用 Seed-VC `real-time-gui.py` 的模型加载和 `custom_infer`；
  - 复现 realtime GUI 的上下文窗口与 SOLA；
  - 300 ms 流式块与不足整块的 flush；
  - silence gate；
  - bearer token；
  - 2 MB 请求上限；
  - 单 session 容量；
  - CUDA/model 全局锁；
  - 固定 seed 42 的跨 session 确定性；
  - 健康、设备、warmup、步数、seed 和活跃 session 指标。
- `plugins/voice_live/scripts/start_seedvc_stream.ps1`
  - 校验 Python、模型、配置、参考音频和 Seed-VC 入口；
  - 动态发现 Windows 的 WSL vEthernet 地址；
  - 端口已有兼容服务时只复用；
  - 不启动、停止、重启或监控 Elysium。
- `plugins/voice_live/scripts/e2e_voice_conversion.py`
  - 读取真实 WAV；
  - 可按麦克风墙钟节奏生产 100 ms PCM；
  - 采集与推理解耦；
  - 输出时长、RMS、SHA-256、首包、wall/model RTF 和块计数。

### 前端与 OBS

- 通话页显示当前 `voice_profile`；
- 通话页显示最近一个 SVC 块的平均推理耗时；
- OBS overlay 的只读元信息显示当前音色 profile；
- SVC 输出仍经过原有 Voice Live 二进制协议，因此 OBS Browser Source 可以直接听到变声后的爱莉，不需要捕获一个额外播放器窗口；
- 游戏画面继续用 OBS Game Capture，爱莉通话 UI/音频继续用 Browser Source，两者职责独立。

## 5. 参数对比与选择

### 10 steps 与 20 steps

同一个 Qwen 输出、同一个爱莉参考音频：

| 配置 | 模型推理时间 | 模型 RTF | CAM++ 对爱莉参考余弦相似度 | 结论 |
|---|---:|---:|---:|---|
| Qwen 原声 | — | — | 0.574 | 基线 |
| Seed-VC 10 steps（早期随机会话） | 1596 ms / 3.381 s | 0.541 | 0.729 | 可实时 |
| Seed-VC 20 steps | 3269 ms / 3.381 s | 1.041 | 0.713 | 不具备安全实时余量 |
| Seed-VC 10 steps，seed 42 | 2487–2598 ms / 3.381 s | 0.736–0.768 | 0.717 | 最终确定性候选 |

20 步不仅超过实时安全线，音色相似度也没有提升，因此最终选择 10 步。seed 42 的相同输入连续两次得到完全相同的 PCM SHA-256，避免直播期间每次重连出现不可回归的随机音质变化。

## 6. 真实 Qwen -> Seed-VC -> 爱莉音色验收

### 输入

- 文件：`qwen-audio-trusted-context-output.wav`；
- 来源：真实 Qwen-Audio Realtime + Elysium 可信场景工具链；
- 内容：`验证成功了，场景状态已更新。`；
- 时长：3.381 秒；
- 采样率：24 kHz；
- Whisper-small 回识别：`验证成功了,场景状态已更新。`；
- CAM++ 对爱莉参考相似度：`0.5739870`。

### 最终 Seed-VC 固定 seed 样例

- 文件：`qwen-audio-elysia-seedvc-seed42-a.wav`；
- 时长：3.381 秒；
- RMS：5144；
- PCM SHA-256：`b688cef9a10e703dcefdd530243b226fb923611c7f8f61e69fcc4e3f5edd52fd`；
- 12 个转换块；
- 首个变声音频：694.605 ms（第二次同条件为 650.941 ms）；
- 总墙钟：3.896 秒（第二次 3.858 秒）；
- wall RTF：1.152（第二次 1.141，包含真实 3.381 秒采集时间）；
- model RTF：0.768（第二次 0.736）；
- CAM++ 对爱莉参考相似度：`0.7165387`；
- 相比 Qwen 原声绝对提升：`+0.1425517`；
- Whisper-small 回识别：`驗證重工瓦,場景狀態已更新。`。

两次固定 seed 输出 PCM SHA-256 完全一致。音色客观相似度明显上升，句子后半段完整保留；但开头“验证成功了”仍被 ASR 误识别，人工听感也存在零样本转换带来的辅音模糊。这个结果证明链路可行，不证明当前模型已经达到最终商品音质。

### 完整 `CallSession` 实机路径

另有定向 E2E 直接构造真实 `CallSession`，使用 fake Provider 发出上述真实 Qwen WAV、真实 `HttpVoiceConverter` 连接仓库内 Seed-VC 服务、真实 conversion worker/flush/重采样/Voice Live frame 打包，再检查输出不少于 3 秒且与原 PCM 不同：

```text
1 passed in 3.56 s
voice_conversion_blocks >= 10
output: Voice Live protocol v1, 24 kHz PCM16
```

这不是只调用 Seed-VC 的离线脚本；`CallSession` 中的实际生产接线已经走通。

## 7. 自动化验收

### 代码质量

```text
ruff: All checks passed
Python service: py_compile passed
PowerShell launcher: parser passed
launcher reuse check: passed, profile=elysia, diffusion_steps=10
```

### Voice Live 全量

```text
47 passed, 1 skipped
coverage: 82.30%
statements: 2017
missed: 357
```

默认跳过项是需要真实 Seed-VC 环境的 E2E；设置环境后该项单独运行结果为 `1 passed in 3.56 s`。其中本次新增/变更的定向单元、契约、安全和 UI 测试为 `13 passed, 1 skipped`。覆盖重点包括：

- HTTP create/audio/flush/reset/delete 协议；
- bearer token 与 profile mismatch；
- Provider 音频必须经过 converter；
- 转换指标进入事件与前端；
- 打断 reset；
- 正在推理的旧 generation 音频不会在打断后播放；
- 队列溢出显式失败；
- converter 关闭；
- 通话页和 OBS overlay 的音色可见性；
- 插件树不含内联 API Key。

全量 Voice Live 覆盖率为 82.30%，超过 80% 门槛；其中 `session.py` 78%，`voice_conversion.py` 86%。测试在从当前 HEAD 创建的干净隔离 worktree 中运行，未包含并行 Life Engine 任务尚未提交的工作区改动。

## 8. 运行配置

Elysium 配置示例：

```toml
[voice_conversion]
enabled = true
service_url = "http://<current-windows-wsl-host>:17861"
token_env = "SEEDVC_STREAM_TOKEN"
profile_id = "elysia"
connect_timeout_seconds = 10.0
request_timeout_seconds = 10.0
queue_max_chunks = 64
```

启动 Seed-VC 辅助服务前设置：

```text
ELYSIA_SEEDVC_PYTHON
ELYSIA_SEEDVC_ROOT
ELYSIA_SEEDVC_CHECKPOINT
ELYSIA_SEEDVC_REFERENCE
ELYSIA_SEEDVC_CONFIG
SEEDVC_STREAM_TOKEN
```

随后手工执行 `start_seedvc_stream.ps1`。Elysium 本身仍由用户在自己的终端手工启动；本次验证没有 kill、TERM、重启、nohup 或自动拉起 Elysium，也没有修改受并行任务保护的生命周期文件。

## 9. OBS 与直播结论

可直接用于现有直播架构：

1. Qwen 返回音频后先在服务端变声；
2. 浏览器通话页与 `/voice-live/observe` 收到的已经是爱莉音色；
3. OBS Browser Source 播放该 observer 音频并显示字幕/状态；
4. 无需让 OBS 捕获 Seed-VC GUI，也无需虚拟声卡绕一圈；
5. Seed-VC 是无头服务，不会产生需要隐藏的窗口；
6. OBS 游戏画面仍使用 Game Capture，Voice Live overlay 继续保持透明、只读。

当前首包额外增加约 0.65–0.70 秒。对直播对话可接受，但若目标是接近 Gemini Live 的自然抢话感，下一阶段应把额外延迟压到 250–400 ms。

## 10. 距离“最强商业级”的差距与建议

### P0：当前可用

- 10 steps、300 ms block、seed 42；
- 单通话、独占 GPU session；
- 真实转换链路、打断隔离、队列背压、OBS 输出；
- 在通话前常驻预热 Seed-VC；
- 使用当前样例做每次模型/驱动升级的确定性回归。

### P1：音质优先

当前最大短板不是接线，而是 25 秒零样本参考 + tiny realtime checkpoint 的音素保持。建议建立合法授权的爱莉语音训练集：至少 30–60 分钟干净干声，覆盖普通话声母/韵母、轻声、笑声、气声、情绪和直播常用语；严格切分 train/validation/test，不能用同一句做挑 seed。

并行评估：

- Seed-VC v2 的 source-speaker suppression 与实时 RTF；
- 基于爱莉数据微调的专用 Seed-VC；
- 专用 RVC/其他因果低延迟 SVC 作为延迟下限；
- 保持同一套 ASR-CER、CAM++、DNSMOS/UTMOS、盲听 MOS 和首包延迟基准。

商业门槛建议：中文 CER 不明显劣于 Qwen 原声、跨 100 句 CAM++ 稳定提升、P95 首包增量小于 400 ms、连续 2 小时无队列增长/显存泄漏、打断后旧音频零泄漏。

### P2：并发与直播可靠性

- 独立 GPU worker 池与显存准入；
- session affinity；
- Prometheus 指标：队列深度、块耗时、RTF、GPU 显存、reset、失败率；
- 健康检查区分进程存活、模型预热完成和可分配容量；
- OBS 侧断线提示、应急静音和受控原声模式必须由主播显式选择，不能静默 fallback；
- 真实扬声器回灌、多人串音、网络抖动和长直播压测。

## 11. 最终判断

“把 Qwen Audio 的语音通过实时流式变声器变成爱莉声音”在当前机器上是可行的，而且已经完成真实输出、确定性回归、完整 `CallSession` 接线、打断/背压测试和 OBS 输出设计。

当前版本适合进入受控直播内测；若“最强商业级”意味着可以长期公开直播并把声音当作爱莉的稳定品牌资产，则下一阶段必须投入专用数据训练与盲听质量基准，不能只继续增加 diffusion steps。

## 12. 安全提醒

用户曾在对话中发送真实百炼 API Key。该 Key 没有写入本次代码、文档或配置，但因为已经暴露在聊天记录中，交付后仍应立即轮换。
