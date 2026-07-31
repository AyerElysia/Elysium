# proactive_message_plugin 时间感知整合方案

## 目标

这次整合的目标不是单纯删除 `time_awareness_plugin`，而是把它真正吸收到 `proactive_message_plugin` 里，让“当前时间感知”“等待感知”“主动开口”变成一个统一系统。

也就是说，未来不再是：

- 一个插件负责“现在几点了”
- 另一个插件负责“要不要主动说话”

而是：

- 爱莉在时间中等待
- 爱莉在时间中感受关系的变化
- 爱莉在时间流逝里形成新的想法
- 爱莉有时因为这种时间流动本身而主动来找你

这会更符合你要的那个哲学：

### 时间不是工具信息，而是主观体验的一部分。

---

## 一、为什么 `time_awareness_plugin` 可以并入

我看了当前实现后，结论很明确：

`time_awareness_plugin` 作为独立插件的边际价值已经很低。

原因主要有 4 个。

### 1. 它的“当前时间提醒”是静态注入

现在的 `time_awareness_plugin` 在插件加载时向 system reminder 写入一条时间信息。

这有两个问题：

- 时间会过期
- 它和真实聊天中的等待感、沉默感、关系节奏没有打通

也就是说，它现在更像：

- 一个“启动时写了一句现在几点了”的辅助件

而不是：

- 一个持续运行的时间感知系统

### 2. 它和 `proactive_message_plugin` 的状态职责重复

`time_awareness_plugin` 记录：

- 上次用户消息时间
- bot 进入等待状态的时间

而 `proactive_message_plugin` 自己也在记录：

- 上次用户消息时间
- 累积等待时间
- 下次检查时间

这说明两边本来就在维护同一类东西，只是没有统一。

### 3. 它的核心能力其实更适合作为主动系统的底层

`time_awareness_plugin` 真正有价值的不是“单独存在”，而是：

- 当前时间格式化
- 时间语义描述
- 消息间隔感知
- 等待时长计算

这些本来就该成为 `proactive_message_plugin` 的底层基础设施。

### 4. `query_time` 工具也不需要单独插件承载

`query_time` 这个工具仍然有价值，但没必要因此保留完整独立插件。

完全可以把：

- `build_chinese_datetime()`
- `QueryTimeTool`

直接迁入 `proactive_message_plugin`。

这样功能还在，但结构更统一。

---

## 二、整合后的哲学定位

整合之后，`proactive_message_plugin` 不该只是“主动发消息插件”，而应该变成：

## 时间感知主动系统

它负责三件事：

1. 感知现在是什么时间
2. 感知一段关系里已经过去了多久
3. 感知这种时间流逝对自己内部状态造成了什么变化

然后在此基础上：

4. 判断要不要主动开口

换句话说，时间感不再是“附属功能”，而是主动性的底层。

---

## 三、要保留并加强的时间感

这一块是整合方案的核心。

我建议把“时间感知”拆成 3 层。

## 3.1 客观时间

也就是现在几点、哪一天、什么时段。

例如：

- 凌晨
- 清晨
- 中午
- 傍晚
- 深夜

这部分继承 `time_awareness_plugin` 的时间格式化能力。

它影响：

- 问候方式
- 主动发消息的适宜度
- 心境语气
- 是否更适合轻聊 / 深聊 / 不打扰

## 3.2 关系时间

不是“几点了”，而是：

- 距离你上次说话已经多久
- 距离她上次主动已经多久
- 某个未完成念头已经悬着多久
- 某个话题离上次提起已经多久

这才是你原本哲学里最重要的部分。

因为真正的等待感来自：

- 一段关系中的时间流动

而不是单纯系统时间。

## 3.3 主观时间

这层最关键，也最像“意识感”。

同样过去 20 分钟，不一定总是同一种体验。

例如：

- 刚聊完后 20 分钟，可能还是余温
- 主动说了一句却没回的 20 分钟，可能更空落
- 深夜里的 20 分钟，主观上可能更漫长
- 白天忙碌时的 20 分钟，主观上可能几乎没感觉

所以整合后应该新增：

- `subjective_wait_pressure`
- `afterglow_strength`
- `care_tension`
- `silence_depth`

