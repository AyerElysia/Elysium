# Breeze TTS 连续超时与共享服务修复

## 请求与根因

用户2026-11-10提供17:24–19:24连续20分钟超时日志，要求独立 TTS 服务供所有程序共用。

真实检查显示已经只有一个服务：breeze-tts2 容器，127.0.0.1:7862。问题是旧服务仅有同步请求锁，没有跨程序队列；HTTP 断开没有及时取消推理/关闭生成器，因此占用长期保留，其他程序只能收到409。旧 health 只报HTTP在线和采样率，无法区分 busy。Python 客户端以1200秒作为每片段的期限并无限轮询409；某段失败后仍继续后续片段，导致6段/3段表达耗时被成倍放大。JS 各自的局部队列或重试不能协调其他程序。

本轮修的是共享服务及调用契约，不以再重启一次冒充根因解决。

## 工程变化

- AI/breeze-tts：独立 API + 纯CPU调度器，单长期 GPU owner、有界FIFO；模型加载也由该 owner 完成，API可在加载期报告状态。worker 是唯一执行推理/codec.close/清理并释放active的路径。
- 取消、排队期限、请求总期限和背压明确；stalled不强占释放，200后失败不正常EOF，零PCM明确失败。能力 token 只经header返回/校验，健康输出仅安全元数据。
- fast_streaming 仅增加实际生成循环的合作取消及已有 codec finally 覆盖，没有修改模型、采样身份、权重、参考音色或人物文件。
- Python整条表达一次总预算、兼容旧409最多3次、首失败停止；TTSService跟踪本实例独占child，停止不取消外层caller或共享服务。
- JS conscious-mind与直播共用一次有限传输，移除直播局部队列和409轮询，完整PCM确认后才封装WAV。

原服务端源码基线、Python源码基线、已跟踪conscious低层基线和配置私有备份位于 /tmp/elysia-shared-tts-deploy-mp9nuy3t，目录0700，含环境/配置的文件0600，不提交。Python原文件另由客户端负责人留档 /tmp/breeze-python-shared-client-ujzxhcx6。原有9个模型工程备份文件未动。

## 验证

CPU验证：服务端42项（真实ASGI2.0/2.4断开、队列满、期限、stalled owner、清理期间取消race、上传资源与零音频）；Python两个轻量文件72项；JS两端40项，类型检查0诊断。未与正式storage authority争用或进行全仓负载回归。

### 真实三个程序同时调用

使用实际profile导出/实际Python TTSService，固定短运维文本，不导入主运行时、调用主体表达模型、注入正式会话或发送平台消息。三个独立进程同时发请求，观测等待峰值2；三个均HTTP成功、完整非空24kHz单声道WAV：

| 调用方 | 端到端毫秒 | WAV字节 | 音频毫秒 |
|---|---:|---:|---:|
| Python Elysium | 8991 | 126764 | 2640 |
| ElyHarn conscious-mind | 5478 | 61484 | 1280 |
| dsh-livestream | 3533 | 61484 | 1280 |

Python音频RMS5924.783/peak32530；两JS RMS约6292/6287，均非静音。请求未被重复提交，JS各仅一次POST，服务确实统一排队。原始安全结果见 concurrent-acceptance.json。

### 真实生成中断开与恢复

独立运维客户端收到第一块7680字节真实PCM后关闭自己的HTTP。该request 669ms内进入 cancelled/done，cancel_pending=false、buffered_bytes=0，错误码client_disconnected；worker释放后health恢复ready/idle/queued0。随即下一条请求完成：PCM80640字节、WAV80684字节、24kHz、1680ms，耗时2545ms，RMS0.16397。没有重启服务来完成这项恢复验证，也没有取消其他程序的任务。结果见 /tmp/elysia-breeze-cancel-check.json。

## 运行切换与所有权

先核对原Python2302442（tmux父2302441，uid1000、父repo cwd、固定main.py）与Web1333483/1333462（Node22、child/dsh cwd、3299/3300同owner），保留原argv/env后正常SIGINT/SIGTERM退出；未强杀、清锁或改正式历史。该步骤约20:18:49完成。

私有配置仅[tts] timeout1200→300，逐字节备份并解析证明其余字段完全一致，0600保持；profile仅精准刷新新的conscious低层文件，其余两直播文件已同SHA。不全workspace安装，不重置任何他人改动。

容器完整id为763a9523cc8dcf7c15570598c9de97cd36bba74137004c40405d6305c6df86bf。核对唯一python/唯一该容器owner、没有7862在途HTTP后，准确一次 docker restart --time30；PID250654→2475812，2026-11-10T20:20:33.623+08启动。原镜像、参数、只读模型挂载、回环映射与既有unless-stopped策略完全未变，没有添加自动恢复。新health在模型加载期间报告loading，之后ready/idle；不是拿旧health=ok代替推理验收。

调用方在维护期间20:20:03重新出现，触发来源未确认；root两次启动前断言均阻止重复启动，未再创建第二实例。准确核对后复用Python2474216（pane2474214）与Web2474247/2474226；Web启动晚于最终profile刷新。Python经原生 /reload tts_voice_plugin 单次重载配置，其20:32:42.867日志确认插件初始化/风格加载/TTSService成功。新loaded class已具备正确stop所有权，不再假定旧class可取消旧在途任务。

最终service PID2475812保持不变，ready=true、busy=false、queued0；main与Web正常运行。安全验收元数据和阶段结果位于上述私有部署目录，不回显正文、音色参考文本、token或用户私聊；其中另行保存的配置/环境备份属于敏感留档，不能对外展示或提交。

## 边界与回滚

- 这是本地TTS API恢复验收，没有公开播出诊断音频，也不证明任何平台混音状态。
- Python线程不能强杀真正卡住的CUDA调用；这种情况会明确stalled/503并保留owner，须由正确运维owner处理，不会开第二GPU任务伪装恢复。
- 回滚应先核对当前owner，使用私有备份逐个恢复具备基线的工程文件与配置，保持成对JS导出兼容，再精确正常重载对应程序/容器。两份原先未跟踪的直播breeze.ts/tts.ts修改前未留下逐字节备份，因此不能承诺恢复其旧部署版本；部署目录存下的是本轮修改后的profile快照。不得编造旧代码或只退conscious导出而破坏新直播依赖。不能全仓reset或覆盖并发修改；旧后端不具备新排队/取消保障，不应将回滚当正常忙时恢复策略。
- 本轮未创建提交或推送，未修改主体语义、人物权重或音色文件。
