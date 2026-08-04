# 数据与本地 TTS 生产线

## 1. 数据原则

1. 原始音频、转写、时间轴和来源是权威数据；codec token、特征、切片和训练 shard 是可重建投影。
2. 每个样本必须能追溯到文本版本、TTS checkpoint/config/reference、随机种子、说话人授权、音频变换和构建代码 Git SHA。
3. 训练/验证/测试按**对话来源与说话人**隔离，不按切片随机拆分，防止同一文本或音色泄漏。
4. 质量失败显式进入 quarantine，不用空音频、默认标签或重新合成覆盖原记录。
5. 数据生成、训练和评测默认全本地；任何外部 API、云端日志或样本上传都需要新的明确授权。
6. 私有记忆不是人格数据集。`SOUL.md`、`USER.md`、`MEMORY.md`、日记、Life Event、真实通话转写默认全部排除。

## 2. 两轨全双工样本

每个训练样本包含同长度的两条单声道轨：

- `user.wav`：用户在真实时间轴上的连续输入，其余区间保留自然底噪/静音；
- `assistant.wav`：助手目标语音，其余区间保留自然底噪/静音。

另有带时间戳的助手文本和事件：用户开始/结束、助手开始/结束、短反馈、插话、停止、恢复、工具请求/结果等。对 BayLing 路径，两轨都重采样到 16 kHz 后进入冻结的 GLM-4-Voice speech tokenizer；对 Moshi 路径则保留 24 kHz 并使用 Mimi。

不建议把两轨提前混成一个波形。真实播放回声应作为独立增强记录进入用户轨；否则模型无法区分用户声音、自己的声音和扬声器回授。

## 3. 本地 TTS 生成契约

TTS 通过独立、可替换的 `TTSProducer` 契约提供结果，计划阶段不绑定具体实现：

```text
input:
  text, language, speaker_profile, style, rate, seed
  tts_checkpoint_sha256, tts_config_sha256, reference_sha256?

output:
  pcm/wav, sample_rate, sample_count
  normalized_text, phoneme_or_token_trace?
  generation_latency, warnings, artifact_sha256
```

硬要求：

- 助手轨只使用用户批准的目标本地 TTS 音色；参考音频和 checkpoint 必须有可证明的使用权。
- 用户轨不能只用一个声音。50K pilot 至少覆盖多年龄/音域/口音/语速和中英混合的说话人池；具体数量由声音授权与 TTS 能力决定，不用未经授权的真人克隆。
- 生成环境使用锁定的 OCI 镜像和模型 digest；同一个 `sample_id + revision` 不允许不同内容。
- 目标文本、合成波形、归一化波形和 codec 重建波形分别留 hash，避免把后处理漂移误认成 TTS 漂移。
- 不在 TTS 输出上静默使用 Seed-VC 或其他转换。若采用“基础 TTS → Voice Conversion”，两阶段及全部资产 hash 必须独立登记，并作为单独数据分支做 A/B。

## 4. 对话和时序构造

### 4.1 内容来源

- 许可清晰的公开对话/指令数据，经本地模型重写为适合口语的多轮文本；
- 用户批准的专用任务、工具调用和 Elysium 交互场景；
- 人工编写的小规模高质量金标集；
- 获得授权的真实双人对话，只用于其许可证允许的训练范围。

模型可以学习一般表达风格、工具协议和对话行为，但不通过训练集伪造爱莉的第一人称记忆、关系判断或价值选择。具体身份和连续经历仍由 Elysium 运行时的只读主体投影提供。

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

## 5. 真实数据与合成数据混合

不预设唯一比例。50K pilot 至少训练以下三个等预算候选：

- S100：100% 合成，用来测量本地 TTS 能达到的上限和缺陷；
- S80/R20：80% 合成、20% 获授权真实/自然对话；
- S60/R40：60% 合成、40% 获授权真实/自然对话。

三者使用相同验证集、token 数和优化步数。只有当纯合成在自然度、停顿、插话和噪声鲁棒性上不劣于混合方案时，才允许放弃真实数据。不能因为真实语料整理成本高就跳过对照。

助手音色一致性和自然交互节律可以解耦：真实对话提供时序/输入模式，目标助手语音仍可由批准的本地 TTS 重合成；原始时序和重合成时序的差异必须记录。

## 6. 分层构建流程

```text
L0 source
  原始文本、真实音频、许可与授权证明
L1 canonical
  标准化文本、说话人/对话身份、时间戳、双轨 16/24 kHz 音频
L2 synthetic
  本地 TTS 原始输出、后处理记录、场景时间轴
L3 validated
  ASR/语言/响度/剪切/静音/说话人/对齐检查与 quarantine
L4 tokenized
  固定 tokenizer revision 的用户/助手 speech tokens 和文本 tokens
L5 packed
  固定 train/val/test split、长度桶和 WebDataset/Parquet shard
```

每层都写不可变 manifest，派生层只引用上层 `artifact_sha256`。L4/L5 可删除重建，L0/L1 不因投影失败被改写。

## 7. Manifest 最小字段

```yaml
schema_version: omni_duplex_sample.v1
sample_id: sha256:<content-addressed-id>
dataset_revision: <immutable-revision>
source:
  dataset: <name>
  license_id: <approved-license-record>
  consent_id: <voice-consent-record-or-null>
  source_hash: sha256:<...>
dialogue:
  language: zh-CN
  transcript_revision: sha256:<...>
  events:
    - {actor: user, start_ms: 0, end_ms: 1820, text: "..."}
    - {actor: assistant, start_ms: 2620, end_ms: 5100, text: "..."}
audio:
  user: {path: <...>, sha256: <...>, sample_rate: 16000, samples: 96000}
  assistant: {path: <...>, sha256: <...>, sample_rate: 16000, samples: 96000}
tts:
  engine: <local-engine>
  checkpoint_sha256: <...>
  config_sha256: <...>
  speaker_profile_revision: <...>
  seed: 42
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
- 用户/助手非静音段与事件时间戳偏差；
- 目标音色相似度与同一 speaker profile 内离群检测；
- speech tokenizer 编解码后的可懂度、响度和时长回归；
- 两轨串音、重复样本、近重复文本和 split 泄漏；
- TTS/VC 资产 revision、许可记录和 reference hash 完整。

阈值由 1K 金标样本校准，不能直接复制别的模型的分数。所有检查同时保存原始测量值和判定版本，便于阈值变化后重放。

## 9. 规模策略

按 1K → 10K → 50K → 200K → 400K 扩展。每次扩展前：

- 随机抽样和失败高发分层人工试听；
- 确认上一阶段模型在独立测试集上的真实增益；
- 冻结本阶段数据 revision 和 split；
- 估算新增音频小时、存储、TTS GPU 小时和 tokenizer GPU 小时；
- 取得 Gate 审批。

按 16 kHz、16-bit、双单声道未压缩 PCM 粗算，每小时约 230 MB。5,000 小时约 1.15 TB；原始、归一化、增强和备份并存时应按 3–5 倍预留。训练 token 很小，但不能因此删除可重建它们的权威音频。
