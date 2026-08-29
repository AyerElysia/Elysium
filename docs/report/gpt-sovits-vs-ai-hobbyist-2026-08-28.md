# GPT-SoVITS 与 AI-Hobbyist 推理框架（2026-08-28）

## 结论

当前普通消息 TTS **已经在用 GPT 那一套**：`tts_voice_plugin`、`backend = "legacy_compat"`、本机 RVC-Boss `api_v2`、`127.0.0.1:9880`、v2ProPlus 微调权重。没有切到 IndexTTS / vLLM-Omni。

AI-Hobbyist 的 `GPT-SoVITS-Inference` 是 RVC-Boss 的推理向 fork，HTTP 合同仍是 `/tts` + `/set_gpt_weights` + `/set_sovits_weights`。换 fork **不会**变成另一种推理引擎，也修不了缺失权重。公共站 `tts.ai-hobbyist.org` 禁止发送主体语音或参考音频，本次没有接入。

## 本机 live 路径

| 项 | 值 |
|---|---|
| 协议 | GPT-SoVITS `api_v2` `/tts` |
| 启动 | Elysium `scripts/tts/start_gpt_sovits_hiely.sh` 监督 `/root/GPT-SoVITS/api_v2.py` |
| 工作目录 | `/root/GPT-SoVITS`（RVC-Boss 树，不是 AI-Hobbyist clone） |
| 风格权重 | `latest/hiely-gpt.ckpt`、`latest/hiely-sovits.pth`（由批准路径生成链接） |
| 本机批准组合 | `hiely-e25.ckpt` + `hiely_e80_s12960.pth` |

`models.toml` 没有 `tasks.tts`。`life_send_voice(text=...)` 只消费 `tts_voice_plugin:service:tts`。

## 历史“推理总有问题”的可核对原因

`log_apiv2.txt` 里有 4 次：

- `/set_gpt_weights` → `hiely-e10.ckpt` **200**
- `/set_sovits_weights` → `hiely_e20_s1980.pth` **400**

`SoVITS_weights_v2ProPlus/` 里 **没有** `hiely_e20_s1980.pth`（现存是 e60/e70/e80）。当时 GPT 切到 e10，SoVITS 切换失败，进程仍带着启动时的 e80，随后 `/tts` 仍返回 200。这是 **GPT/SoVITS 检查点不成对**，不是“换一个 Inference 仓库就能好”。

当前插件在权重切换非 200 时必须 fail closed，不再用残留权重继续合成。本机配置显式固定人工试听通过的 e25/e80；2026-08-29 的后续加固又把路径与 SHA-256 绑定成不可拆分合同，启动器不再回退最新 epoch，Service 也会在拉起后端前拒绝缺失、替换或摘要不匹配的权重。

## 2026-08-29 全链路复盘与修复

仅确认“后端叫 GPT-SoVITS”不足以解释用户听到的问题。用真实聊天文本 `32` 个可发音字符做同链路复现后，确定还有四个工程根因：

1. 可发音投影先删除 `♪` / emoji，会把 `哦♪ 想` 变成 `哦 想`，两个子句在声学输入中粘连；
2. 启动器已经加载 e40/e80，但每条 `/tts` 前仍重复调用两个权重端点，热请求浪费约 6～9 秒；
3. 外部服务和自有服务没有分开建模，不能既安全复用自有进程权重，又对陌生端口 fail closed；
4. WSL 下旧启动器用 `exec` 后，受管句柄可先退出而 API 被重新挂到 `/init`。验证脚本记录“服务子进程已停止”，但 `9880` 和 GPU 模型仍在，属于生命周期假成功。

已修复：

- 装饰音符/emoji 位于两个可发音子句之间时只派生 `，`，尾部装饰移除；人工验收过的 `～`/`……` 韵律与原始消息、trajectory 和记忆不变；
- 在改变任一模型前完整预检 GPT/SoVITS 权重对；缺文件、HTTP 非 200 都不发送 `/tts`；
- 自有进程以进程对象和 symlink-aware 文件 identity 绑定权重状态；同进程热请求跳过重复加载；外部端口永不复用该缓存；
- 本机启动器显式声明已加载 `default` 权重对，使冷启动也不做第二次加载；
- 监督启动器纳入仓库，保持为进程组 owner，等待并回收 API 子进程；停止后验证进程、端口与显存；
- 日志拆分 `server_wait_ms`、`weight_ms`、`synthesis_ms`、`total_ms`，QQ 投影另记算法、字节数和耗时；
- v2ProPlus 不消费的 V3 参数恢复为 API 默认 `sample_steps=32`、`super_sampling=false`。150/true 与 32/false 对照 WAV 哈希完全相同，不再把无效字段冒充质量优化。

第一阶段基础设施修复使用 e40/e80、default 参考音频、`cut4`、seed `20260826` 的同文本实测：

| 链路 | server wait | 权重 | 合成 | 总计 |
|---|---:|---:|---:|---:|
| 修复前冷链 | 约 34 s | 约 7 s | 约 22 s | 65.45 s |
| 修复后冷链（仓库正式启动器） | 28.11 s | 0.5 ms | 17.54 s | 45.67 s |
| 修复后热链（同一自有进程） | 9.5 ms | 2.5 ms | 6.45 s | 6.47 s |

这次修复只证明了生命周期和请求合同，**没有通过用户听感门**。后续复盘恢复了历史试听矩阵，确认用户认可的样本 `20_gpt_e25_sovits_e80_qq.wav` 实际是未经 QQ 投影的 32 kHz 原始 WAV。用 e25/e80、`cut5`、`speed_factor=0.95`、seed `20260826` 重放同一文本后，新原件与历史好样本的 PCM 波形相关系数为 `0.99999992`，说明模型与权重已能复现好样本。

真正的剩余偏差来自 NapCat 发送前的旧 `qq_voice_presence_v1`：它先抬高 2.8/4.5 kHz、限幅并降到 24 kHz，之后 NapCat 又编码 NT-Silk。离线调用当前 NapCat 原生编码器证明，新旧 NapCat 4.18.13/4.18.15 对同一原音生成逐字节相同的 Silk；升级 NapCat 不会改变该编码。旧投影的 SI-SDR 明显差于原音直接进 NapCat，因此生产默认改为原音直送，旧投影仅作显式实验兼容。本机同时验证了高码率 pysilk 可被 NapCat 解码，但在听感通过前不引入新的生产依赖或复杂度。

批准组合的同文本复验为：冷链约 34.3 秒（启动约 22.1 秒、合成约 12.2 秒），热链约 3.3 秒；原始 WAV 为 32 kHz mono / 8.74 秒。验证结束后 `9880` 无监听、GPT-SoVITS 进程不存在、GPU 回到空闲基线。

最终真实“Life Chatter → QQ → 手机播放”仍必须在用户手动重启 Elysium 后验收；本次没有替用户重启 Elysium/NapCat，也没有外发测试消息。生产推送必须等该运行态闭环通过。

## 明确没有做的事

- 没有把语音发到 `tts.ai-hobbyist.org`
- 没有用 AI-Hobbyist fork 替换 `/root/GPT-SoVITS`
- 没有把 `backend` 改成 `vllm_omni`

若以后要并排试验 AI-Hobbyist 本地 fork，必须另开目录与端口，沿用同一套 hiely 权重和 `/tts` 合同，成对试听后再决定是否改 `start_command`。公共推理站仍禁止。
