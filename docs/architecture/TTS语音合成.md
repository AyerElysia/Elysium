# TTS 语音合成

> 文档状态：当前生产边界。
> 当前消息 TTS Service 同时支持 **IndexTTS2.5 + vLLM-Omni**、**GPT-SoVITS api_v2** 与 **Breeze TTS 2**，由部署配置显式选择。
> 本机 2026-08-28 的 ignored live 配置是 `legacy_compat`：RVC-Boss `api_v2` `/tts`，v2ProPlus 微调权重，端口 `9880`。IndexTTS2.5 + vLLM-Omni 仍是代码合同，但本机启动命令已与当前 CLI 不兼容，不得再写成“当前生产就是 IndexTTS”。
> Breeze 当前只作为自托管可选 Provider 接入；本机生产选择仍是 GPT-SoVITS，不因 Breeze 服务在 `7862` 存活而自动切换。
> `config/models.toml` 不再存在 `tasks.tts`；MiMo TTS 也不属于当前消息语音链。
> 不得把主体语音或参考音频发到 `tts.ai-hobbyist.org` 一类公共推理平台。AI-Hobbyist `GPT-SoVITS-Inference` 与本机正在使用的 RVC-Boss `api_v2` 是同一套 `/tts` + `/set_*_weights` 合同，换 fork 不能修复缺失权重或失败后继续合成。

## 1. 定位

TTS 只把爱莉已经决定表达的文字变成声音。它不替她决定说什么、何时说，不拥有独立人格、记忆或情绪权威。

需要区分三种场景：

- **普通消息 TTS**：Life Chatter 的 `life_send_voice`，以及非 Life Chatter 的通用 TTS Action，消费本地消息 TTS Service；
- **直播 TTS**：直播运行时拥有独立的有界 HTTP 客户端、切句、内容寻址音频与舞台播放回执，可配置本地端点，但不复用聊天动作的发送语义；
- **Voice Live**：持续听说、停顿与打断的实时意识实例，不是“聊天文本再接一次 TTS”。

这些链可以使用本地模型，但场景责任不同。当前普通消息共享 `tts_voice_plugin:service:tts`；该 Service 以一个稳定接口封装 vLLM-Omni、GPT-SoVITS 与 Breeze，具体后端属于本机部署选择。直播仍由自己的客户端与配置负责，不能把直播播放成功当成消息平台发送成功。

## 2. 当前消息表达链

