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
- 可选空间音效；
- Action 与 `/tts` Command；
- IndexTTS2 空闲卸载由后端服务自身负责。

## 配置

部署配置位于被 Git 忽略的 `config/plugins/tts_voice_plugin/config.toml`。

关键字段：

- `[plugin].enable`：是否注册 Service/Action/Command；
- `[tts].server`：本地 TTS HTTP 地址；
- `[tts].auto_start`、`server_dir`、`start_command`、`startup_timeout`：后端按需启动合同；
- `[tts].timeout`、`max_text_length`：请求上限；
- `[[tts_styles]]`：必须至少有 `default`，包含参考音频、提示文本、语言与可选历史兼容权重字段；
- `[spatial_effects]`：可选混响与卷积。

当前 IndexTTS2 包装器接受兼容请求但使用自己的调优 preset；客户端不会把历史 GPT-SoVITS 的 3～10 秒参考音频限制强加给 IndexTTS2。参考文件仍必须存在，真实能力由当前本地后端判断。

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

完整架构见 [TTS 语音合成](../../docs/architecture/TTS语音合成.md)。

## 许可

AGPL-v3.0。
