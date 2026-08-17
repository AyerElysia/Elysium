# TTS 语音合成

> 文档状态：当前生产边界（2026-08-17）。
> 当前消息 TTS：本地 **IndexTTS2** 兼容服务。
> `config/models.toml` 不再存在 `tasks.tts`；MiMo TTS 也不属于当前消息语音链。

## 1. 定位

TTS 只把爱莉已经决定表达的文字变成声音。它不替她决定说什么、何时说，不拥有独立人格、记忆或情绪权威。

需要区分四种场景：

- **普通消息 TTS**：Life Chatter 的 `life_send_voice`，以及非 Life Chatter 的通用 TTS Action，消费本地消息 TTS Service；
- **N.E.K.O Surface 自动语音**：文字回复由 Surface Adapter 自动交给本地 TTS Service，并按回复顺序播放；显式 TTS Action 必须被抑制，避免一条回复生成两份声音；
- **直播 TTS**：直播运行时拥有独立的有界 HTTP 客户端、切句、内容寻址音频与舞台播放回执，可配置本地端点，但不复用聊天动作的发送语义；
- **Voice Live**：持续听说、停顿与打断的实时意识实例，不是“聊天文本再接一次 TTS”。

这些链可以使用本地模型，但场景责任不同。当前普通消息和 Surface 自动语音共享 `tts_voice_plugin:service:tts`；本机部署把该服务指向 IndexTTS2 的 GPT-SoVITS `api_v2` 兼容层。直播仍由自己的客户端与配置负责，不能把直播播放成功当成消息平台发送成功。

## 2. 当前消息表达链

```text
爱莉意识实例形成最终表达
  → life_send_voice(text=..., voice_style=..., text_language=...)
  → tts_voice_plugin:service:tts
  → 本地 IndexTTS2 兼容 HTTP 服务 /tts
  → Base64 音频
  → QQ / 飞书等平台发送
  → 真实发送回执
  → Life Event / 记忆
```

`life_send_voice(path=...)` 仍可发送已有本地音频，不依赖 TTS Service。`path` 与 `text` 必须二选一。

消息链禁止再调用：

- 已删除的 `tasks.tts`；
- MiMo speech client；
- `model.toml` 或其他模型任务的静默回退；
- Surface 场景中的第二个显式 TTS 动作。

本地 TTS Service 未启用、未就绪或返回空音频时，动作必须显式失败；不能换陌生默认音色、不能把文字消息伪装成语音成功。

## 3. 本地服务与生命周期

部署配置位于被 Git 忽略的 `config/plugins/tts_voice_plugin/config.toml`。当前本机配置至少明确：

- 本地服务地址；
- IndexTTS2 工作目录与启动命令；
- `default` 风格与参考音频；
- 请求、启动等待与文本长度上限；
- 空闲卸载策略由 IndexTTS2 服务自身负责。

TTS 后端只在真正请求语音且端口未就绪时按配置启动。它可以独立按需拉起和空闲释放模型，但不得启动、停止或重启 Elysium、NapCat，也不得注册操作系统自启动。

仓库中的 Service 保留历史 GPT-SoVITS `api_v2` 兼容字段，因为当前 IndexTTS2 包装器正是通过该协议接入。协议兼容不代表当前模型仍是 GPT-SoVITS，更不代表 MiMo。后端身份以本机服务健康响应、模型目录 revision 和输出验证为准。

## 4. 场景边界

### 4.1 QQ 与飞书

本地合成成功后，平台适配器再完成各自协议转换与发送。QQ/NapCat 使用 OneBot 语音记录；飞书需要可播放音频上传。只有平台返回真实成功，才算“已经说出口”。

### 4.2 N.E.K.O Surface

Surface 的普通文字回复由 Adapter 自动合成并按回复顺序发布 `assistant.voice`。新用户轮次会取消上一轮尚未交付的语音。Life Chatter 在 Surface 上不暴露第二条语音动作；即使旧工具调用到达，执行层也必须拒绝重复合成。

### 4.3 直播

直播使用 `plugins/livestream` 的独立 `HttpTTSClient`、有界音频响应、切句队列和舞台回执。直播配置可以指向本地 TTS，但其 artifact、播放和重放合同不能由消息 TTS Service 代替。

### 4.4 实时通话

Voice Live 使用 Realtime Provider 持续处理输入输出。它不消费消息 TTS Service 拼接每轮回复。Seed-VC 若启用，只改变下行音色；当前禁用时不得占用显存。

## 5. 追溯与隐私

TTS 工程证据应记录模型/声音 revision、输入文本 hash、输出音频 hash、格式、采样率、时长、意识实例、场景、表达 ID 和真实发送/播放状态。

运行日志不得记录私人正文、完整请求体、参考音频内容或凭据。常规日志只记录字符数、风格键、语言键、音频字节数、耗时和 content-free 错误类型。

## 6. 验收

每次修改至少验证：

1. `config/models.toml` 缺少 `tts` 任务不会影响消息 TTS；
2. 固定短句可通过本地 Service 生成非空、可解码音频；
3. Life Chatter 只看到 `life_send_voice`，不会同时看到重叠的通用 TTS Action；
4. Surface 文字回复只生成一份自动语音；
5. Service 缺失、空音频、超时、取消和平台发送失败均显式失败且不产生伪成功；
6. QQ、飞书、Surface、直播分别完成自己的发送或播放验收，不能互相代替；
7. 日志不包含合成正文或完整请求体；
8. TTS 后端的启动与恢复不改变 Elysium/NapCat 生命周期。

部署步骤见[部署、配置、测试与使用说明](../operations/deployment_and_usage.md)。
