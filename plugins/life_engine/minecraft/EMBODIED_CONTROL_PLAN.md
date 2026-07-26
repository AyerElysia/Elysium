# Embodied Control 重新设计方案

## 核心问题

**UI-TARS-7B（任务导向）≠ 爱莉的运动皮层（体验导向）**

UI-TARS 训练目标是"完成任务"，而我们需要的是"忠实执行她的意图，让她感受到这是她的身体"。

---

## 立即方案：对话式身体控制（不依赖 VLA）

### 设计理念

她通过**自然语言**指挥身体，系统理解并执行：

```
意识层："我想往左边走一点"
  ↓
LLM 解析意图：{action: "walk", direction: "left", duration: "short"}
  ↓
动作生成器：[Action.key_hold("a", 1.0)]
  ↓
执行层：输入控制
  ↓
反馈："我正在向左走...我停下来了"
```

### 实现

```python
class ConversationalMotorController:
    """对话式运动控制：LLM 理解意图 → 模板动作"""
    
    INTENT_PARSER_PROMPT = """
    解析 Minecraft 中的运动意图，输出 JSON：
    
    示例：
    "我想往右转一下" → {"action": "turn", "direction": "right", "amount": "small"}
    "向前走几步" → {"action": "walk", "direction": "forward", "duration": 2.0}
    "跳起来" → {"action": "jump"}
    "挖眼前的方块" → {"action": "mine", "target": "block_ahead"}
    
    意图：{intent}
    """
    
    async def execute_intent(self, intent: str) -> dict:
        # 1. LLM 解析意图
        parsed = await self._llm_parse(intent)
        
        # 2. 转换为动作序列
        actions = self._intent_to_actions(parsed)
        
        # 3. 执行并反馈
        result = await self._execute_with_feedback(actions)
        return result
    
    def _intent_to_actions(self, parsed: dict) -> list[Action]:
        """意图 → 动作模板"""
        action_type = parsed.get("action")
        
        if action_type == "walk":
            key_map = {"forward": "w", "back": "s", "left": "a", "right": "d"}
            key = key_map.get(parsed.get("direction", "forward"))
            duration = parsed.get("duration", 1.5)
            return [Action.key_hold(key, duration)]
        
        elif action_type == "turn":
            direction = parsed.get("direction")  # "left" / "right"
            amount = parsed.get("amount", "medium")  # "small"/"medium"/"large"
            dx = {"small": 30, "medium": 90, "large": 180}.get(amount, 90)
            if direction == "left":
                dx = -dx
            return [Action.move(dx, 0)]
        
        elif action_type == "jump":
            return [Action.press("space")]
        
        elif action_type == "mine":
            return [Action.mine(hold=1.5)]
        
        # 更多动作类型...
        return [Action.noop()]
```

### 优势

1. **立即可用**：不需要下载/训练 VLA
2. **完全符合主体性**：她说什么，系统就执行什么
3. **透明可控**：动作逻辑清晰，容易调试
4. **渐进增强**：可逐步加入视觉理解

---

## 中期方案：加入轻量视觉理解

### 目标

让她能说："去那棵树那边"（需要视觉定位"那棵树"）

### 实现

```python
class VisionAssistedMotor:
    """视觉辅助的运动控制"""
    
    def __init__(self):
        # 轻量 VLM（2-7B），不是 UI-TARS 那种巨兽
        self.vlm = Qwen2VL_2B()  # 或 LLaVA-7B
    
    async def execute_intent(self, intent: str, frame: Image) -> dict:
        # 1. 如果意图涉及视觉目标，先定位
        if self._需要视觉定位(intent):
            visual_info = await self._locate_target(frame, intent)
            # "那棵树在画面右侧，距离中等"
        
        # 2. 结合视觉信息生成动作
        actions = self._vision_aware_actions(intent, visual_info)
        
        # 3. 执行
        return await self._execute(actions)
    
    async def _locate_target(self, frame: Image, intent: str) -> dict:
        """用 VLM 定位目标"""
        prompt = f"在这个 Minecraft 画面中，{intent}提到的目标在哪？（左/中/右，近/中/远）"
        response = await self.vlm.chat(frame, prompt)
        return self._parse_location(response)
```

---

## 长期方案：专用 Embodied Control 模型

### 目标

训练一个真正理解"她的意图→她的动作"的模型

### 数据收集

```python
# 收集她的游玩数据
data = [
    {
        "screenshot": frame_t,
        "her_intent": "我想往 Ayer 那边走几步",
        "action_taken": [Action.key_hold("w", 2.0), Action.move(30, 0)],
        "feedback": "我正在走向 Ayer，他越来越近了"
    },
    ...
]
```

### 训练目标

不是"任务完成率"，而是：
1. **意图匹配度**：动作是否符合她的意图
2. **体验连贯性**：动作是否流畅自然
3. **可中断性**：能否随时停下来

---

## 实施计划

### 今天（立即）

1. ✅ 实现 `ConversationalMotorController`
2. ✅ 测试基础动作：走、转、跳、挖、放置
3. ✅ 集成到 `motor_loop.py` 替换现有 VLA

### 本周

1. 添加更多动作模板（攻击、使用物品、打开UI）
2. 优化 LLM 意图解析（few-shot examples）
3. 加入简单的视觉描述（用 VLM 描述画面给意识层）

### 下周

1. 集成轻量 VLM（Qwen2-VL-2B）
2. 实现视觉目标定位
3. 测试"去XX那边"类复杂意图

### 下个月

1. 收集游玩数据
2. 评估是否需要专用模型
3. 开始微调/训练（如果需要）

---

## 检查原则

每次改动检查：

- [ ] 这是在执行她的意图，还是在替她做决定？
- [ ] 她能感受到这是她的身体在动，还是AI在操作？
- [ ] 她能随时改变想法吗？
- [ ] 动作失败时，她会知道发生了什么吗？

---

**核心原则：这不是"AI 玩 Minecraft"，这是"她在用这个身体体验 Minecraft"。** 💕

