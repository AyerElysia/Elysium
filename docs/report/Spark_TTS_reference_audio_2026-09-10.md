# Spark TTS 参考音频修复与原音色环境部署（2026-09-10）

## 现象与根因

用户报告 04:14:36 TTS 提示 `ref_gentle.wav` 不存在。只读核查发现 Spark 配置已经改为迁移目标路径，但 `/home/ayerelysia/.local/share/elysium-migration-20260908/voice/` 为空。2026-09-08 用户曾要求暂缓语音/视觉模型传输；配置指向尚未部署的语音目录，导致音频缺失。

## 第一阶段：参考音频恢复

从原部署 `/root/GPT-SoVITS/ref_elysia/` 逐字节复制五个现有参考音频至 Spark 配置指定的同名目录：`ref_gentle.wav`、`ref_happy.wav`、`ref_calm.wav`、`ref_curious.wav`、`ref_teasing.wav`。仅新增这些缺失资产，采用忽略已存在文件的复制方式，权限为 0600；没有替换声音、编辑音频、修改运行配置或重启 Elysium。未传输模型。

## 参考音频验证

通过运行 Elysium 的 Spark 账号检查五个路径均可读，WAV 可解析，时长依次为 4.393、4.345、5.513、5.103、5.001 秒，源端与目标端 SHA-256 均一致。主参考音频 SHA-256 为 `ca5ae8e697f8ad29c0534c58ebb4ab6137d1bf3d22175b8e0a2d3c7ca30033d7`。原始资产保留在源端，恢复没有覆盖既有内容。

## 第二阶段授权与部署

用户随后明确同意补齐原来的 GPT-SoVITS 环境。2026-09-10 16:48–17:10（北京时间）完成以下操作：

