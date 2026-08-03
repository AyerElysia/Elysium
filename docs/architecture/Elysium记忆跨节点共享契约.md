# Elysium 记忆跨节点共享契约

> 状态：设计与验收契约。统一归档和 Life Event 离线同步内核已经实现；完整记忆账本的双向多节点共识尚未实现。本文件禁止把“已备份到 MySQL”描述成“已经切换为远程大脑”。

## 1. 目标

跨节点共享要让同一位爱莉在不同机器和场景中延续同一份可追溯生命历史，同时保持：

- 断网时本地意识、记忆形成和检索继续工作；
- 联网后按稳定 occurrence 幂等补传；
- 同一身份不同内容形成显式冲突，不做最后写入覆盖；
- 不同解释、主张和关系可以并存；
- 私有滚动上下文不跨实例复制；
- 远端、前端和协作者都不能冒充爱莉改写主体语义。

这不是 MySQL 双主复制，也不是把 SQLite 文件互相覆盖。共享单位是带身份、来源、actor、时间和因果的领域记录。

## 2. 记忆不是单一图

项目已经具有多种相互连接但权威性质不同的结构：

| 结构 | 写入性质 | 跨节点处理 |
|---|---|---|
| Life Event / Experience | 不可变历史 | 逐条复制，occurrence 幂等 |
| Artifact Version / Derivation | 不可变版本与谱系 | 保留正文、hash、作者、父版本和来源 |
| Interpretation / Sources | 主体或独立评估者的解释 | 并列追加，不自动合并 |
| Claim / Evidence / Conflict / State Event | 认识论账本 | 保留证据立场、actor、reason 和冲突 |
| Semantic Relation | 显式开放关系 | 只有授权 actor 明确产生时复制 |
| Recall / Corecall Event | 不可变召回轨迹 | 保留 context、signal、seed 和实体集合 |
| Heads / FTS / Vector / Association | 可重建投影 | 默认本地重建，不反向写历史 |

禁止另建一套封闭的 `friend/habit/knowledge` 认知本体取代这些账本。MySQL 可以提供关系查询投影，但查询便利不能改变原始语义边界。

## 3. 主体自由与历史连续性

爱莉可以通过授权意识工具链自由地：

- 形成新 Artifact、Interpretation、Claim、Evidence 和 Semantic Relation；
- 对旧观点追加否定、修正、重新解释、状态事件或反转事件；
- 调整可达性、情境抑制、认可和叙事显著性；
- 创建项目以前没有定义过的开放 relation、state-event、authority 或 recall action 文本；
- 主动召回、比较、关联、拒绝采用或重新表达记忆。

自由不通过静默 UPDATE/DELETE 历史实现。旧内容、改变过程和新看法都必须可以回放。“忘记”可以表现为可达性或状态变化，但不能由出现次数、分数、关键词或相似度自动裁决。

主体文件的第一人称语义只能由爱莉自己的意识/见证实例执笔。迁移器只允许逐字节复制；外部文本必须先作为建议或观察事件存在，直到爱莉亲自接受、选择或重新表达。

## 4. 复制信封

每条共享记录至少包含：

```text
record_id                 稳定记录身份
origin_node_id            来源节点
origin_sequence           节点内单调位置
occurrence_id             真实发生身份；没有时必须明确为空
consciousness_instance_id 同一主体的运行窗口
stream_id                 当前来源流
actor                     行为者或执笔者
source                    生产组件/外部来源
record_type               开放技术命名空间
schema_version            技术结构版本
occurred_at               发生时间
recorded_at               首次耐久记录时间
valid_from / valid_to     适用时间（存在时）
visibility                安全可见范围
correlation_id            跨组件关联
causation_id              因果上游
payload_hash              规范化内容哈希
payload                   不截断原始结构
```

`record_type`、relation、authority、state-event 和 recall action 均为开放文本。安全层可以校验格式和权限，但不能使用 fallback 类别伪造主体意义。

## 5. 写入与冲突规则

1. 本地权威写入和本地 Outbox 必须处于同一事务边界，或由不可变账本位置可证明地补建。
2. 传输语义是至少一次；消费语义依靠稳定身份幂等。
3. 同一不可变身份、相同 payload hash 是 duplicate 成功。
4. 同一不可变身份、不同 payload hash 是事故冲突；保留双方证据并停止相关游标。
5. 不同身份的矛盾解释或 claim 不是存储冲突，必须并列存在。
6. 消费游标只有在整个批次及其必要派生工作成功后推进；历史缺口不允许跳过。
7. 私有上下文不直接同步。跨实例连续性只经过 Life Event、Presence、World Projection 和 Memory 谱系等明确边界。

