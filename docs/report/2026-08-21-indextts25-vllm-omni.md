# IndexTTS2.5 / vLLM-Omni 消息 TTS 接入报告

> 状态：代码合同、独立服务真实模型加载、Elysium TTSService 长文本链、并发基准及完整 Life Engine 风险回归均已通过；本机 ignored 配置已切换并完成解析验证。仍待用户手工启动 Elysium 后完成私聊/群聊平台回执，因此尚不能表述为最终生产验收通过，也尚未推送。

## 1. 目标

把普通消息 TTS 从历史 `/tts` 兼容包装器迁移到微调 IndexTTS2.5 的 vLLM-Omni 两阶段服务，同时保留长文本语义分段。主体仍只提交一次完整表达；片段只是合成运输单元，最终只形成一条语音消息。

## 2. 已核对现场

- 旧服务运行于本机 `127.0.0.1:9881`，使用历史 IndexTTS2 兼容包装器；在新服务完成验收前必须保留回退能力。
- Windows 工作空间存在完整 `checkpoints_timbre` 微调 bundle；部署时必须复制完整依赖目录并排除未完成的 `.http.part` 文件，不能只复制 `gpt.pth`。
- vLLM-Omni 官方 IndexTTS2.5 recipe 使用 Stage 0 自回归 talker 与 Stage 1 codec/声码器；标准配置 `async_chunk=false`，单请求不是“边生成边播放”的真正流式。
- WSL 根文件系统已从约 250.9 GiB 扩至约 349.3 GiB，净增约 98.4 GiB；扩容后文件系统检查与二次 resize 均通过，未删除或迁移用户数据。
- 独立部署位于 `/root/Elysia/IndexTTS25_vllm`，不会替换原 `/root/Elysia/IndexTTS`。模型 bundle 共 33 个文件、约 9.3 GiB，不含 `.http.part`；微调 `gpt.pth` SHA-256 为 `b4ef7ade9da5262f0d4eca103eccf11f7adefdd757ace281366bc4e1733955fa`。
- 固定环境为 vLLM `0.27.0`、vLLM-Omni `0.27.0rc2.dev123+gb3270dec8`、PyTorch `2.13.0+cu130`、Triton `3.7.1`、Transformers `5.14.1`，Python `3.12.5`；依赖检查无破损，RTX 5090 Laptop / SM 12.0 CUDA 自检通过。

## 3. 接入合同

- 后端由配置显式选择 `vllm_omni` 或 `legacy_compat`，不得从 URL 猜测。
- vLLM 请求使用 `/v1/audio/speech` 与 `model/input/response_format/speed/ref_audio/extra_params`；调用端本地路径不发送给服务端。
- 参考音频在一条表达开始时只读取、编码一次；如已配置命名音色，则不重复传输参考音频。
- 长文本按自然边界切为稳定有序片段。vLLM-Omni 默认并发 2、硬上限 4；完成顺序可以不同，拼接顺序必须等于原文顺序。
- 一条完整表达持有一个合成锁，不允许两条外发语音互相穿插；片段并行只发生在锁内部。
- 每段都请求无损 WAV，全部成功后统一加入边界停顿、拼接、空间处理与最终编码。任何一段失败都不返回部分音频。
- 日志不记录正文、参考音频、完整请求或凭据，只记录字符数、片段序号、音频字节和 content-free 错误类型。

## 4. 为什么采用有界并行

Stage 0 在每个请求内仍需先得到完整语义序列，Stage 1 才生成音频。将一段很长的文本单次送入并不能获得稳定的低首包延迟，而自然句段彼此可成为独立 batch 请求。片段并行因此能够利用 vLLM 的连续批处理缩短墙钟时间，又不改变上游人格样本、表达正文或平台发送语义。

并发不是越高越好。当前目标设备为 24 GB 消费级 GPU，官方高显存服务器结果不能直接外推。实测并发 4 的总体墙钟最短，但相对并发 2 只再减少约 9.5%，单段尾延迟却由约 1.4 秒升至约 2.4 秒；因此默认 2，保留 4 作为经过显存与共享负载验收后的可选吞吐档。

