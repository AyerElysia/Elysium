# TTS Voice Plugin (`tts_voice_plugin`)

Elysium 的本地消息 TTS Service。它用一个稳定接口支持 **IndexTTS2.5 + vLLM-Omni** 的 OpenAI-compatible `/v1/audio/speech` 与 GPT-SoVITS `api_v2` 的 `/tts`；部署必须用 `[tts].backend` 明确选择，不能根据 URL 猜测或静默换音色。

## 责任边界

- `tts_voice_plugin:service:tts`：把已经决定表达的文本合成为 Base64 音频；
- `tts_voice_action`：供非 Life Chatter 场景显式发送语音；
- Life Chatter 使用自己的 `life_send_voice`，通过 Service 签名调用本插件，不再经过 `tasks.tts`；
- 直播与 Voice Live 有独立运行合同，不由本 Action 冒充。

TTS 不决定正文、情绪或是否表达。Service 缺失、合成失败、返回空音频或平台发送失败时必须如实失败，禁止换成陌生默认音色。

展示文本、trajectory 和记忆保留爱莉真正写出的正文。Service 只为声学模型派生一份非权威的可发音投影：移除不可发音符号，保留人工验收过的 `～` 与 `……`；不可发音装饰位于两个子句之间时只派生短停顿。投影绝不写回正文。

## 功能

- vLLM-Omni `/v1/audio/speech` 合成、健康检查与按需启动；
- GPT-SoVITS `/tts` 合成、原生 `text_split_method` 与按需启动；
- 参考音频、多风格、语言检测与文本清洗；
- vLLM-Omni 对长表达做有界运输切分；GPT-SoVITS 每条表达只请求一次并由后端原生切分；
- 可选空间音效；
- Action 与 `/tts` Command；
- 插件自有后端按最后一条完整表达结束时间做闲置释放，下一次语音按需重启；外部服务永不被接管或关闭。

## 配置

部署配置位于被 Git 忽略的 `config/plugins/tts_voice_plugin/config.toml`。

关键字段：

- `[plugin].enable`：是否注册 Service/Action/Command；
- `[tts].backend`：`vllm_omni` 或 `legacy_compat`，不根据 URL 猜测；
- `[tts].server`、`model`、`api_key_env`：vLLM-Omni 地址、served model name 与可选鉴权环境变量；
- `[tts].auto_start`、`server_dir`、`start_command`、`startup_timeout`：后端按需启动合同；
- `[tts].legacy_owned_startup_weights_ready`：仅声明当前插件自有启动器已经加载 `default` 权重对；外部服务永不消费；
- `[tts].idle_shutdown_seconds`：插件自有后端的闲置关闭时限，默认 1800 秒；设为 0 可保持常驻；
- `[tts].timeout`：每个内部合成请求的时限；
- `[tts].max_text_length`：一条完整表达的文本上限，超限显式失败，绝不静默截断；
- `[tts].long_text_split_enabled`、`segment_max_units`、`segment_min_units`：不具备原生切分合同的 transport 的外层切句开关和片段预算；默认单段 24 个近似单位；
- `[tts].segment_concurrency`：同一长表达在 vLLM-Omni 中的有界并发，默认 2、硬上限 4；GPT-SoVITS 不使用外层片段并发；
- `[tts].phrase_pause_ms`、`clause_pause_ms`、`sentence_pause_ms`、`paragraph_pause_ms`：拼接时按原标点追加的停顿；
- `[[tts_styles]]`：必须至少有 `default`，包含参考音频、提示文本、语言、速度，以及成对的 GPT/SoVITS 权重路径与 SHA-256；默认 `speed_factor=0.90`，部署值仍须试听验收；
- `[tts_advanced].text_split_method`、`seed`：GPT-SoVITS 原生切分与语义采样合同；`seed=-1` 表示随机，生产固定值必须来自成对试听。`sample_steps/super_sampling` 是 V3 参数，当前 v2ProPlus 不以它们调清晰度；
- `[spatial_effects]`：可选混响与卷积。