```text
爱莉意识实例形成最终表达
  → life_send_voice(text=..., voice_style=..., text_language=...)
  → tts_voice_plugin:service:tts
  → 可发音投影（非权威，不写回展示正文）
  ├─ vLLM-Omni：有界运输切分 → /v1/audio/speech
  │              → 按原序号归位 → PCM 拼接
  ├─ Breeze TTS 2：有界运输切分 → multipart /v1/audio/speech
  │                 → 24 kHz s16le PCM 校验并封装 WAV
  └─ GPT-SoVITS：完整表达一次 /tts
                   → 配置的后端原生 text_split_method
  → 一次最终编码
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

不同后端必须只有一个切分 owner。vLLM-Omni 与 Breeze transport 没有等价的原生整段切分回执，因此 Service 可按段落、句号、问号、感叹号、分号、冒号、逗号的优先级拆成有界片段；没有自然边界且单段仍超限时，才使用稳定的技术硬边界。Breeze 服务为单并发，片段必须串行；vLLM-Omni 可在硬上限内并发。GPT-SoVITS `api_v2` 已原生支持 `text_split_method`，Service 必须把完整表达一次性交给 `/tts`，禁止外层切分后再用 `cut5` 二次切分。

这个拆分只属于音频合成运输层：

- 上游仍只产生一次 `life_send_voice`，正文、思考和 trajectory 不被拆成多条；
- vLLM-Omni 模式允许片段有界并发，由两阶段运行时批处理；Breeze 片段按表达锁串行并复用同一份参考音频；GPT-SoVITS 每条表达只有一个请求和一次权重组合切换；
- 后端完成顺序不得改变表达顺序，结果按稳定片段序号归位后才能拼接；
- 每段先取得 WAV，按原有边界加入可配置静音，再拼为连续 PCM；空间效果只对完整音频应用一次，平台格式也只编码一次；
- 只有全部片段成功后才返回一个 Base64 音频并发送一条平台消息；任一段失败、解码失败、采样率不一致或最终编码失败，整条表达都失败；
- `max_text_length` 是完整表达上限。超限时显式拒绝且不调用后端，禁止过去的静默截断；
- 日志只记录片段数、字符数、近似单位和失败片段序号，不记录每段正文。

片段预算是工程近似值，不冒充 tokenizer，也不参与主体语义判断。中日韩字符大致按一个单位，连续拉丁字母和数字约四字符一个单位；默认外层预算为 24 单位，只约束没有原生切分合同的 transport。GPT-SoVITS 只记录完整表达的单位数、音频时长与单位/秒告警，因为其 API 没有给出可验证的内部片段回执。

### 2.2 展示正文与可发音投影

展示正文是爱莉真正选择的表达，允许保留 `～`、`……`、音符和 emoji；trajectory、消息、记忆与后训练样本均保存这份正文。TTS Service 在调用声学模型前派生一份非权威投影：剥离控制标记和不可发音符号，但保留经人工试听确认的 `～` 与 `……` 韵律；不可发音装饰符若位于两个可发音子句之间，只派生逗号级短停顿，禁止提升为句号。该投影只用于发音，禁止写回展示正文或冒充新的主体表达。

GPT-SoVITS 的 `text_split_method`、`speed_factor`、`seed`、参考音频与权重组成一份不可拆分的试听合同。生产参数不能只凭名称或最高 epoch 自动选择，必须同时用标准清晰度文本和包含真实聊天标点的文本成对试听。本机当前人工验收基线为 GPT `hiely-e25.ckpt`、SoVITS `hiely_e80_s12960.pth`、`cut5`、`speed_factor=0.95`、seed `20260826`；这属于部署验收结果，不是对其他模型后端的全局默认值。

默认风格速度为 `0.90`，用于给辅音和句内停顿保留更多声学时间；它是可配置的工程参数，不改变正文，也不能代替真实平台试听。

IndexTTS2.5 的标准 vLLM-Omni 配置不是即时音频分块流式：Stage 0 先完成当前片段的语义序列，Stage 1 再产生音频。因此项目把加速边界放在“句段级有界并行”，而不是把半段音频提前冒充一条已经完成的表达。默认并发为 2，硬上限为 4；生产值必须依据端到端延迟、RTF、显存峰值与段间韵律一致性实测，不能仅凭吞吐提高就扩大。

Breeze 的 `/v1/audio/speech` 接收可信 `instruction`、`cfg_scale`、`seed` 与成对的 `ref_audio/ref_text`，返回 24 kHz、单声道、s16le 流式 PCM。消息 TTS 仍须收到完整片段、核对响应媒体类型/采样格式/采样率并封装为 WAV 后才可进入原子拼接与平台发送，首包到达不等于整条表达已经说出口。`instruction` 只能来自部署风格配置，不能由用户正文覆盖；`seed=-1` 时由风格键与发音投影稳定派生，便于重现后训练样本。当前研究基线 `cfg_scale=3.8` 只是一组可配置起点，最终音色仍需按参考资产和真实平台链试听。

## 3. 本地服务与生命周期

部署配置位于被 Git 忽略的 `config/plugins/tts_voice_plugin/config.toml`。本机当前 live 配置明确：

- `backend = "legacy_compat"`，服务地址为 GPT-SoVITS `api_v2`（`127.0.0.1:9880`）；
- `scripts/tts/start_gpt_sovits_hiely.sh` 默认固定到人工批准的 e25/e80 检查点，先校验两份 SHA-256，再原子地刷新 `latest/` 符号链接并作为进程组 owner 监督 v2ProPlus `api_v2`；自定义权重必须显式提供路径与匹配摘要，不再自动选择“最新稳定文件”；
- `default` 及其他风格的参考音频、GPT/SoVITS 权重路径与 SHA-256；
- 请求、启动等待与文本长度上限；
- GPT-SoVITS 原生 `text_split_method`（本机验收基线为 `cut5`）与固定语义采样种子；
- 插件自有后端的 `idle_shutdown_seconds`，默认 1800 秒，0 表示常驻。

若要把消息 TTS 切回 IndexTTS2.5 + vLLM-Omni，必须先让本机启动命令与当前 vLLM-Omni CLI 对齐，再用 ignored 配置把 `backend` 改成 `vllm_omni`，并完成真实合成验收。代码支持该协议，不等于它正在服务。

若要试用 Breeze，使用 ignored 配置显式设置 `backend = "breeze_tts2"`、服务地址与独立启动命令，并为每个风格配置逐字准确的 `prompt_text`、`breeze_instruction`、`breeze_cfg_scale` 和 `breeze_seed`。健康检查必须返回 `status=ok` 与正采样率；合成端点繁忙时 Service 只在单次请求总 deadline 内等待，错误响应、空流、奇数字节 PCM 或协议头不匹配均整条失败。Breeze 模型只能按真实语音请求启动，闲置释放沿用同一进程所有权合同，不得因 Elysium 启动而常驻。当前模型及自托管输出受 BreezeBlue Research and Non-Commercial License 约束，部署前必须核对用途。

`legacy_compat` 在调用任何 `/set_*_weights` 或 `/tts` 前必须先验证完整 GPT/SoVITS 权重合同：路径存在、配置了 SHA-256、实际摘要匹配，且校验期间文件 identity 未变化。摘要按 symlink-aware identity 在进程内缓存，权重被替换后自动失效并重新校验。任一检查失败时整条表达显式失败，禁止用进程内残留的另一套 epoch 继续合成，也禁止把空摘要解释成“信任当前后端”。

`legacy_owned_startup_weights_ready` 是部署者对**插件自有启动进程**的显式声明：`start_command` 已经加载 `default` 配置指向的同一权重对。只有刚启动、仍由当前 Service 持有、文件 identity 也一致的进程可跳过第一次重复切换；同一自有进程之后只复用已经确认的 identity。端口上预先存在的外部服务永不消费该声明，仍逐次调用权重端点以避免缓存掩盖重启或陌生模型。

TTS 后端只在真正请求语音且端口未就绪时按配置启动。插件只持有自己通过 `start_command` 创建的新进程组；若端口上已有外部服务，插件只消费、不接管。WSL 启动器不得用 `exec` 把监督者交给 interop relay，否则句柄可能先退出而 API 被重新挂到 `/init`，造成“日志说已停、端口与显存仍在”的假成功。受版本控制的启动器保持为进程组 owner，等待并回收精确 API 子进程。最后一条完整表达结束后，插件为自有进程建立单调时钟闲置计时；新请求会取消旧计时。计时到期后必须先取得完整表达合成锁，并复核进程 identity 与 owner 未变化，才关闭完整进程组并释放显存。下一次请求继续通过现有 single-flight 自动拉起。这个生命周期不得启动、停止或重启 Elysium、NapCat，也不得注册操作系统自启动。

闲置关闭使用完整进程退出，而不是依赖当前 IndexTTS2.5 Stage 1 未启用的 vLLM sleep mode。代价是下一次语音承担冷启动；本机 `startup_timeout` 必须覆盖实测最慢冷启动。长合成正在执行、计时已被新活动取消、进程已被替换或服务并非插件所有时，旧计时一律 no-op。

仓库中的 Service 对 `legacy_compat`、`vllm_omni` 与 `breeze_tts2` 都提供显式协议合同。`legacy_compat` 每条完整表达只调用一次 `/tts`，由后端原生切分并最多切换一次 GPT/SoVITS 权重组合；`vllm_omni` 不调用权重切换端点，不发送本地文件路径，而是使用 `model/input/response_format/speed/ref_audio/extra_params`。Breeze 使用 multipart 字节上传，不向服务发送调用端本地路径，参考音频在一条表达内只读取一次，各串行片段共享同一不可变字节快照。后端身份以协议健康、模型/权重 revision 和输出验证为准。

官方 IndexTTS2.5 recipe 使用两阶段部署：Stage 0 为 AR talker，Stage 1 为 EnhancedCodec + S2Mel CFM/DiT + BigVGAN，输出 22.05 kHz mono WAV。Stage 0 使用 vLLM sampling，而不是上游默认 `num_beams=3`；因此迁移验收比较音色、发音、速度、韵律与稳定性，不要求逐样本波形一致。

## 4. 场景边界

### 4.1 QQ 与飞书

本地合成成功后，平台适配器再完成各自协议转换与发送。QQ/NapCat 使用 OneBot
语音记录；飞书需要可播放音频上传。只有平台返回真实成功，才算“已经说出口”。

QQ 的最终 NT-Silk 编码会显著削弱辅音所在的中高频。NapCat 出站适配器因此可为内联
WAV 派生 `qq_voice_presence_v1`：24 kHz SOXR 单声道、2.8 kHz presence 与
4.5 kHz high-shelf 补偿、2 dB headroom 和限幅，然后仍由 NapCat 执行最终
Silk 编码。它是可关闭、可复现的平台运输投影，不是新的 TTS 音色，也不修改 TTS
原件或其他平台音频。URL、已有 Silk、非 WAV 与投影失败均保持原输入。

### 4.2 直播

直播使用 `plugins/livestream` 的独立 `HttpTTSClient`、有界音频响应、切句队列和舞台回执。直播配置可以指向本地 TTS，但其 artifact、播放和重放合同不能由消息 TTS Service 代替。

### 4.3 实时通话

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
9. GPT-SoVITS 长表达只发出一次 `/tts`，payload 保持完整正文并携带原生切分方法；
10. vLLM-Omni 长文本计划保持清洗后正文顺序与标点，每段满足外层预算，最终只有一个可解码音频；
11. 中间任一 transport 片段失败时不产生部分音频，超出完整表达上限时不静默截断；
12. 两个并发完整表达不会在同一消息 TTS Service 内交错调用后端。
13. vLLM-Omni 使用 `/v1/models` 健康检查和 `/v1/audio/speech` 合成，请求中不存在调用端本地路径；
14. 并发 1/2/4 分别记录端到端耗时、RTF、显存峰值和段间一致性，只有 2 或 4 在质量不退化且无 OOM 时才可成为生产值。
15. 闲置关闭只命中同一插件自有进程；新活动重置期限，长合成不被中断，陈旧 timer 不关闭 replacement，插件卸载不遗留 timer；关闭后下一次请求可重新拉起。
16. 可发音投影不改变原始表达；标准文本与真实聊天标点文本都通过固定参数试听，重启后请求合同不漂移。
17. 配置了 GPT/SoVITS 权重时，缺文件、缺 SHA-256、摘要不匹配或切换非 200 均不得发出 `/tts`，也不得把残留权重的音频当成成功。
18. 装饰音符或 emoji 位于两个可发音子句之间时只派生稳定短停顿，尾部装饰被移除，`～`/`……` 韵律和权威正文不变。
19. 外部服务不复用进程内权重缓存；自有服务只复用绑定到同一进程与同一文件 identity 的确认状态。
20. 日志分别记录权重合同校验、server wait、weight、synthesis 与 total 毫秒数，不含正文；QQ 默认原样发送 TTS 音频，只有显式启用旧实验投影时才记录其算法、字节数和耗时。
21. WSL 监督启动器退出后，精确 API 子进程、监听端口和 GPU 资源均必须消失；只看到父句柄退出不算关闭成功。
22. Breeze 使用 `/health` 与 multipart `/v1/audio/speech`；可信指令不取自用户正文，参考音频与逐字稿成对提交，不发送本地路径。
23. Breeze 的 Content-Type、采样率、s16le 与 PCM 字节边界全部验证后才封装 WAV；409 仅在同一总 deadline 内重试，任一片段失败不返回部分表达。
24. 本机 live 配置继续保持 `legacy_compat`；可选 Breeze 服务存活、测试通过或模型已加载都不得触发自动切换。

部署步骤见[部署、配置、测试与使用说明](../operations/deployment_and_usage.md)。