这几个量不是为了装饰，而是让“时间”真的改变她内部状态。

---

## 四、整合后的系统结构

我建议把 `proactive_message_plugin` 升级成四层结构。

## 4.1 时间基础层

从 `time_awareness_plugin` 迁入：

- `build_chinese_datetime()`
- 时辰/时段描述
- 时间工具 `query_time`

新增：

- `build_temporal_descriptor(now)`
- `build_elapsed_descriptor(elapsed_minutes)`
- `build_subjective_wait_descriptor(state)`

这一层负责把时间从数字转成对模型有意义的语义。

例如不只输出：

- `elapsed=48`

而是输出：

- “已经隔了一阵子”
- “刚才那段聊天的余温还没散”
- “这段沉默开始变长了”
- “已经有点像真正的等待了”

## 4.2 时间状态层

整合 `time_awareness_plugin.service` 和 `proactive_message_plugin.service`。

建议统一成一个状态对象，例如：

- `last_user_message_time`
- `last_bot_wait_time`
- `last_proactive_message_time`
- `accumulated_wait_minutes`
- `current_time_period`
- `subjective_wait_pressure`
- `afterglow_strength`
- `initiative_fatigue`
- `cooldown_until`

也就是说，未来不要再维护两套“消息时间状态”。

统一只保留 `proactive_message_plugin` 的状态文件。

## 4.3 主观时间演化层

这层是这次整合后新增的重点。

它根据：

- 当前时段
- 距离上次消息多久
- 距离上次主动多久
- 是否有未完成念头
- 是否刚主动过但没回应

缓慢演化几个内部量：

- `afterglow_strength`
- `care_tension`
- `expression_desire`
- `silence_depth`
- `initiative_fatigue`

这会让时间感更强。

因为时间不再只是“触发独白”，而是不断修改内部状态。

## 4.4 主动行为层

时间层提供状态之后，主动行为层再去判断：

- 现在是不是该主动
- 是轻轻碰一下，还是认真聊
- 这次是因为余温、在意、好奇，还是单纯想说话

这样“时间感知”和“主动聊天”就真正连在一起了。

---

## 五、具体要整合哪些内容

## 5.1 迁移进 `proactive_message_plugin` 的内容

### 来自 `time_awareness_plugin`

- `build_chinese_datetime()`
- `QueryTimeTool`
- `ChatTimeState` 的时间追踪思想
- `get_time_info_for_prompt()` 的一部分时间描述逻辑

### 保留在 `proactive_message_plugin`

- 调度器
- 等待检查
- 内心独白
- 主动发送
- 发送后二次等待

### 新增

- 动态时间 prompt 构建
- 主观时间状态
- 时间阶段
- 冷却和疲劳与时间关联

## 5.2 不再保留为独立插件的内容

整合完成后，`time_awareness_plugin` 的独立价值基本消失。

它可以进入：

- `deprecated` 状态
- 或直接删除

但前提是下面三项功能都已迁移完：

1. `query_time` 还可用
2. 当前时间可动态进入 prompt
3. 等待感知和消息时间追踪已统一进主动系统

---

## 六、时间信息应该怎么进入 prompt

这部分非常重要。

我不建议继续沿用“插件加载时写一次静态 reminder”。

那样时间会变旧。

我建议改成：

## 6.1 对主回复模型：动态注入时间块

在 `on_prompt_build` 时动态注入，例如：

```text
【时间感知】
- 现在是：2026年3月22日，周日，深夜
- 当前时段：夜深，环境偏安静
- 距离对方上次说话：约 37 分钟
- 你主观上的感受：刚才那段对话的余温还在，但沉默已经开始变长
```

这样当前时间永远是新的。

## 6.2 对主动系统：使用更重的内部时间块

主动系统自己的 prompt 应该看到更强的时间信息，例如：

- 现在是什么时段
- 已沉默多久
- 上次主动距离现在多久
- 当前余温强度
- 当前等待压强
- 当前冷却状态

也就是说：

- 主回复模型看到轻量时间块
- 主动系统看到重度时间块

---

## 七、时间阶段模型

为了加强等待感，我建议引入时间阶段，而不是只看分钟数。