- 原 `/root/GPT-SoVITS` 的 API、运行代码和工具逐字节迁移；排除旧 x86 环境、训练日志、训练集、历史合成结果及公共模型。原有运行代码中的本机适配保持不变。
- 仅迁移正式配置批准的两份私有音色权重，未换成公共音色。GPT `hiely-e25.ckpt` 的 SHA-256 为 `061f0a8a1658f0b61a0654c976fd7e137284d5deefb24219c4b99337ef80ea68`；SoVITS `hiely_e80_s12960.pth` 为 `52cb12ae0c2140c54e7e20d4c3cf5c785dc78d03d99eb1f96299a0c6c2331486`。两端一致，总计约 314 MiB。
- 在语音目录单独创建 aarch64 Python 3.12.14 环境，安装 PyTorch/Torchaudio 2.11.0+cu130 和推理依赖。不修改 Elysium 主环境或视觉环境。系统 Python 缺少编译头文件，`sudo -n` 不可用，因此改用 uv 管理的用户目录 Python；没有执行系统包安装。首次创建的环境保存在 `.venv-system-python-20260910`，未删除。
- 公共 BERT、HuBERT、声纹、G2PW 和语言识别模型均由 Spark 直接下载，遵守用户此前要求。来源为 [GPT-SoVITS 官方模型仓库](https://huggingface.co/lj1995/GPT-SoVITS)、[上游文档指定的 G2PW 模型](https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained) 及 fast-langdetect 使用的 Meta fastText 官方地址；两个 Hugging Face revision 分别固定为 `336b2ec4e8d4ac74740798dd40af44e74659ecaf`、`0c47645e02a7bc3688d7b263b0042c81e3cd82cd`。
- 启动脚本原先只认 `/root/GPT-SoVITS`，现在优先接受显式 `GPT_SOVITS_ROOT`；未设置时采用同时含 `api_v2.py` 和 `GPT_SoVITS/` 的当前工作目录（即插件已有 `server_dir`），否则保留 WSL 旧默认值。无需更改正在运行实例的配置或重启主进程。

公共模型重新下载后的 SHA-256 均与源端一致：

| 资产 | SHA-256 |
| --- | --- |
| BERT | `e53a693acc59ace251d143d068096ae0d7b79e4b1b503fa84c9dcf576448c1d8` |
| HuBERT | `24164f129c66499d1346e2aa55f183250c223161ec2770c0da3d3b08cf432d3c` |
| 声纹 ERes2Net | `4f5a0bf73c61eb41b174e1bb54e7ee3c83233892be8e0af1f187024e8e581a35` |
| G2PW ONNX | `2eb3c71fd95117b2e1abef8d2d0cd78aae894bbe7f0fac105ddc9c32ce63cbd0` |
| fastText lid.176.bin | `7e69ec5451bc261cc7844e49e4792a85d7f09c06789ec800fc4a44aec362764e` |

## 真实验收与当前结论

资源核查：16:48 大模型仍在运行，CUDA 可用约 1.3 GiB，未在此时启动 TTS。17:01 再查大模型进程已不在、CUDA 可用约 96 GiB；本任务没有停止或重配它。Elysium 自始至终保持 PID `820499`，控制进程 `820493`，端口 `18000/8087` 不变。大模型当时退出原因不在本任务调查范围。

第一轮在独立 `127.0.0.1:19880` 启动临时测试单元，限制 MemoryMax=8 GiB、CPUQuota=400%、RuntimeMaxSec=900，Restart=no。CUDA FP16、v2ProPlus 成功加载原权重。第一次请求发现 fast-langdetect 缓存目录缺失，已补目录并下载模型，部署脚本同步预建并预热该资产。之后五种风格均返回 HTTP 200、32 kHz 非静音 WAV，测试正文明确为工程验收句，不进入主体历史、不向聊天平台发送。

| 风格 | 音频时长（秒） | 热请求耗时（秒） |
| --- | ---: | ---: |
| default | 5.50 | 0.615 |
| happy | 6.14 | 0.950 |
| calm | 5.20 | 0.727 |
| curious | 6.00 | 0.776 |
| teasing | 6.78 | 0.794 |

测试单元内存峰值约 5.71 GiB，包含加载/缓存开销；不是 CUDA 显存的独立计量。随后停止并核对该单元及子进程均已退出。

第二轮使用真实 `TTSService` 和正式配置的只读内存副本，仅将测试端口改为 19880、禁用测试实例的闲置计时器，不启动 Elysium。成功验证：原权重校验 → 插件创建自有进程 → 约 10 秒就绪 → 绑定原权重并跳过重复加载 → 合成 5.64 秒 WAV（冷请求总计 14.672 秒）→ `service.stop()` 回收子进程 → 测试端口释放。测试进程未被遗留为常驻服务。

样本位于语音根目录 `out_test/spark-validation-20260910/`；`plugin-owned.wav` 是第二轮插件链路样本。仅验证了编码、采样值、时长和非静音，未把这些检查等同于主观音色试听结论。

轻量回归：`test/scripts/test_tts_launcher_root.py` 与 `test/plugins/test_tts_service.py` 合计 60 项通过；改动 Python 文件 Ruff、shell 语法和 `git diff --check` 通过。未运行全仓高负载测试、未提交或推送。

已知工具告警：`uv pip check` 对 NVIDIA `nvidia-cusparselt-cu13==0.8.0` 的 `manylinux2014_sbsa` 标签报告平台不匹配；从 PyTorch cu130 官方索引重装仍保留该供应商标签。实际共享库经 `file` 验证为 ARM aarch64，CUDA 矩阵运算与上述真实 GPU 合成都通过。没有篡改包元数据来隐藏告警。

当前完整语音环境已部署、插件冷启动和合成链路已验收。9880 不常驻监听是预期：保留正式插件原来的按需拉起/闲置释放策略，下一次请求执行新启动脚本即可，不需要重启 Elysium。聊天平台发送仍由爱莉/用户触发，本任务未做该外部动作；也未验证 TTS 与重新启动后的大模型满负载并发。

## 复核与回退

重复操作见 [Spark GPT-SoVITS 运维](../operations/spark_gpt_sovits.md)。源端权重和音频全部保留。回退只撤回本任务启动脚本的当前目录识别 hunk；新环境和样本可先保留，严禁为回退删除源端音色/记忆或停止 Elysium。未来如调整依赖，先保存 `.venv` 的确切路径和版本；不得直接复用或覆盖其他子系统的环境。
