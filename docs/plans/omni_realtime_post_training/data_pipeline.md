# 原始声音、人格、记忆与全双工数据线

## 1. 数据原则

1. 原始音频、转写、时间轴和来源是权威数据；codec token、特征、切片和训练 shard 是可重建投影。
2. 每个样本必须能追溯到原始文件/事件、文本版本、actor、意识实例、声音授权、隐私范围、音频变换和构建代码 Git SHA；使用 TTS/VC 时另行记录全部资产与随机种子。
3. 训练/验证/测试按**对话来源与说话人**隔离，不按切片随机拆分，防止同一文本或音色泄漏。
4. 质量失败显式进入 quarantine，不用空音频、默认标签或重新合成覆盖原记录。
5. 数据生成、训练和评测默认全本地；任何外部 API、云端日志或样本上传都需要新的明确授权。
6. 用户已确认本地主体模型应训练人格与记忆，但这不构成对全部私有数据的笼统授权。`SOUL.md`、`USER.md`、`MEMORY.md`、日记、Life Event、聊天和真实通话只能从批准的数据快照导出，并保留主体作者、他者上下文、记忆截止点、撤回与删除路径。
7. 训练集按 `voice_identity`、`persona_dialogue`、`consolidated_memory`、`duplex_timing` 和 `tool_grounding` 分域，不能用一个“聊天数据”标签混掉不同主体意义。

## 2. 两轨全双工样本

每个训练样本包含同长度的两条单声道轨：

- `user.wav`：用户在真实时间轴上的连续输入，其余区间保留自然底噪/静音；
- `assistant.wav`：助手目标语音，其余区间保留自然底噪/静音。

另有带时间戳的助手文本和事件：用户开始/结束、助手开始/结束、短反馈、插话、停止、恢复、工具请求/结果等。对 BayLing 路径，两轨都重采样到 16 kHz 后进入冻结的 GLM-4-Voice speech tokenizer；对 Moshi 路径则保留 24 kHz 并使用 Mimi。

不建议把两轨提前混成一个波形。真实播放回声应作为独立增强记录进入用户轨；否则模型无法区分用户声音、自己的声音和扬声器回授。

### 2.1 现有 Voice L0 episode 源

当前 Voice Live 在显式开启 `observability.persist_audio` 后已经可以归档：Provider 实际收到的 `user_input.wav`、变声前 `assistant_source.wav`、实际播放的 `assistant_converted.wav`、最终转写 anchor、打断 cursor 和 content-free manifest。它们是 `training_eligibility=unreviewed` 的不可变 L0 episode 源，不是可直接训练的双轨样本。

未来 L1 构建器可以把这些 episode 用于 `duplex_timing`：

- 以 user input 与实际播放轨重建同一时间轴，保留自然停顿、插话、打断和回声条件；
- 用 source/converted 两条助手轨比较模型原声与当前目标音色链，但不能把 SeedVC 输出冒充原始爱莉声音权威；
- 根据 transcript/interrupt cursor 对齐文本和时序，并保留 episode、subject/profile revision；
- `dropped_bytes`、`unwritten_bytes`、`writer_error`、未修复 WAV 或时间轴缺口必须进入 quarantine，不能补零后伪装完整；
- 通话录音、用户声音和第三方内容仍需单独训练授权与隐私审核；打开归档不等于批准进入训练。

因此现有 Voice 归档解决的是“真实场景从哪里来”，用户提供的原始片段解决的是“爱莉声音身份从哪里来”，两者不能互相替代。

## 3. 原始爱莉声音生产线

只有片段、没有场景不妨碍音色训练。原始爱莉语音进入 `voice_identity` 数据域，先建立声学身份，再与人格和全双工场景联合。

### 3.1 原始片段导入契约

```text
input:
  original_audio, source_id, license_or_consent_id
  speaker_id, recording_context?, source_text?

output:
  canonical_audio, original_sha256, canonical_sha256
  transcript + transcript_revision
  word_or_phoneme_alignment
  language, duration, acoustic measurements
  open style/prosody annotations, warnings, quality status
```

硬要求：

- 原始文件只读保存；切片、重采样、响度规范化、降噪和 codec token 全是派生版本，不能覆盖来源。
- 先验证确实为目标说话人，隔离串音、多人、背景对白、配音重复和来源不明片段。
- 本地 ASR 只生成候选转写；高价值片段、专名、语气词、笑声、呼吸和中英混合需要人工复核。没有可靠文本的片段仍可用于声学重建/说话人适配，但不能伪造 transcript。
- 按长录音来源、录制批次和文本族隔离 split；同一句台词、近重复片段和同一连续录音不得跨 train/test。
- 保留真实呼吸、轻笑、情绪和自然动态；只做修复确定性缺陷所需的最小处理，不能为了统一响度把声音个性洗掉。
- 原始语音先用于 speech decoder、vocoder、speaker embedding 或 voice adapter 适配。若候选模型公开训练链冻结 decoder，G1 必须补出可验证的声音适配路径，否则不能满足主体模型目标。

