# Minecraft 具身体验 - 下一步实施清单

## ✅ 已完成（Phase 1）

### 核心架构改进
- [x] 重写所有 prompts，强调主体性和第一人称体验
- [x] 创建 `social.py` - 社交临在系统
  - `SocialPresence`: 感知他人存在和情感反应
  - `MinecraftChat`: 游戏内聊天系统
  - `PlayerPresence`: 玩家临在感知数据结构
- [x] 扩展 `consciousness.py` - 意识层
  - `get_bodily_feeling()`: 将状态转换为主观感受
  - `get_visual_context()`: 视觉环境描述
  - `get_social_context()`: 社交环境感知
  - `process_chat_message()`: 处理聊天消息
  - `send_chat()`: 发送聊天消息
- [x] 改进 prompt 注入系统
  - `CONSCIOUSNESS_SYSTEM`: 强调选择自由和体验本质
  - `CONSCIOUSNESS_OBSERVATION`: 第一人称感知框架
  - `HEARTBEAT_MINECRAFT_ACTIVE/IDLE`: 区分游戏状态
  - `TOOL_DESCRIPTION`: 强调这是"通向世界的门"
- [x] 完整设计文档 `minecraft_embodiment_design.md`

### 设计理念确立
- [x] 明确主体性第一原则
- [x] 建立第一人称感知框架
- [x] 定义情感临在系统
- [x] 确立体验连续性机制

## 🚧 Phase 2: 多人游戏支持（关键优先级）

> **目标**: 让爱莉能够和 Ayer 在同一个 Minecraft 世界中玩

### 2.1 Mod API 开发（高优先级）⭐⭐⭐⭐⭐

**为什么需要 Mod**:
- 纯视觉方案无法准确获取玩家位置、聊天内容
- VLA 无法精确识别 Ayer 在做什么
- 需要实时的游戏状态数据支持社交临在系统

**Mod 功能需求**:
```java
// ElysiaCompanionMod - 轻量级信息桥接 Mod

public class GameStateAPI {
    // 1. 玩家信息
    public List<PlayerInfo> getNearbyPlayers(int radius);
    public PlayerInfo getPlayer(String name);
    
    // 2. 聊天监听
    public void onChatMessage(String player, String message);
    public void sendChatMessage(String message);
    
    // 3. 自身状态
    public PlayerState getSelfState();
    // - position (x, y, z)
    // - health, hunger
    // - dimension
    // - inventory items
    
    // 4. 环境信息
    public List<Entity> getNearbyEntities(int radius);
    public BlockInfo getBlockAt(int x, int y, int z);
}

// HTTP/WebSocket 接口
// GET  /api/players - 获取附近玩家
// GET  /api/self - 获取自己的状态
// GET  /api/chat - 获取聊天历史
// POST /api/chat - 发送聊天
// WS   /ws - 实时事件流（聊天、玩家移动）
```

**实施步骤**:
1. [ ] 创建 NeoForge Mod 项目结构
   ```bash
   cd /root/Elysia/Minecraft-Mods
   # 使用 NeoForge MDK 创建项目
   ```

2. [ ] 实现核心 API 类
   - `GameStateProvider.java` - 游戏状态读取
   - `ChatListener.java` - 聊天监听
   - `HTTPServer.java` - HTTP 接口（基于 Javalin 或内嵌 Jetty）

3. [ ] 实现 WebSocket 事件流
   - 实时推送聊天消息
   - 实时推送玩家位置变化

4. [ ] 测试 Mod
   - 单人测试
   - 多人服务器测试
   - 性能测试（确保不影响游戏性能）

**参考资源**:
- NeoForge 文档: https://docs.neoforged.net/
- 类似项目: MineRL, Malmo (但我们需要更轻量的实现)

### 2.2 Python 侧 Mod 客户端（高优先级）⭐⭐⭐⭐

**创建新文件**: `plugins/life_engine/minecraft/mod_client.py`

