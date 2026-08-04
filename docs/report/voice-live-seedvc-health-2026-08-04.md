# Voice Live × Seed-VC 音质与实时性审计

> 范围：当前 Windows Seed-VC 服务、Qwen-Audio 下行样本、爱莉参考池与 Voice Live 转换契约。
> 边界：审计和代码验证没有停止、重启或替换正在运行的 Seed-VC/Elysium 实例。

## 1. 已确认根因

当前 checkpoint `DiT_uvit_tat_xlsr_ema.pth` 的 SHA-256 为
`c853ea578b409f625f961bcb15d5cff1f8ef9a75f3209ec21d9b7c73ab422e88`，
与官方 `Plachta/Seed-VC` 文件一致。当前实时链没有加载旧的爱莉专属微调模型，
因此“实时效果差”不能归因于旧微调。

旧参考 WAV 虽长 25.28 秒，但 realtime `custom_infer()` 只读取开头
`max_prompt_length=3.0` 秒。这三秒的有效语音帧比例约为 64.3%，频谱平坦度约
0.1376；“总时长足够”掩盖了真正送入模型的短提示质量。

`C:\Temp\Data\data_v2` 已包含经过 VAD、-20 LUFS 归一化和峰值限制的参考池。
推荐首个 A/B 候选为 `elysia_neutral_02.wav`：开头三秒有效语音帧约 89.0%，
频谱平坦度约 0.0135，且无削波。另保留 `elysia_strong_02.wav` 与
`elysia_strong_05.wav`，用于判断更强表达是否值得牺牲通用稳定性。该选择只影响
音色提示，不替主体决定表达内容。

## 2. 旧基线实测

配置：官方 tiny checkpoint、10 steps、CFG 0.7、300 ms block、40 ms crossfade、
右上下文 20 ms、固定 seed 42。

- 1.82 秒真实 Qwen WAV，按 20 ms 墙钟节奏输入：首包 678.261 ms；总墙钟
  2.456 秒；模型 RTF 0.857；
- 同一样本无墙钟节奏输入：模型 RTF 0.846；另一次 GPU 竞争下为 0.945；
- 最近真实 episode：13 个模型块，总推理 2796.070 ms，模型 RTF 0.717，
  P95 单块 347.640 ms，已有一块超过 300 ms；
- 转换输出 RMS 比 Qwen 输入高约 3.7 dB，峰值接近 -0.09 dBFS；300 ms 边界
  跳变中位数约为输入的 7.6 倍；
- 审计时 GPU 显存为 23.2/24.5 GB，并有游戏进程并存。模型接近实时上限时，
  GPU 竞争会直接形成队列积压。

按 Seed-VC 官方说明，算法延迟下限近似 `2 × block_time + extra_time_right`。
旧配置仅算法部分即约 620 ms，与实测首包一致；它不可能通过 HTTP 微调降到
自然抢话需要的 250–400 ms。

## 3. 本轮工程修正

1. Seed-VC profile revision 由 checkpoint、preset、参考 WAV 和全部实时参数的
   SHA-256/规范化配置共同派生；health 与 session 都返回同一 revision。
2. 启动器只有在协议、profile、三项资产 SHA-256 和全部参数逐项一致时才复用端口；
   不一致时显式失败，绝不自动停止或替换旧进程。
3. Elysium 客户端按完整模型块聚合 Provider delta，减少不足一块时的空 HTTP 请求；
   flush 和 reset 保留严格尾音/打断语义。
4. health 增加有界滚动推理遥测：average、EWMA、P95、max、model RTF、实时余量，
   并区分 `warming` / `healthy` / `degraded` / `overloaded`。
5. 启动器低延迟候选调整为 8 steps、240 ms block、CFG 0、40 ms crossfade、
   -3 dB 输出余量。按官方说明，CFG 0 相比 0.7 可获得约 1.5 倍推理提速；
   最终音质与 P95 必须在用户手动重启后用同句 A/B 重新验收，不能用推算冒充实测。

## 4. 下一次人工验收门

- 用户手动停止旧 Seed-VC，并用新的手工启动脚本加载 `elysia_neutral_02.wav`；
- 测试时先关闭 GPU 密集型游戏，记录空载与真实直播负载两组结果；
- 同一句至少比较：Qwen 原声、旧参考 10/300/0.7、新参考 8/240/0；
- 必须同时看中文 ASR-CER、说话人相似度、P95 首包、块 P95/RTF、边界跳变和盲听；
- 商业候选要求块 P95 小于 block time，连续通话队列不增长，打断后旧音频零泄漏；
- 若 tiny 模型在干净参考下仍明显损伤辅音或产生电音，应停止继续压 steps，转入
  专用低延迟 VC/RVC/StreamVC 的同协议 A/B，而不是再次无证据微调。

## 5. 按 Voice 会话驻留的现场复验

协议 v3 将轻量 HTTP 服务壳与 CUDA 模型驻留分离。服务启动后、没有 Voice session
时，health 为 `status=ok`、`model_residency.state=unloaded`、租约 0，服务内部 CUDA
allocated/reserved 均为 0。鉴权探针不会加载模型。

同一现场的生命周期证据：

- 旧常驻实例退出前整卡使用 23018 MiB；退出并启动 v3 空壳后为 21659 MiB。整卡数值
  会受其他 GPU 进程影响，因此只作为旁证；
- 第一次真正冷启动耗时 10878 ms；会话持有期间服务内部 allocated 为 722383360
  bytes，整卡使用 22950 MiB；
- 删除最后一个 session 后，状态回到 `unloaded`，租约为 0，服务内部 allocated
  回落到 8688128 bytes、reserved 回落到 25165824 bytes，整卡使用 21968 MiB；
- 第二次有文件缓存的模型激活耗时 2176 ms。5.06 秒真实 WAV 完整产生 5.06 秒输出，
  首个输出（包含模型激活）为 2445 ms，22 个模型块总推理 3877.909 ms，模型
  RTF 为 0.766；会话结束后 generation/load/unload 均正确推进并再次回到
  `unloaded`；
- 该轮滚动 P95 为 204.758 ms，低于 240 ms block，但刚超过 85% 的 degraded
  预警线；说明它已具备实时吞吐余量，但在当前高显存竞争现场仍不应宣称容量充裕。

测试输出位于本机临时目录，不进入 Git。以上证明链路、实时吞吐和显存回收，不能替代
用户对音色自然度、辅音完整性和情感表达的盲听结论。