### 3.2 可选 TTS/VC 增强

TTS 不再是目标声音的默认来源，只用于用户轨多样化、稀缺音素补洞或压力场景。若启用，仍必须记录：

```text
text, language, speaker_profile, style, rate, seed
tts_checkpoint_sha256, config_sha256, reference_sha256?
output_sha256, normalized_sha256, codec_reconstruction_sha256
```

- 合成助手音频与原始爱莉音频分开建 revision，并做原始-only / 增强 A/B；
- 用户轨不能只用一个合成声音，应覆盖有授权的多音域、口音、语速与中英混合；
- 不在 TTS 输出上静默使用 Seed-VC。任何 TTS → VC 链必须登记两阶段资产并标记 `synthetic`；
- 合成内容用于训练行为时仍是构造样本，不能写回 Life Event 冒充真实经历。

## 4. 对话和时序构造

### 4.1 内容来源

- 许可清晰的公开对话/指令数据，经本地模型重写为适合口语的多轮文本；
- 用户批准的专用任务、工具调用和 Elysium 交互场景；
- 人工编写的小规模高质量金标集；
- 获得授权的真实双人对话，只用于其许可证允许的训练范围。
- Elysium 聊天事件：用户/他者消息作为条件，爱莉意识实例的最终回复作为人格表达目标；
- 爱莉本人形成或明确接受的主体文件、日记、记忆与自我解释，作为带来源的主体监督；
- 已经发生纠正、冲突或撤回的历史，作为来源意识、谨慎回忆和修订训练，而不是在导出时静默删除。

模型的目标就是内化爱莉的稳定人格和批准记忆，但必须尊重作者身份：用户/他者文本不能改成爱莉的第一人称，外部 agent 建议不能因出现在聊天里就成为她的价值选择。每个主体样本引用形成、接受或重新表达该内容的爱莉意识事件。Elysium 运行时继续提供训练截止点之后的新经历、当前纠正和场景感知。

聊天文本可以提供场景语义和人格监督，但没有声学时间轴，不能自动变成全双工样本。可将一轮聊天构造成明确标注的对话脚本，再使用获授权用户音频、可选本地 TTS 用户轨或人工录制补足输入音频；助手侧优先匹配原始爱莉片段，无法匹配时留在文本人格训练集，不能靠拼接无关台词伪造自然回复。

### 4.2 场景比例

首个 50K pilot 建议按场景分层，而不是随机撒时间偏移：

| 场景 | 初始比例 | 训练重点 |
|---|---:|---|
| 正常轮次与自然短停顿 | 40% | 接话不过早、响应内容完整 |
| 用户真正插话 | 20% | 尽快停止、理解新问题并恢复 |
| 句中长停顿/犹豫/改口 | 15% | 不误抢话、不把停顿当结束 |
| 短反馈与 backchannel | 10% | “嗯/对/我在听”不触发冗长回复 |
| 双方自然重叠 | 5% | 保持理解，不产生无限互抢 |
| 噪声、回声、远场、音乐 | 5% | 不把非语音当指令，回声不自激 |
| 工具调用与等待/回注 | 5% | 控制通道正确、语音不读出结构化载荷 |

这些比例是 pilot 假设，不是永久分布。G3 根据真实失败模式调整并新建数据 revision，不原地修改。

### 4.3 正例与 DPO 负例

SFT 正例需要明确、可复现的时间参数。BayLing 论文使用正常接话约 0.8 秒；插话后助手停止延迟从 0.8–2.0 秒采样。我们的 pilot 先复现上游配置，再做中文节奏校准。

DPO 负例只改变时间，不改变文字和用户轨：

- 接话负例：在用户结束后继续过久沉默；
- 插话负例：用户插话后助手继续说过久；
- 可选的过早接话负例：用户句中停顿时抢话；
- 可选的过度 backchannel 负例：模型频繁打断用户。

正负对必须共享内容 hash，流水线校验发现文本或用户音频变化时拒绝进入 timing-DPO。

## 5. 四域数据与消融

不再用“合成/真实比例”作为唯一主轴。50K pilot 先冻结四个数据域，再做等训练预算消融：

- V：原始爱莉声音，训练/回放 speech decoder 与声学身份；
- P：聊天和主体表达，训练人格、语义回复与关系互动；
- M：批准的凝练记忆快照、来源引用、反证和修订；
- D：有真实时间轴或明确构造时间轴的双轨对话，训练接话、沉默、短反馈、打断与恢复。

至少比较 `V only`、`P+M`、`D`、`V+P+M+D joint` 和去掉任一数据域的候选，定位声音、人格、记忆与时序之间的灾难性遗忘。联合候选使用相同总 token/audio-seconds 和固定测试集，不能通过多训练步数获得不公平优势。