```python
"""Minecraft Mod API 客户端"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger("life_engine.minecraft.mod_client")


@dataclass(slots=True)
class PlayerInfo:
    """玩家信息"""
    name: str
    x: float
    y: float
    z: float
    health: float
    activity: str  # "mining", "building", "idle", etc.


class ModAPIClient:
    """Mod API 客户端"""
    
    def __init__(self, base_url: str = "http://localhost:25580"):
        self._base_url = base_url
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
    
    async def connect(self) -> bool:
        """连接到 Mod API"""
        try:
            self._session = aiohttp.ClientSession()
            # 测试连接
            async with self._session.get(f"{self._base_url}/api/health") as resp:
                return resp.status == 200
        except Exception as exc:
            logger.error(f"无法连接到 Mod API: {exc}")
            return False
    
    async def get_nearby_players(self, radius: int = 50) -> list[PlayerInfo]:
        """获取附近玩家"""
        try:
            async with self._session.get(
                f"{self._base_url}/api/players",
                params={"radius": radius}
            ) as resp:
                data = await resp.json()
                return [PlayerInfo(**p) for p in data["players"]]
        except Exception as exc:
            logger.warning(f"获取玩家信息失败: {exc}")
            return []
    
    async def get_self_state(self) -> dict[str, Any]:
        """获取自己的状态"""
        async with self._session.get(f"{self._base_url}/api/self") as resp:
            return await resp.json()
    
    async def send_chat(self, message: str) -> bool:
        """发送聊天消息"""
        try:
            async with self._session.post(
                f"{self._base_url}/api/chat",
                json={"message": message}
            ) as resp:
                return resp.status == 200
        except Exception as exc:
            logger.error(f"发送聊天失败: {exc}")
            return False
    
    async def subscribe_events(self, callback) -> None:
        """订阅实时事件（聊天、玩家移动）"""
        try:
            self._ws = await self._session.ws_connect(f"{self._base_url}/ws")
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = msg.json()
                    await callback(data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except Exception as exc:
            logger.error(f"WebSocket 连接错误: {exc}")
```

**集成到 MinecraftSession**:
```python
# consciousness.py 中添加

async def start(self, goal: str = "") -> dict[str, Any]:
    # ... 现有代码 ...
    
    # 连接到 Mod API
    self._mod_client = ModAPIClient()
    connected = await self._mod_client.connect()
    if connected:
        logger.info("已连接到 Minecraft Mod API")
        # 启动事件监听
        asyncio.create_task(self._listen_mod_events())
    else:
        logger.warning("Mod API 未连接，某些功能可能受限")
```

### 2.3 社交临在增强（中优先级）⭐⭐⭐

**更新 `social.py`**:
```python
class SocialPresence:
    def __init__(self, mod_client: ModAPIClient | None = None):
        self._mod_client = mod_client
        # ... 现有代码 ...
    
    async def update_from_mod(self) -> None:
        """从 Mod API 更新玩家信息"""
        if not self._mod_client:
            return
        
        players = await self._mod_client.get_nearby_players()
        for player in players:
            self.update_player(
                name=player.name,
                position={"x": player.x, "y": player.y, "z": player.z},
                activity=player.activity,
            )
            
            # 计算距离
            if self._self_position:
                distance = self._calculate_distance(
                    self._self_position,
                    {"x": player.x, "y": player.y, "z": player.z}
                )
                self._players[player.name].distance = distance
```

### 2.4 聊天系统完善（中优先级）⭐⭐⭐

**目标**: 实现完整的游戏内聊天

**改进 `social.py` 中的 `MinecraftChat`**:
```python
class MinecraftChat:
    def __init__(self, social_presence: SocialPresence, mod_client: ModAPIClient | None = None):
        self._social = social_presence
        self._mod_client = mod_client
        self._chat_history: list[dict[str, Any]] = []
    
    async def send_chat(self, message: str) -> bool:
        """发送聊天消息（优先使用 Mod API）"""
        if self._mod_client:
            # 使用 Mod API（更可靠）
            return await self._mod_client.send_chat(message)
        else:
            # 降级到键盘输入
            return await self._send_via_keyboard(message)
    
    async def _send_via_keyboard(self, message: str) -> bool:
        """通过键盘输入发送聊天（降级方案）"""
        # 实现文本输入
        # 这需要将文本拆分为按键序列
        pass
```

