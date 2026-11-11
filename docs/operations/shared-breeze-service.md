# 独立共享 Breeze TTS 服务

## 边界

Spark 上唯一 Breeze API 运行在现有 breeze-tts2 容器，本机地址 127.0.0.1:7862。模型服务独立于 Elysium、ElyHarn 和直播插件；调用方只发 HTTP 请求，auto_start=false，不启动、重启或关闭共享服务。当前继续保持原容器的部署策略，未添加新的守护或恢复循环；不扩大到公网监听。

现有 Python Elysium、ElyHarn conscious-mind、dsh-livestream 使用同一接口与既有音色配置。客户端不得自行争抢 GPU、各自创建模型服务或对已受理的 POST 盲目重投。

## 调用契约

- POST /v1/audio/speech：保持原 multipart 字段 text、instruction、cfg_scale、ref_audio/ref_text、seed。返回 audio/pcm，X-Sample-Rate=24000、X-Sample-Format=s16le。完整 PCM 成功结束后，调用方才封装并交付 WAV。
- X-Breeze-Client 是受限工程标签，不是身份、优先级或信任评分。服务统一 FIFO，只有一个 GPU worker 执行模型准备、随机种子、推理、codec 关闭与清理。
- 等待队列默认 8 项、排队 30 秒、从受理起总预算 300 秒。X-Breeze-Timeout-Ms 只能缩短服务端预算。队列满返回 503 + Retry-After；排队超时返回 504；已开始的流失败会断流，不能把半音频或空音频当成功。
- 200 后返回 X-Request-ID 与私有 X-Cancel-Token。GET/DELETE /v1/requests/{id} 只接受该请求的 capability header；token 不进 URL、日志或健康输出。排队时尚无响应头，取消原 HTTP 请求即可取消自己的等待。
- GET /health 提供 ready、busy、queued、active 的安全 id/client/phase/elapsed/cancel 信息。ready 表示能接受工作，不等于当前空闲；看 busy 与 queued。加载或 stalled 返回 503。健康信息不包含正文、音色文本、凭据或私有参考路径。
- 默认 PCM 缓存有界为 8 块/512KiB、单块64KiB、总输出64MiB；输入和上传大小同样有界，超出时显式失败，不截断后宣称成功。

Python 客户端整条表达只有一个期限（包含本地等锁、所有片段与 PCM），首段失败即停止后续片段；对旧服务 409 兼容最多 3 次，不再每片段等待 20 分钟。JS 两端复用一个低层传输，不再维护无法跨程序协调的局部队列或 409 轮询。503/504 明确失败，不自动重复未知结果。

## 取消与生命周期

客户端断开、取消、超时会发协作取消信号；只有唯一 worker 真正退出生成器、关闭 codec 并清理后才释放 owner。超过10秒仍未退出时，服务标记 stalled 并拒绝新受理，不抢清锁、不开第二 GPU owner。线程中的推理不能靠取消外层等待强制停止；取消检查必须进入实际生成循环。[AnyIO 线程说明](https://anyio.readthedocs.io/en/stable/threads.html)

Python TTSService 每条表达有本实例独占 child task。stop 仅取消并等待本实例的活动/排队表达；不误伤外层聊天任务或其他调用方，不关闭外部 Breeze。真正的调用者取消继续传播。每个客户端退出不会关闭模型服务。

## 运维

服务端代码在 /home/ayerelysia/AI/breeze-tts，详见其 docs/shared-service.md。当前容器继续使用原 python -m breeze_infer.api /models/breeze --host 0.0.0.0 --port 7862 命令，Docker 只发布到 host 127.0.0.1。不要加 Uvicorn 多 worker；API CLI 强制 workers=1。

操作前核对容器完整 id、实际入口、PID、UID/cwd、监听映射、当前任务和 owner。持续 busy 时先看 active 的阶段、取消状态和耗时；不能凭旧版 health=ok 判定推理槽空闲，也不能固定周期重启。

当前 Python 私有配置 config/plugins/tts_voice_plugin/config.toml 中仅把 [tts] timeout 从1200改为300；音色、参考音频、其他字段不变。该文件仍忽略且0600。服务恢复不通过改主体文件、伪造发送结果或静默切云服务实现。

本机以外的程序应经过已授权的本地转发接入；本说明没有扩大公开访问或关闭认证。

真实并发与取消验收见 [2026-11-10 记录](../report/2026-11-10-shared-breeze-service.md)。
