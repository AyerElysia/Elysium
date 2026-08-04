# 一手资料索引

> 核对日期：2026-08-04。开工时必须重新读取上游最新 README、模型卡、许可证和 commit，不以本文件代替法律审查。

## 全双工模型

- [BayLing-Duplex 论文](https://arxiv.org/html/2606.14528)：三通道交错序列、四类状态 token、400K SFT、timing DPO、训练参数与公开评测。
- [BayLing-Duplex 官方代码](https://github.com/BayLing-Models/BayLing-Duplex)：当前公开推理实现、模型下载和实时运行方式。
- [BayLing-Duplex 模型权重](https://huggingface.co/BayLing-Models/BayLing-Duplex)：公开 checkpoint 与 GLM speech tokenizer/decoder 依赖。
- [BayLing-Duplex / GLM-4-Voice License](https://github.com/BayLing-Models/BayLing-Duplex/blob/main/LICENSE)：学术、商业登记、分发、展示和衍生模型命名要求。
- [GLM-4-Voice 官方仓库](https://github.com/zai-org/GLM-4-Voice)：9B 中英语音底座、16 kHz/12.5 Hz speech tokenizer 与流式 decoder。
- [Moshi 官方仓库](https://github.com/kyutai-labs/moshi)：双音频流、Mimi codec、PyTorch/Rust/浏览器实时实现和许可证。
- [Moshi 官方后训练仓库](https://github.com/kyutai-labs/moshi-finetune)：双声道数据格式、时间戳转写、LoRA/全量微调、8 卡训练和导出。
- [Moshiko 模型卡](https://huggingface.co/kyutai/moshiko-pytorch-bf16)：英语、CC-BY-4.0、8B 规模和训练基础信息。
- [NVIDIA PersonaPlex 项目页](https://research.nvidia.com/labs/adlr/personaplex/)：真正全双工、文本角色/音频 voice prompt、合成与真实对话混合的公开结论。
- [NVIDIA PersonaPlex 模型卡](https://huggingface.co/nvidia/personaplex-7b-v1)：英语、7B、Moshi 底座、训练数据和 NVIDIA Open Model License。
- [PersonaPlex 论文](https://research.nvidia.com/labs/adlr/files/personaplex/personaplex_preprint.pdf)：约 1,840 小时客服、410 小时问答合成数据、8×A100 训练设置和评测。
- [Freeze-Omni 官方仓库](https://github.com/VITA-MLLM/Freeze-Omni)：chunk-level 状态预测和流式 speech encoder/decoder。

## Omni / 中文音频候选

- [Qwen3-Omni 官方仓库](https://github.com/QwenLM/Qwen3-Omni)：30B-A3B Instruct/Thinking、Transformers/vLLM、音视频输入和流式语音输出。
- [Qwen3-Omni 技术报告](https://arxiv.org/pdf/2509.17765)：Thinker–Talker、234 ms 理论首包、12.5 Hz 多码本、post-training 与 Apache-2.0 说明；未定义原生双通道全双工。
- [MiniCPM-o 4.5 官方仓库](https://github.com/OpenBMB/MiniCPM-V)：连续音视频/全双工声明、训练/推理框架与官方已知限制。
- [MiniCPM-o 4.5 模型卡](https://huggingface.co/openbmb/MiniCPM-o-4_5)：模型能力、许可证与运行要求。
- [Step-Audio 2 官方仓库](https://github.com/stepfun-ai/Step-Audio2)：中文/英语音频理解与生成、Base/mini 权重、Apache-2.0 和 vLLM 路径。
- [ms-swift 官方仓库](https://github.com/modelscope/ms-swift)：Qwen Omni、MiniCPM-o 等多模态 SFT/DPO/GRPO 支持；具体 speech-output loss 仍需逐模型验证。
- [VeOmni 官方仓库](https://github.com/ByteDance-Seed/VeOmni)：多模态分布式训练基座和 Qwen Omni recipe，可作为未来框架对照。

## 计算平台

- [NVIDIA DGX B300 用户指南](https://docs.nvidia.com/dgx/dgxb300-user-guide/introduction-to-dgxb300.html)：8×B300、8×288 GB HBM3e、72 PFLOPS FP8、NVSwitch、本地 NVMe 与系统规格。
- [NVIDIA HGX B300 参考架构](https://docs.nvidia.com/enterprise-reference-architectures/hgx-ai-factory/latest/components.html)：2.3 TB HBM3e、14.4 TB/s 聚合 NVLink 和节点配置。
- [NVIDIA Transformer Engine 低精度训练文档](https://docs.nvidia.com/deeplearning/transformer-engine/user-guide/features/low_precision_training/introduction/introduction.html)：BF16/FP16、FP8/MXFP8/NVFP4 recipe 和精度边界。

## 仓库内现状

- [实时通话意识](../../architecture/实时通话意识.md)：现有 Voice Live Provider、意识实例、上下文、工具、打断、Seed-VC 和手工 Elysium 生命周期契约。
- [Voice Live 商业重建报告](../../report/voice_live-commercial-rebuild-2026-08-02.md)：当前 Qwen Realtime、MiniCPM-o、Moshi/Freeze-Omni 对比和真实链路基线。
- [Seed-VC 集成报告](../../report/voice-live-elysia-seedvc-integration-2026-08-02.md)：现有目标音色路径、性能和资产 revision 边界。