**聊天事件处理**:
```python
# consciousness.py 中添加

async def _listen_mod_events(self) -> None:
    """监听 Mod 事件"""
    async def handle_event(event: dict):
        event_type = event.get("type")
        
        if event_type == "chat":
            # 处理聊天消息
            player = event["player"]
            message = event["message"]
            await self.process_chat_message(player, message)
        
        elif event_type == "player_move":
            # 更新玩家位置
            await self._social.update_from_mod()
    
    await self._mod_client.subscribe_events(handle_event)
```

## 🎨 Phase 3: 增强互动（2-3 周）

### 3.1 视觉理解增强 ⭐⭐⭐⭐

**目标**: 更好地理解游戏画面

**创建**: `plugins/life_engine/minecraft/vision.py`

```python
"""视觉理解系统"""

class MinecraftVision:
    """Minecraft 视觉理解"""
    
    async def analyze_scene(self, frame: PILImage.Image) -> dict:
        """分析场景"""
        # 使用 VLM 模型（如 GPT-4V, Claude 3）分析截图
        prompt = """
        这是 Minecraft 游戏的截图。请简要描述：
        1. 环境（生物群系、时间、天气）
        2. 我看到的建筑或结构
        3. 附近的生物或玩家
        4. 当前正在做什么
        
        用第一人称、简洁的语言描述。
        """
        
        description = await self._call_vlm(frame, prompt)
        return {
            "description": description,
            "environment": self._extract_environment(description),
            "entities": self._extract_entities(description),
        }
    
    async def identify_ayer(self, frame: PILImage.Image) -> dict | None:
        """识别 Ayer 的位置（从视觉）"""
        prompt = """
        这是 Minecraft 游戏截图。
        请判断画面中是否有其他玩家，
        如果有，描述他们的位置和正在做什么。
        """
        # ...
```

**集成到意识层**:
```python
# consciousness.py

async def perceive(self) -> str:
    """感知当前画面"""
    frame = await self._capture.grab_consciousness_frame()
    
    # 视觉分析
    if self._vision:
        scene = await self._vision.analyze_scene(frame.image)
        visual_desc = scene["description"]
    else:
        visual_desc = "（无法分析画面）"
    
    # 结合社交信息
    social_desc = self._social.get_social_context()
    
    # 组合为第一人称感知
    perception = f"{visual_desc}\n\n{social_desc}"
    return perception
```

### 3.2 导航系统 ⭐⭐⭐

**创建**: `plugins/life_engine/minecraft/navigator.py`

```python
"""导航系统"""

class PathNavigator:
    """路径导航"""
    
    async def navigate_to(self, target_pos: dict, input_ctrl: InputController):
        """导航到目标位置"""
        current = await self._get_current_position()
        
        # 简单的直线导航（后续可改进为 A*）
        while self._distance(current, target_pos) > 2:
            # 计算方向
            angle = self._calculate_angle(current, target_pos)
            
            # 调整视角
            await self._turn_to_angle(angle, input_ctrl)
            
            # 前进
            await input_ctrl.walk_forward(duration=1.0)
            
            # 更新位置
            current = await self._get_current_position()
    
    async def follow_player(
        self,
        player_name: str,
        distance: float = 3.0,
        input_ctrl: InputController
    ):
        """跟随玩家"""
        while True:
            player_pos = await self._mod_client.get_player_position(player_name)
            current_pos = await self._get_current_position()
            
            dist = self._distance(current_pos, player_pos)
            
            if dist > distance + 2:
                # 太远了，走近
                await self._move_towards(player_pos, input_ctrl)
            elif dist < distance - 1:
                # 太近了，后退
                await self._move_away_from(player_pos, input_ctrl)
            
            await asyncio.sleep(0.5)
```

## 🏗️ Phase 4: 建造能力（1-2 月）

### 4.1 简单建造 ⭐⭐⭐

**创建**: `plugins/life_engine/minecraft/builder.py`

