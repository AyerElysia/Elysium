# TTS Voice Plugin (`tts_voice_plugin`)

Elysium 的本地消息 TTS Service。当前本机部署连接 IndexTTS2 包装器；该包装器暴露历史 GPT-SoVITS `api_v2` 兼容端点，因此插件中的部分字段继续保留协议兼容名称。协议名称不代表当前模型仍是 GPT-SoVITS，更不代表使用 MiMo。

## 责任边界

- `tts_voice_plugin:service:tts`：把已经决定表达的文本合成为 Base64 音频；
- `tts_voice_action`：供非 Life Chatter 场景显式发送语音；
- Life Chatter 使用自己的 `life_send_voice`，通过 Service 签名调用本插件，不再经过 `tasks.tts`；
- N.E.K.O Surface 的普通文字回复由 Surface Adapter 自动调用 Service，显式 Action 会被抑制，避免重复播放；
- 直播与 Voice Live 有独立运行合同，不由本 Action 冒充。

TTS 不决定正文、情绪或是否表达。Service 缺失、合成失败、返回空音频或平台发送失败时必须如实失败，禁止换成陌生默认音色。

## 功能

- 本地 `/tts` HTTP 合成与按需启动；
- 参考音频、多风格、语言检测与文本清洗；
- 长表达按自然句和有界片段顺序合成，再以标点停顿拼成一条音频；
- 可选空间音效；
- Action 与 `/tts` Command；
- IndexTTS2 空闲卸载由后端服务自身负责。

## 配置

部署配置位于被 Git 忽略的 `config/plugins/tts_voice_plugin/config.toml`。

关键字段：

- `[plugin].enable`：是否注册 Service/Action/Command；
- `[tts].server`：本地 TTS HTTP 地址；
- `[tts].auto_start`、`server_dir`、`start_command`、`startup_timeout`：后端按需启动合同；
- `[tts].timeout`：每个内部合成请求的时限；
- `[tts].max_text_length`：一条完整表达的文本上限，超限显式失败，绝不静默截断；
- `[tts].long_text_split_enabled`、`segment_max_units`、`segment_min_units`：长文本内部切句开关和片段预算；
- `[tts].phrase_pause_ms`、`clause_pause_ms`、`sentence_pause_ms`、`paragraph_pause_ms`：拼接时按原标点追加的停顿；
- `[[tts_styles]]`：必须至少有 `default`，包含参考音频、提示文本、语言与可选历史兼容权重字段；
- `[spatial_effects]`：可选混响与卷积。

当前 IndexTTS2 包装器接受兼容请求但使用自己的调优 preset；客户端不会把历史 GPT-SoVITS 的 3～10 秒参考音频限制强加给 IndexTTS2。参考文件仍必须存在，真实能力由当前本地后端判断。

长文本拆分只是合成运输细节。对上游意识实例、trajectory、平台与记忆而言，输入仍是一条完整表达，输出仍是一条语音消息；内部片段不形成多条人格样本，也不允许分段发送。所有片段必须依次成功，随后才会统一拼接、施加一次空间效果并编码一次；任何一段失败都会使整条语音显式失败。

## 依赖与部署

插件依赖 `aiohttp`、`soundfile` 和 `pedalboard`，必须由锁定的部署入口安装：

```bash
./deploy.sh bootstrap
```

禁止在生产启动期间临时安装依赖。TTS 后端可以由一次真实合成请求按配置启动，但不得启动、停止或重启 Elysium/NapCat，也不得建立操作系统自启动。

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

完整架构见 [TTS 语音合成](../../docs/architecture/TTS语音合成.md)。

## 许可

AGPL-v3.0。
