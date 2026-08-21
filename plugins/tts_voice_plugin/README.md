# TTS Voice Plugin (`tts_voice_plugin`)

Elysium 的本地消息 TTS Service。当前生产目标是微调 **IndexTTS2.5 + vLLM-Omni** 的 OpenAI-compatible `/v1/audio/speech`；历史 `/tts` 包装器只作为显式回退，不再代表主部署。

## 责任边界

- `tts_voice_plugin:service:tts`：把已经决定表达的文本合成为 Base64 音频；
- `tts_voice_action`：供非 Life Chatter 场景显式发送语音；
- Life Chatter 使用自己的 `life_send_voice`，通过 Service 签名调用本插件，不再经过 `tasks.tts`；
- N.E.K.O Surface 的普通文字回复由 Surface Adapter 自动调用 Service，显式 Action 会被抑制，避免重复播放；
- 直播与 Voice Live 有独立运行合同，不由本 Action 冒充。

TTS 不决定正文、情绪或是否表达。Service 缺失、合成失败、返回空音频或平台发送失败时必须如实失败，禁止换成陌生默认音色。

## 功能

- vLLM-Omni `/v1/audio/speech` 合成、健康检查与按需启动；
- 可显式回退历史 `/tts` 协议；
- 参考音频、多风格、语言检测与文本清洗；
- 长表达按自然句和有界片段顺序合成，再以标点停顿拼成一条音频；
- 可选空间音效；
- Action 与 `/tts` Command；
- IndexTTS2.5/vLLM-Omni 的空闲释放由后端服务自身负责。

## 配置

部署配置位于被 Git 忽略的 `config/plugins/tts_voice_plugin/config.toml`。

关键字段：

- `[plugin].enable`：是否注册 Service/Action/Command；
- `[tts].backend`：`vllm_omni` 或 `legacy_compat`，不根据 URL 猜测；
- `[tts].server`、`model`、`api_key_env`：vLLM-Omni 地址、served model name 与可选鉴权环境变量；
- `[tts].auto_start`、`server_dir`、`start_command`、`startup_timeout`：后端按需启动合同；
- `[tts].timeout`：每个内部合成请求的时限；
- `[tts].max_text_length`：一条完整表达的文本上限，超限显式失败，绝不静默截断；
- `[tts].long_text_split_enabled`、`segment_max_units`、`segment_min_units`：长文本内部切句开关和片段预算；
- `[tts].segment_concurrency`：同一长表达在 vLLM-Omni 中的有界并发，默认 2、硬上限 4；历史后端仍串行；
- `[tts].phrase_pause_ms`、`clause_pause_ms`、`sentence_pause_ms`、`paragraph_pause_ms`：拼接时按原标点追加的停顿；
- `[[tts_styles]]`：必须至少有 `default`，包含参考音频、提示文本、语言与可选历史兼容权重字段；
- `[spatial_effects]`：可选混响与卷积。

vLLM-Omni 模式发送官方字段 `model/input/response_format/speed/ref_audio/extra_params`，不会发送历史 `text_lang/ref_audio_path`。参考音频在一条表达开始时读取并编码一次，各并发片段共享同一不可变 data URL；也可配置预先上传的命名音色，避免每次传输参考音频。客户端不会把历史 GPT-SoVITS 的 3～10 秒限制强加给 IndexTTS2.5。

长文本拆分只是合成运输细节。对上游意识实例、trajectory、平台与记忆而言，输入仍是一条完整表达，输出仍是一条语音消息；内部片段不形成多条人格样本，也不允许分段发送。vLLM-Omni 可对片段做有限并行批处理，但结果必须按原序号归位，随后统一拼接、施加一次空间效果并编码一次；任何一段失败都会使整条语音显式失败。

## 依赖与部署

插件依赖 `aiohttp`、`soundfile` 和 `pedalboard`，必须由锁定的部署入口安装：

```bash
./deploy.sh bootstrap
```

禁止在 Elysium 启动事务中临时安装依赖。vLLM-Omni 应在独立 Linux/WSL 环境中预装并完成模型验收；TTS 后端可以由一次真实合成请求按配置启动，但不得启动、停止或重启 Elysium/NapCat，也不得建立操作系统自启动。

## 验收

至少验证：

1. Service 可解析本机 `default` 风格；
2. 固定短句生成非空、可解码音频；
3. Life Chatter 不依赖 `tasks.tts`；
4. Surface 不重复合成；
5. 空音频、超时和发送失败不记录伪成功；
6. 日志不包含合成正文、完整请求体或凭据。
7. 长表达的清洗后正文顺序不变、每段不越过配置预算，且只返回一份完整音频；
8. 任一内部片段失败时不返回半截音频，完整文本超限时不发生网络请求。
9. vLLM-Omni 的片段并发不超过配置上限，完成顺序变化也不改变最终正文顺序。

完整架构见 [TTS 语音合成](../../docs/architecture/TTS语音合成.md)。

## 许可

AGPL-v3.0。