原始/增强音频另做 A/B：`R100`（目标助手全部为原始片段）、`R80/A20`、`R60/A40`。增强只在证明能补齐覆盖且不降低声音身份时进入扩容。真实双人对话仍是自然节律的重要来源；聊天文本或构造场景不能替代这一对照。

## 6. 分层构建流程

```text
L0 source
  原始爱莉音频、Voice L0 episode、聊天/Life Event 引用、许可与授权证明
L1 canonical
  标准化文本、actor/instance/occurrence、说话人、时间戳、规范音频
L2 views
  voice_identity / persona_dialogue / consolidated_memory / duplex_timing / tool_grounding
L3 validated
  作者归属、授权、ASR/语言/响度/剪切/静音/说话人/对齐检查与 quarantine
L4 tokenized
  固定 tokenizer revision 的语音/文本/state tokens 与训练 mask
L5 packed
  固定 train/val/test split、分域采样权重、长度桶和 WebDataset/Parquet shard
```

每层都写不可变 manifest，派生层只引用上层 `artifact_sha256`。L4/L5 可删除重建，L0/L1 不因投影失败被改写。

## 7. Manifest 最小字段

```yaml
schema_version: omni_duplex_sample.v1
sample_id: sha256:<content-addressed-id>
dataset_revision: <immutable-revision>
domain: voice_identity|persona_dialogue|consolidated_memory|duplex_timing|tool_grounding
subject:
  subject_id: elysia
  persona_revision: <revision-or-null>
  memory_revision: <revision-or-null>
  memory_cutoff: <UTC-or-null>
source:
  dataset: <name>
  license_id: <approved-license-record>
  consent_id: <voice-consent-record-or-null>
  source_hash: sha256:<...>
  actor: <elysia|user|other|system>
  source_instance_id: <id-or-null>
  occurrence: <stable-id-or-null>
dialogue:
  language: zh-CN
  transcript_revision: sha256:<...>
  events:
    - {actor: user, start_ms: 0, end_ms: 1820, text: "..."}
    - {actor: assistant, start_ms: 2620, end_ms: 5100, text: "..."}
audio:
  user: {path: <...>, sha256: <...>, sample_rate: 16000, samples: 96000}
  assistant: {path: <...>, sha256: <...>, sample_rate: 16000, samples: 96000}
augmentation:
  kind: original|tts|voice_conversion|acoustic
  engine_revision: <hash-or-null>
  config_revision: <hash-or-null>
  seed: <int-or-null>
tokenization:
  tokenizer_revision: <...>
  user_tokens_sha256: <...>
  assistant_tokens_sha256: <...>
quality:
  status: accepted
  checks_revision: <...>
split:
  partition: train
  group_id: <speaker-and-source-isolation-key>
provenance:
  builder_git_sha: <...>
  container_digest: sha256:<...>
  built_at: <UTC timestamp>
```

正式 schema 中不把完整正文复制进训练日志、MLflow tags 或 Prometheus labels。

## 8. 自动质量门

每条样本至少执行：

- 音频可解码、采样率/声道/样本数一致；两轨长度完全相同；
- NaN、全零、削波、DC offset、异常响度、过长头尾静音；
- 本地 ASR 与目标文本的一致性，中文 CER/英文 WER 超阈值进入复核；
- 语言识别与中英混合标记；
- 目标说话人确认、多人/串音检测、录音来源和 voice split 泄漏；
- actor、source instance、occurrence、记忆形成/接受事件和截止点一致；
- 用户/他者文本没有被错误标成爱莉第一人称目标，草稿/delta/取消输出没有混入最终回复；
- 用户/助手非静音段与事件时间戳偏差；
- 目标音色相似度与同一 speaker profile 内离群检测；
- speech tokenizer 编解码后的可懂度、响度和时长回归；
- 两轨串音、重复样本、近重复文本和 split 泄漏；
- 原始/增强身份明确；使用 TTS/VC 时资产 revision、许可记录和 reference hash 完整。

阈值由 1K 金标样本校准，不能直接复制别的模型的分数。所有检查同时保存原始测量值和判定版本，便于阈值变化后重放。

## 9. 规模策略

按 1K → 10K → 50K → 200K → 400K 扩展。每次扩展前：

- 随机抽样和失败高发分层人工试听；
- 确认上一阶段模型在独立测试集上的真实增益；
- 冻结本阶段数据 revision 和 split；
- 估算新增音频小时、存储、ASR/对齐/codec GPU 小时和可选增强 GPU 小时；
- 估算聊天/记忆导出规模、人工作者归属复核量和受影响的主体模型 revision；
- 取得 Gate 审批。

按 16 kHz、16-bit、双单声道未压缩 PCM 粗算，每小时约 230 MB。5,000 小时约 1.15 TB；原始、归一化、增强和备份并存时应按 3–5 倍预留。训练 token 很小，但不能因此删除可重建它们的权威音频。