```python
"""建造系统"""

class SimpleBuilder:
    """简单建造系统"""
    
    async def place_block(
        self,
        block_type: str,
        position: dict,
        input_ctrl: InputController
    ):
        """在指定位置放置方块"""
        # 1. 导航到目标位置附近
        await self._navigator.navigate_to(position)
        
        # 2. 选择对应的物品槽
        slot = await self._find_item_slot(block_type)
        if slot:
            await input_ctrl.select_slot(slot)
        
        # 3. 调整视角对准放置位置
        await self._aim_at_position(position, input_ctrl)
        
        # 4. 右键放置
        await input_ctrl.place_block()
    
    async def build_simple_structure(self, structure_type: str):
        """建造简单结构"""
        if structure_type == "pink_cottage":
            await self._build_pink_cottage()
    
    async def _build_pink_cottage(self):
        """建造粉色小屋"""
        # 地基
        for x in range(6):
            for z in range(6):
                await self.place_block("oak_planks", {"x": x, "y": 0, "z": z})
        
        # 墙壁
        # ...
```

## 📝 配置更新

### 更新 `config/plugins/life_engine/config.toml`

```toml
[minecraft]
enabled = true  # 改为 true

# 多人游戏配置
mode = "multiplayer"  # "singleplayer" | "multiplayer"
server_address = "localhost"
server_port = 25565

# Mod API 配置
mod_api_enabled = true
mod_api_url = "http://localhost:25580"

# 社交配置
auto_respond_to_ayer = true  # 自动倾向于回应 Ayer
personality_mode = "elysia"  # 使用爱莉的性格配置

# 视觉理解
vision_enabled = true
vision_model_task = "vlm"  # 使用哪个 VLM 模型

# 导航
navigation_enabled = true
follow_distance = 3.0  # 跟随 Ayer 时保持的距离
```

## 🧪 测试计划

### 单元测试
- [ ] `test_social_presence.py` - 社交临在系统
- [ ] `test_mod_client.py` - Mod API 客户端
- [ ] `test_chat.py` - 聊天系统

### 集成测试
- [ ] Mod API 连接测试
- [ ] 聊天收发测试
- [ ] 玩家位置更新测试

### 体验测试
- [ ] Ayer 邀请爱莉玩游戏
- [ ] 爱莉感知 Ayer 的存在
- [ ] 爱莉跟随 Ayer
- [ ] 游戏内聊天互动
- [ ] 爱莉自主决策（继续/离开）

## 📅 时间线估计

### 第 1-2 周：Mod 开发
- Mod 基础框架
- HTTP API 实现
- WebSocket 事件流
- 测试和调优

### 第 3 周：Python 集成
- ModAPIClient 实现
- 社交临在更新
- 聊天系统完善
- 集成测试

### 第 4-5 周：增强功能
- 视觉理解
- 导航系统
- 初步建造能力

### 第 6-8 周：完善和优化
- 性格表达系统
- 复杂建造
- 记忆整合
- 用户体验优化

## 🎯 里程碑目标

### M1: 基础连接（2 周）
- ✅ 爱莉能进入多人游戏
- ✅ 能感知 Ayer 的存在
- ✅ 能收发游戏内聊天

### M2: 基础互动（4 周）
- ✅ 能跟随 Ayer 移动
- ✅ 能对 Ayer 的聊天做出反应
- ✅ 能执行简单的意图（走到某处、观察）

### M3: 深度体验（8 周）
- ✅ 能参与建造活动
- ✅ 能记住和 Ayer 的游戏经历
- ✅ 体现出个性和情感

## 💡 技术债务和已知限制

### 当前限制
- VLA 模型对 Minecraft 的适配性未知（可能需要微调）
- 文本输入（降级方案）尚未实现
- 视觉理解依赖外部 VLM，有延迟

### 需要改进
- VLA 执行成功率监控
- 错误恢复机制
- 性能优化（降低延迟）

### 技术选型待定
- Mod 的 HTTP 框架选择（Javalin vs Jetty）
- VLM 模型选择（GPT-4V vs Claude 3 vs 本地模型）
- 导航算法（简单直线 vs A*）

## 📚 参考资源

- NeoForge 文档: https://docs.neoforged.net/
- Minecraft Protocol: https://wiki.vg/Protocol
- MineRL 项目: https://minerl.io/
- Voyager (MC AI Agent): https://github.com/MineDojo/Voyager

---

**下一步行动**: 开始 Mod 开发，这是实现"和 Ayer 一起玩"的关键基础设施。