vLLM-Omni 模式发送官方字段 `model/input/response_format/speed/ref_audio/extra_params`，不会发送历史 `text_lang/ref_audio_path`。参考音频在一条表达开始时读取并编码一次，各并发片段共享同一不可变 data URL；也可配置预先上传的命名音色，避免每次传输参考音频。客户端不会把历史 GPT-SoVITS 的 3～10 秒限制强加给 IndexTTS2.5。

长文本拆分只是合成运输细节。对上游意识实例、trajectory、平台与记忆而言，输入仍是一条完整表达，输出仍是一条语音消息；内部片段不形成多条人格样本，也不允许分段发送。vLLM-Omni 可对片段做有限并行批处理，但结果必须按原序号归位，随后统一拼接、施加一次空间效果并编码一次；任何一段失败都会使整条语音显式失败。

`legacy_compat` 不得先由 Elysium 拆句、再由 GPT-SoVITS 的 `text_split_method` 二次切分。Service 将可发音投影后的完整表达一次性交给 `/tts`，每条表达最多切换一次 GPT/SoVITS 权重，由配置的原生切分策略维护句内韵律。因为 legacy API 不返回内部切分回执，观测只记录整条表达的单位数、时长和语速告警，不伪造内部片段指标，也不记录正文。

## 依赖与部署

插件依赖 `aiohttp`、`soundfile` 和 `pedalboard`，必须由锁定的部署入口安装：

```bash
./deploy.sh bootstrap
```

禁止在 Elysium 启动事务中临时安装依赖。vLLM-Omni 应在独立 Linux/WSL 环境中预装并完成模型验收；TTS 后端可以由一次真实合成请求按配置启动，但不得启动、停止或重启 Elysium/NapCat，也不得建立操作系统自启动。

闲置计时使用单调时钟并由项目任务管理器持有。新合成会取消旧计时；到期任务必须取得完整表达的合成锁，并复核仍是同一插件自有进程后才可关闭。关闭后下一次合成沿用按需启动 single-flight。Elysium 卸载时取消计时并回收自有进程；连接到已经存在的外部 TTS 时不建立闲置关闭任务。

本机 WSL GPT-SoVITS 使用 `scripts/tts/start_gpt_sovits_hiely.sh`。脚本默认固定到人工批准的 e25/e80 权重，并在更新符号链接或启动 API 前校验 SHA-256；自定义检查点必须同时提供匹配的 `GPT_SOVITS_*_CHECKPOINT` 与 `GPT_SOVITS_*_SHA256`，不存在“自动选最新”的质量回退。脚本保持为进程组 owner 并等待 API 子进程，避免 `exec` 后 interop relay 先结束、真实 API 被挂到 `/init` 的孤儿进程。停止成功必须同时验证子进程、监听端口与模型显存已经释放。

## 验收

至少验证：

1. Service 可解析本机 `default` 风格；
2. 固定短句生成非空、可解码音频；
3. Life Chatter 不依赖 `tasks.tts`；
4. Surface 不重复合成；
5. 空音频、超时和发送失败不记录伪成功；
6. 日志不包含合成正文、完整请求体或凭据。
7. GPT-SoVITS 长表达只产生一次 `/tts` 请求，且请求携带配置的原生切分方法；
8. vLLM-Omni 长表达的清洗后正文顺序不变、每段不越过配置预算，且只返回一份完整音频；
9. 任一内部片段失败时不返回半截音频，完整文本超限时不发生网络请求。
10. vLLM-Omni 的片段并发不超过配置上限，完成顺序变化也不改变最终正文顺序。
11. 闲置到期只关闭插件自有进程；新合成、长合成和替换后的进程不会被旧计时误杀，关闭后下一次请求可按需重启。
12. 装饰符号只改变可发音投影，不修改原始表达；固定试听文本必须同时验证标准中文与真实聊天标点。
13. GPT/SoVITS 权重对在任何网络切换前完成文件与 SHA-256 预检，任一缺失、替换或摘要不匹配都不得发出权重切换或 `/tts`。
14. 同一自有进程复用确认过的权重 identity；外部服务和 replacement process 必须重新确认。
15. WSL 启动器停止后 9880 不再监听，监督者和 API 子进程都不存在。

完整架构见 [TTS 语音合成](../../docs/architecture/TTS语音合成.md)。

## 许可

AGPL-v3.0。
