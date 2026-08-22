# TTS 语音合成

> 文档状态：当前生产边界（2026-08-20）。
> 当前消息 TTS：微调 **IndexTTS2.5 + vLLM-Omni**。
> `config/models.toml` 不再存在 `tasks.tts`；MiMo TTS 也不属于当前消息语音链。

## 1. 定位

TTS 只把爱莉已经决定表达的文字变成声音。它不替她决定说什么、何时说，不拥有独立人格、记忆或情绪权威。

需要区分四种场景：

- **普通消息 TTS**：Life Chatter 的 `life_send_voice`，以及非 Life Chatter 的通用 TTS Action，消费本地消息 TTS Service；
- **N.E.K.O Surface 自动语音**：文字回复由 Surface Adapter 自动交给本地 TTS Service，并按回复顺序播放；显式 TTS Action 必须被抑制，避免一条回复生成两份声音；
- **直播 TTS**：直播运行时拥有独立的有界 HTTP 客户端、切句、内容寻址音频与舞台播放回执，可配置本地端点，但不复用聊天动作的发送语义；
- **Voice Live**：持续听说、停顿与打断的实时意识实例，不是“聊天文本再接一次 TTS”。

这些链可以使用本地模型，但场景责任不同。当前普通消息和 Surface 自动语音共享 `tts_voice_plugin:service:tts`；本机生产部署把该服务指向 vLLM-Omni 的 OpenAI-compatible speech API。直播仍由自己的客户端与配置负责，不能把直播播放成功当成消息平台发送成功。

## 2. 当前消息表达链

```text
爱莉意识实例形成最终表达
  → life_send_voice(text=..., voice_style=..., text_language=...)
  → tts_voice_plugin:service:tts
  → 短文本：一次 /v1/audio/speech
  → 长文本：语义片段有界并发 /v1/audio/speech
  → 按原序号归位 → PCM 拼接 → 一次最终编码
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

### 2.1 长文本是一个表达，不是多次表达

IndexTTS2.5 在较长连续输入下容易累积韵律和稳定性压力。消息 TTS 因而允许把一条长表达在 Service 内按段落、句号、问号、感叹号、分号、冒号、逗号的优先级拆成有界片段。没有自然边界且单段仍超限时，才使用稳定的技术硬边界。

这个拆分只属于音频合成运输层：

- 上游仍只产生一次 `life_send_voice`，正文、思考和 trajectory 不被拆成多条；
- vLLM-Omni 模式允许片段有界并发，由两阶段运行时批处理；历史兼容后端仍串行；
- 后端完成顺序不得改变表达顺序，结果按稳定片段序号归位后才能拼接；
- 每段先取得 WAV，按原有边界加入可配置静音，再拼为连续 PCM；空间效果只对完整音频应用一次，平台格式也只编码一次；
- 只有全部片段成功后才返回一个 Base64 音频并发送一条平台消息；任一段失败、解码失败、采样率不一致或最终编码失败，整条表达都失败；
- `max_text_length` 是完整表达上限。超限时显式拒绝且不调用后端，禁止过去的静默截断；
- 日志只记录片段数、字符数、近似单位和失败片段序号，不记录每段正文。

片段预算是工程近似值，不冒充 IndexTTS tokenizer，也不参与主体语义判断。中日韩字符大致按一个单位，连续拉丁字母和数字约四字符一个单位；精确阈值和各级停顿都由 `[tts]` 配置统一管理。

IndexTTS2.5 的标准 vLLM-Omni 配置不是即时音频分块流式：Stage 0 先完成当前片段的语义序列，Stage 1 再产生音频。因此项目把加速边界放在“句段级有界并行”，而不是把半段音频提前冒充一条已经完成的表达。默认并发为 2，硬上限为 4；生产值必须依据端到端延迟、RTF、显存峰值与段间韵律一致性实测，不能仅凭吞吐提高就扩大。

## 3. 本地服务与生命周期

部署配置位于被 Git 忽略的 `config/plugins/tts_voice_plugin/config.toml`。当前本机配置至少明确：

- 本地服务地址；
- `backend=vllm_omni`、served model name、IndexTTS2.5 native bundle 与启动命令；
- `default` 风格与参考音频；
- 请求、启动等待与文本长度上限；
- 长文本片段预算、并发上限与标点停顿；
- 插件自有后端的 `idle_shutdown_seconds`，默认 1800 秒，0 表示常驻。

TTS 后端只在真正请求语音且端口未就绪时按配置启动。插件只持有自己通过 `start_command` 创建的新进程组；若端口上已有外部服务，插件只消费、不接管。最后一条完整表达结束后，插件为自有进程建立单调时钟闲置计时；新请求会取消旧计时。计时到期后必须先取得完整表达合成锁，并复核进程 identity 与 owner 未变化，才关闭两阶段进程组并释放显存。下一次请求继续通过现有 single-flight 自动拉起。这个生命周期不得启动、停止或重启 Elysium、NapCat，也不得注册操作系统自启动。

闲置关闭使用完整进程退出，而不是依赖当前 IndexTTS2.5 Stage 1 未启用的 vLLM sleep mode。代价是下一次语音承担冷启动；本机 `startup_timeout` 必须覆盖实测最慢冷启动。长合成正在执行、计时已被新活动取消、进程已被替换或服务并非插件所有时，旧计时一律 no-op。

仓库中的 Service 保留 `legacy_compat`，只用于显式回退；生产 `vllm_omni` 不调用权重切换端点，不发送本地文件路径，而是使用 `model/input/response_format/speed/ref_audio/extra_params`。参考音频在一条表达内只编码一次，各片段共享同一不可变 data URL；配置命名音色后可避免重复上传。后端身份以 `/v1/models`、模型 bundle revision 和输出验证为准。

官方 IndexTTS2.5 recipe 使用两阶段部署：Stage 0 为 AR talker，Stage 1 为 EnhancedCodec + S2Mel CFM/DiT + BigVGAN，输出 22.05 kHz mono WAV。Stage 0 使用 vLLM sampling，而不是上游默认 `num_beams=3`；因此迁移验收比较音色、发音、速度、韵律与稳定性，不要求逐样本波形一致。

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
9. 长文本计划保持清洗后正文顺序与标点，每段满足预算，最终只有一个可解码音频；
10. 中间任一片段失败时不产生部分音频，超出完整表达上限时不静默截断；
11. 两个并发完整表达不会在同一消息 TTS Service 内交错调用 IndexTTS。
12. vLLM-Omni 使用 `/v1/models` 健康检查和 `/v1/audio/speech` 合成，请求中不存在调用端本地路径；
13. 并发 1/2/4 分别记录端到端耗时、RTF、显存峰值和段间一致性，只有 2 或 4 在质量不退化且无 OOM 时才可成为生产值。
14. 闲置关闭只命中同一插件自有进程；新活动重置期限，长合成不被中断，陈旧 timer 不关闭 replacement，插件卸载不遗留 timer；关闭后下一次请求可重新拉起。

部署步骤见[部署、配置、测试与使用说明](../operations/deployment_and_usage.md)。