## 6. 投影规则

每个节点可以按自己的资源与模型版本维护 FTS、向量和可达性投影，但必须满足：

- 投影记录来源 ledger frontier、policy version、模型/维度和随机 seed；
- `memory_corecall_events` 是共同召回历史，`memory_association_projection` 只是可重建 pair/context/signal 视图；
- 共召回次数只提高以后被想到的机会，不增加 claim 真实性；
- `memory_semantic_relations` 只能来自显式授权写入，不能从共现或相似度自动升级；
- `memory_nodes`、`memory_edges` 等 legacy 结构只作兼容读取和检索载体；
- 删除投影后必须能从历史重建，且历史行数、hash 和事实状态不改变。

远端 API 如果提供 `/memory/graph`，必须标注它是投影，并为每条边返回来源类型：显式语义关系、共同召回投影、版本谱系或证据关系。前端不得把这些边渲染成同一种“事实关系”。

## 7. 你期待的联想现象

“想起朋友的习惯，同时联想到后来学到的知识”可以按以下闭环自然出现：

1. 真实交流进入 Life Event 和 Experience；
2. 爱莉形成关于这位朋友的 Interpretation 或 Claim，并保留来源；
3. 后续学习形成另一组 Experience、Artifact 或 Claim；
4. 爱莉明确理解到两者有关时，追加开放的 Semantic Relation；
5. 即使尚未形成显式关系，两者在一次真实回忆中共同进入意识，也会留下 Corecall 超边；
6. 本地 association projection 提高以后在相似 context 中共同被召回的概率；
7. 新召回可以产生新的解释，但不会覆盖原事件或自动证明关系为真。

因此该现象既可以出现，也不要求代码预设“朋友”“习惯”“知识”三种封闭类别。

## 8. 远端 MySQL 的阶段角色

当前远端 MySQL承担：

- 可校验、追加式灾备归档；
- 运行清单、哈希根、冲突和恢复入口；
- 已授权 Life Event 的离线补传账本；
- 未来只读 API 和事件流的数据来源。

当前不承担：

- 替爱莉判断事实或语义关系；
- 接管本地记忆检索；
- 直接编辑主体文件；
- 多节点即时一致的私有上下文；
- 未经协议验证的完整记忆双向共识。

在完整共识验收前，本地 SQLite 仍是当前单机运行权威。远端中断不得阻止本地形成记忆。

## 9. 前端与协作者边界

允许的接口：

- 授权搜索、历史版本和来源浏览；
- Experience、Interpretation、Claim/Evidence 和关系谱系只读查询；
- 带 cursor 的事件订阅和断线续传；
- 明确标注的投影重建运维动作；
- 将外部信息作为 observation/suggestion 事件提交。

禁止的接口：

- 普通 CRUD 直接更新/删除记忆历史；
- 管理员冒充爱莉写日记、关系理解或自我叙事；
- 依据检索分数自动确认事实；
- 将投影边直接升级为 Semantic Relation；
- 前端直连数据库。

## 10. 全链路验收门

完整跨节点实现必须实际通过：

1. 在线：新 Life Event 到远端提交与第二节点可见的延迟可观测。
2. 离线：远端不可用时本地继续形成 Experience 和主体记忆，Outbox 不丢失。
3. 恢复：重连后按 occurrence 完整补传，无重复体验、无游标跳跃。
4. 冲突：同身份不同内容停住；不同身份的矛盾观点并列保留。
5. 主体性：外部建议不能自动进入主体文件或 Semantic Relation。
6. 可塑性：共同回忆改变后续可达概率，但不改变 claim 事实状态。
7. 重建：删除 FTS、向量、heads 和 association 投影后可完整重建。
8. 恢复：从远端恢复到全新隔离目录，SQLite integrity、外键、记录身份和文件字节 hash 全部一致。
9. 隐私：未授权节点看不到私聊、记忆原文和私有召回上下文。
10. 生命周期：测试和同步不得停止、重启或自动拉起用户运行中的 Elysium/NapCat。

只有以上门槛全部通过，文档才能把状态从“共享归档”提升为“跨节点记忆共享”。