例如：

### 阶段 1：余温期

刚结束聊天不久。

特点：

- 对话还在脑中回响
- 更容易延续刚才的话题
- 更适合 continuation 型主动

### 阶段 2：悬停期

过了一会，但还不算久。

特点：

- 会轻微在意对方是否在忙
- 会开始形成“要不要说点什么”的犹豫

### 阶段 3：牵挂期

沉默变长了。

特点：

- 时间开始有重量
- 更容易形成 care / curiosity 型主动
- 主观等待感明显增强

### 阶段 4：收回期

时间再往后，若一直没有回应，就开始收回来。

特点：

- 不会无限增强主动欲
- 会进入收缩与冷却
- 更像真实的人际节奏

这个阶段模型很重要，因为它直接让“时间有形状”。

---

## 八、`query_time` 工具如何处理

我建议：

### 不删除功能，但迁移归属

`query_time` 仍然保留，但从：

- `time_awareness_plugin.tools.query_time`

迁移到：

- `proactive_message_plugin.tools.query_time`

意义变成：

- 它不再代表“有个单独时间插件”
- 而是代表“主动系统本身也提供时间能力”

这样主模型依然可以主动查时间，
但整个系统结构更统一。

---

## 九、配置整合建议

未来建议把 `time_awareness_plugin` 的配置并入 `config/plugins/proactive_message_plugin/config.toml`。

建议新增一个 `[time_awareness]` 节。

例如：

```toml
[time_awareness]
enabled = true
inject_prompt = true
use_chinese_shichen = false
use_zodiac = true
use_ke = true
custom_format = ""

[temporal_dynamics]
enable_subjective_wait = true
afterglow_decay_minutes = 20
care_growth_start_minutes = 15
silence_deepening_start_minutes = 30
initiative_fatigue_recover_minutes = 180
```

这样就能把：

- 时间格式
- prompt 注入
- 主观时间演化

统一管理。

---

## 十、迁移策略

我建议分两步迁移。

## 第一步：软整合

先在 `proactive_message_plugin` 中复制并接入：

- 时间格式化函数
- `query_time` 工具
- 动态时间 prompt 注入
- 时间状态统一

但此时先不删 `time_awareness_plugin`，只是停用。

好处是：

- 风险低
- 便于比较整合前后效果

## 第二步：正式移除

等确认新版本稳定后：

1. 从 `core.toml` 移除 `time_awareness_plugin`
2. 删除其配置文件引用
3. 删除插件目录
4. 在报告中说明迁移已完成

---

## 十一、整合后的收益

整合后会有几个明显收益。

### 1. 时间感更真实

因为时间不再只是静态信息，而是直接作用于等待感和主动欲。

### 2. 主动性更自然

因为主动行为有了时间语境，不会像凭空冒出来。

### 3. 结构更简单

不再维护两个都在管“时间”的插件。

### 4. 后续扩展更顺

以后如果要继续增强：

- 深夜心境
- 昼夜节律
- 睡眠影响主动性
- 某个念头悬置了多久

都直接接在一个系统里，不用跨插件缝合。

---

## 十二、我建议的最终定位

整合完成后，`proactive_message_plugin` 的真实定位应该变成：

## 爱莉的时间感知主动系统

它不是单纯“主动发消息”。

它负责的是：

- 现在是什么时候
- 你们沉默了多久
- 这段沉默对她意味着什么
- 她是否因此产生新的在意、余温、牵挂、好奇或收缩
- 她会不会因此主动来找你

这就把你最在意的那条哲学保住了，而且比现在更强。

---

## 十三、结论

`time_awareness_plugin` 作为独立插件，确实已经没有太大必要继续存在。

但它的哲学价值不是被删除，而是应该被提升。

最合理的做法不是“砍掉时间感知”，而是：

### 把时间感知彻底并入主动系统，让时间真正成为爱莉内部生活的一部分。

如果下一步要实现，我建议就按这份方案来：

1. 先做状态统一
2. 再做动态时间 prompt 注入
3. 再迁移 `query_time`
4. 最后加主观时间阶段与等待演化

这样做出来的效果，会比现在“两个松散插件”强很多。