## 5. 真实验证结果

- 两阶段冷启动 41.64 秒，`/health` 与 `/v1/models` 正常，仅监听 `127.0.0.1:8092`；served model 为 `indextts25-timbre`。
- 首次冷请求 41.46 秒，生成 6.385 秒、22.05 kHz、单声道、非静音且全 finite 的 WAV。后续三个短请求分别为 0.74、0.96、1.07 秒，三次全部成功。
- 使用同四个自然句段各跑三轮，墙钟中位数如下：

| 片段并发 | 四段墙钟中位数 | 相对串行 | 观察 |
| ---: | ---: | ---: | --- |
| 1 | 3.2318 s | 基线 | 单段尾延迟最低，总等待最长 |
| 2 | 2.6152 s | 快 19.1% | 默认值，吞吐与余量平衡 |
| 4 | 2.3676 s | 快 26.7% | 比并发 2 仅再快 9.5%，单段尾延迟更高 |

- 九批并发基准均无 HTTP 错误、OOM 或 worker 泄漏；服务加载后静态显存约 15.8 GiB，基准中未继续抬升。
- 使用本次真实 Elysium `TTSService` 而非裸客户端完成集成验证：103 字表达稳定切为 4 段，以并发 2 合成后按原顺序拼为一条 24.8185 秒 WAV；墙钟 2.6442 秒、RTF 0.1065、输出 SHA-256 `bb8465046dcf2a72513157c9016fc394fbf5bbc083f5c3cfd436a1024e9a6186`。
- WSL 扩容后的完全冷态复验中，两阶段服务约 93 秒就绪，仍低于 300 秒启动门；首次实际 Elysium 长文本请求包含 JIT 冷热身，墙钟 38.87 秒。紧接着相同 103 字/4 段/并发 2 暖态复验墙钟 4.92 秒，输出 24.4586 秒、22.05 kHz 单声道 WAV，RTF 0.2010。由此明确区分“冷启动/首批编译”与稳定服务态，不用暖态数字掩盖首次使用成本。
- 中英混合文本以 `lang=zhen` 真实生成 4.9459 秒 WAV；同一句中文在 speed=0.9 时为 5.0503 秒、speed=1.1 时为 3.9126 秒，证明模型原生语速参数方向正确。
- vLLM 0.26 与该 IndexTTS2.5 源码存在已证实 API 不兼容；0.27 解决该问题。RTX 5090 上 FlashInfer 0.6.16 的 JIT capability guard 会误判 SM 12.0，因此部署显式设置 `VLLM_USE_FLASHINFER_SAMPLER=0`，只切换到 vLLM 原生 top-k/top-p sampler；两阶段与连续批处理保持启用。当前 Stage 0 启用 CUDA Graph，Stage 1 按本机显存稳定性配置为 eager，并保留 S2Mel DIT torch.compile。
- TTS、NapCat 群语音、Chatter 与平台工具专项共 116 项通过；Life Engine 串行风险回归 1770 passed / 15 skipped。并行全套中一次 Minecraft 50 ms deadline 测试受负载影响少收一条 observation，隔离连续 5/5 通过，串行全套也通过；该时序项与本轮文件无交集，未以重跑掩盖。

## 6. 剩余生产验收门

1. 用户手工启动 Elysium，确认插件启动、服务 owner 与卸载回收符合规范；agent 不代替用户启动或重启 Elysium。
2. 完成一次私聊与一次群聊真实语音发送回执，确认平台各只收到一条完整语音，且长文本没有拆成多条平台消息。
3. 取消与单段失败的 all-or-nothing 合同已由专项测试覆盖；命名音色上传属于可选优化，不阻断参考音频模式上线。

本机 ignored 配置已解析确认为 `backend=vllm_omni`、`server=http://127.0.0.1:8092`、`model=indextts25-timbre`、`segment_concurrency=2`、`startup_timeout=300`，并保留 `legacy_compat` 代码回退能力。

未完成上述步骤前，旧服务不得删除，配置不得被描述为最终正式切换；遵循“真实启动验证完成后才允许 push”的仓库规范。
