# Spark 原音色 GPT-SoVITS

## 位置与所有权

- 工程入口：`/home/ayerelysia/Elysia/Elysium/scripts/tts/start_gpt_sovits_hiely.sh`。
- 语音根目录：`/home/ayerelysia/.local/share/elysium-migration-20260908/voice/GPT-SoVITS`，专用 `.venv` 为 aarch64 用户目录 Python。
- 正式配置：`config/plugins/tts_voice_plugin/config.toml`。保留 `legacy_compat`、原 Hiely 权重及 SHA-256、五份原参考音频。
- 正式地址 `127.0.0.1:9880` 由 TTS 插件按需创建和回收；不额外安装常驻/自恢复服务。Elysium 仍遵守手工启动约束。

没有显式 `GPT_SOVITS_ROOT` 时，启动脚本在当前目录同时存在 API 文件和运行包的前提下使用该目录；插件设置的 `server_dir` 因而可直接用于 Spark，WSL 旧默认路径仍兼容。若从其他目录手动运行，应显式设置 `GPT_SOVITS_ROOT`。

## 安装与公共资产

仅在缺失环境需要重建时安装；现存环境先备份，不覆盖未知内容。用 uv 管理的 Python 3.12 建 `.venv`，避免系统 Python 缺少开发头文件。先从 `https://download.pytorch.org/whl/cu130` 安装 `torch==2.11.0`、`torchaudio==2.11.0`，再用 `scripts/tts/spark-inference-requirements.txt` 安装推理依赖。

在已装好推理依赖的语音环境中运行 `scripts/tts/download_spark_tts_models.py <语音根目录>`，由 Spark 直接下载固定 revision 的公共模型，并预热 fastText。脚本不下载或上传私人音色权重；私人权重只能从批准的旧部署原样迁移并核对正式配置中的 SHA-256。

供应商 cuSPARSELt wheel 的 `sbsa` 标签会让 uv 平台检查报警；2026-09-10 验收时共享库实际为 ARM aarch64，GPU 推理通过。不要通过修改 wheel 元数据消音；升级时必须重新验证真实加载和合成。

## 隔离验证

先检查 PID、端口 owner 和可用内存，不能为了测试停止 Elysium 或其他推理服务。GPT-SoVITS 与 Spark 大模型共享统一内存；单次历史空闲验收不代表并发容量保证。

1. 在 `server_dir` 运行启动脚本 `--dry-config`，验证批准权重和生成配置，不监听端口。
2. 需直接测后端时使用独立端口 19880，带显式资源限制和有限寿命的自有测试进程；不要抢占正式 9880。
3. `validate_spark_tts.py <正式配置> <新输出.wav> --style default` 只向本机测试端口合成固定工程测试正文；输出使用排他创建，不覆盖原有样本。其他四种风格可逐一测试。不要并发切换同一个 legacy 模型进程。
4. 测试后停止并确认自有进程和端口释放。然后可用 Elysium 主环境运行 `validate_spark_tts_plugin.py <正式配置> <新输出.wav>`，验证真实插件的自有冷启动、合成与停止；该脚本只在内存中改测试端口，不启动主程序、不发送平台消息。输出目录应提前创建于隔离测试位置。
5. 核对 Elysium 原 PID/端口未改变，测试 19880 没有遗留 owner。正式 9880 在下一次真实语音请求前不监听属于正常按需行为。

详细验收、版本、来源与回退边界见 [2026-09-10 部署记录](../report/Spark_TTS_reference_audio_2026-09-10.md)。主观音色仍由用户试听；不能仅凭非静音校验宣称音色品质已评定。
