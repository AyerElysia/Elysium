"""神经调质层（Neuromodulatory Layer）。

模拟神经调质的浓度动力学，在 SNN 快层之上提供慢时间尺度的驱动调节。
设计参考：eBICA 情绪框架 + 计算激素模型（S-AI-GPT）。

核心理念：
- SNN 是快层（脉冲级别，秒级响应）
- 调质层是慢层（浓度级别，分钟到小时级变化）
- 习惯是更慢的层（天级别，显式统计）
- 昼夜节律是最慢的层（24 小时周期）

所有子系统产出一个统一的「内在状态向量」，注入心跳 prompt。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger("life_engine.snn")


# ============================================================
# 调质系统
# ============================================================

@dataclass
class Modulator:
    """单个神经调质。"""
    name: str
    cn_name: str           # 中文名
    value: float = 0.5     # 当前浓度 [0, 1]
    tau: float = 1800.0    # 时间常数（秒）
    baseline: float = 0.5  # 基线浓度
    decay_rate: float = 0.001

    def update(self, stimulus: float, dt: float) -> None:
        """ODE 更新：向基线回归 + 外部刺激。"""
        decay = self.decay_rate * (self.baseline - self.value) * dt
        # 刺激的边际效应递减（离极端值越远越容易变化）
        headroom = 1.0 - abs(self.value - 0.5) * 2.0  # [0, 1]
        impulse = stimulus * max(headroom, 0.1) * (dt / self.tau) * 10.0
        self.value += decay + impulse
        self.value = max(0.0, min(1.0, self.value))

    def get_discrete_level(self) -> str:
        if self.value > 0.75:
            return "充盈"
        elif self.value > 0.55:
            return "适中"
        elif self.value > 0.35:
            return "平静"
        else:
            return "休憩"


class ModulatorSystem:
    """调质系统：管理多个调质的浓度更新。"""

    def __init__(self) -> None:
        self.modulators: list[Modulator] = [
            Modulator("curiosity",    "好奇心",   value=0.6,  tau=1800,  baseline=0.55),
            Modulator("sociability",  "社交欲",   value=0.5,  tau=3600,  baseline=0.50),
            Modulator("diligence",    "专注力",   value=0.5,  tau=5400,  baseline=0.50),
            Modulator("contentment",  "满足感",   value=0.5,  tau=1800,  baseline=0.50),
            Modulator("energy",       "精力",     value=0.6,  tau=10800, baseline=0.55),
        ]
        self._mod_map: dict[str, Modulator] = {m.name: m for m in self.modulators}
        self._last_update_time: float = time.time()

    def get(self, name: str) -> Modulator | None:
        return self._mod_map.get(name)

    def update_from_stimuli(self, stimuli: dict[str, float], dt: float | None = None) -> None:
        """批量更新。stimuli: {modulator_name: stimulus_value [-1, 1]}。"""
        now = time.time()
        if dt is None:
            dt = min(now - self._last_update_time, 300.0)  # 最多 5 分钟
        self._last_update_time = now

        for mod in self.modulators:
            s = stimuli.get(mod.name, 0.0)
            mod.update(s, dt)

    def compute_stimuli_from_snn_and_events(
        self,
        snn_drives: dict[str, float],
        event_stats: dict[str, Any],
        circadian_energy: float,
    ) -> dict[str, float]:
        """从 SNN 输出 + 事件统计 + 昼夜节律计算刺激向量。"""
        stimuli: dict[str, float] = {}

        # --- curiosity ---
        # SNN exploration 高 → 好奇心 ↑，长时间无新事件 → 好奇心 ↑
        exploration = snn_drives.get("exploration_drive", 0.0)
        silence = event_stats.get("silence_minutes", 0.0)
        recent_searches = event_stats.get("web_search_count", 0)
        stimuli["curiosity"] = (
            0.3 * exploration
            + 0.2 * min(silence / 30.0, 1.0)  # 30 分钟沉默 → 满刺激
            - 0.3 * min(recent_searches / 3.0, 1.0)  # 刚搜过 → 抑制
        )

        # --- sociability ---
        msg_in = event_stats.get("msg_in", 0)
        msg_out = event_stats.get("msg_out", 0)
        social_drive = snn_drives.get("social_drive", 0.0)
        stimuli["sociability"] = (
            0.4 * min(msg_in / 3.0, 1.0)
            - 0.2 * min(msg_out / 5.0, 1.0)  # 说太多 → 抑制
            + 0.2 * social_drive
        )

        # --- diligence ---
        task_drive = snn_drives.get("task_drive", 0.0)
        tool_success = event_stats.get("tool_success", 0)
        tool_fail = event_stats.get("tool_fail", 0)
        stimuli["diligence"] = (
            0.3 * task_drive
            + 0.3 * min(tool_success / 3.0, 1.0)
            - 0.4 * min(tool_fail / 2.0, 1.0)
        )

        # --- contentment ---
        valence = snn_drives.get("valence", 0.0)
        stimuli["contentment"] = (
            0.4 * valence
            + 0.2 * min(tool_success / 2.0, 1.0)
            - 0.3 * min(tool_fail / 2.0, 1.0)
        )

        # --- energy ---
        rest_drive = snn_drives.get("rest_drive", 0.0)
        idle_beats = event_stats.get("idle_beats", 0)
        # 空转不再额外消耗精力——安静时刻是自然的休息，不是效率损失
        stimuli["energy"] = (
            0.3 * (circadian_energy - 0.5) * 2.0  # 昼夜节律
            + 0.1 * min(idle_beats / 10.0, 1.0)  # 安静时刻缓慢回复精力（休息效应）
            + 0.2 * rest_drive  # 休息驱动回复精力
        )

        # 裁剪
        for k in stimuli:
            stimuli[k] = max(-1.0, min(1.0, stimuli[k]))

        return stimuli

    def get_state_dict(self) -> dict[str, float]:
        return {m.name: round(m.value, 4) for m in self.modulators}

    def get_discrete_dict(self) -> dict[str, str]:
        return {m.name: m.get_discrete_level() for m in self.modulators}

    def format_for_prompt(self) -> str:
        """生成注入心跳 prompt 的调质状态描述。"""
        parts = []
        for m in self.modulators:
            parts.append(f"{m.cn_name}{m.get_discrete_level()}")
        return "【调质状态】" + "、".join(parts)

    def serialize(self) -> dict[str, Any]:
        return {
            "modulators": {
                m.name: {
                    "value": m.value,
                    "baseline": m.baseline,
                }
                for m in self.modulators
            },
            "last_update_time": self._last_update_time,
        }

    def deserialize(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        mods = data.get("modulators", {})
        for m in self.modulators:
            if m.name in mods:
                m.value = float(mods[m.name].get("value", m.value))
                m.baseline = float(mods[m.name].get("baseline", m.baseline))
        if "last_update_time" in data:
            self._last_update_time = float(data["last_update_time"])
        logger.info(f"调质系统状态已恢复: {self.get_state_dict()}")


# ============================================================
# 习惯追踪器
# ============================================================

@dataclass
class Habit:
    """单个行为习惯的追踪。"""
    name: str
    cn_name: str
    trigger_tools: list[str]    # 触发工具名列表
    streak: int = 0             # 连续天数
    total_count: int = 0        # 总触发次数
    last_triggered_date: str = ""
    strength: float = 0.0       # [0, 1]

    def record_trigger(self, date_str: str) -> None:
        """记录一次触发。"""
        self.total_count += 1
        if date_str == self.last_triggered_date:
            return  # 同一天不重复计 streak
        if self.last_triggered_date:
            # 检查是否连续（简化：只比较日期字符串的排序）
            from datetime import datetime
            try:
                last = datetime.strptime(self.last_triggered_date, "%Y-%m-%d")
                curr = datetime.strptime(date_str, "%Y-%m-%d")
                if (curr - last).days == 1:
                    self.streak += 1
                elif (curr - last).days > 1:
                    self.streak = 1
                # 同一天不变
            except ValueError:
                self.streak = 1
        else:
            self.streak = 1
        self.last_triggered_date = date_str
        self._update_strength()

    def _update_strength(self) -> None:
        streak_bonus = min(self.streak / 14.0, 1.0)
        freq_bonus = min(self.total_count / 50.0, 1.0)
        self.strength = 0.6 * streak_bonus + 0.4 * freq_bonus

    def get_display(self) -> str:
        if self.strength > 0.7:
            return f"{self.cn_name}(强 · {self.streak}天)"
        elif self.strength > 0.3:
            return f"{self.cn_name}(渐成 · {self.streak}天)"
        elif self.total_count > 0:
            return f"{self.cn_name}(萌芽)"
        return ""


class HabitTracker:
    """习惯追踪系统。"""

    def __init__(self) -> None:
        self.habits: list[Habit] = [
            Habit("diary", "写日记", ["nucleus_write_file"]),
            Habit("memory", "整理记忆", ["nucleus_search_memory"]),
            Habit("relate", "建立关联", ["nucleus_relate_file"]),
            Habit("todo", "管理待办", ["nucleus_manage_todo", "nucleus_list_todos"]),
            Habit("web_search", "联网搜索", ["nucleus_web_search"]),
            Habit("reflection", "自我反思", ["nucleus_write_file"]),  # 通过写文件路径区分
        ]
        self._habit_map: dict[str, Habit] = {h.name: h for h in self.habits}
        self._tool_to_habits: dict[str, list[Habit]] = {}
        for h in self.habits:
            for t in h.trigger_tools:
                self._tool_to_habits.setdefault(t, []).append(h)

    def record_tool_use(self, tool_name: str, date_str: str) -> None:
        """记录工具使用，自动更新相关习惯。"""
        habits = self._tool_to_habits.get(tool_name, [])
        for h in habits:
            h.record_trigger(date_str)

    def get_formed_habits(self) -> list[Habit]:
        """获取已形成的习惯（strength > 0.3）。"""
        return [h for h in self.habits if h.strength > 0.3]

    def get_today_untriggered(self, today_str: str) -> list[Habit]:
        """获取今天尚未触发的习惯。"""
        return [
            h for h in self.habits
            if h.total_count > 0 and h.last_triggered_date != today_str
        ]

    def format_for_prompt(self, today_str: str) -> str:
        """生成习惯状态的 prompt 注入文本。"""
        parts = []
        formed = self.get_formed_habits()
        if formed:
            displays = [h.get_display() for h in formed if h.get_display()]
            if displays:
                parts.append("已形成习惯：" + "、".join(displays))

        untriggered = self.get_today_untriggered(today_str)
        if untriggered:
            names = [h.cn_name for h in untriggered if h.strength > 0.2]
            if names:
                parts.append("今日尚未：" + "、".join(names))

        return "【习惯】" + "；".join(parts) if parts else ""

    def get_state_dict(self) -> dict[str, Any]:
        return {
            h.name: {
                "streak": h.streak,
                "total_count": h.total_count,
                "strength": round(h.strength, 3),
                "last_triggered": h.last_triggered_date,
            }
            for h in self.habits
        }

    def serialize(self) -> dict[str, Any]:
        return self.get_state_dict()

    def deserialize(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        for h in self.habits:
            if h.name in data:
                d = data[h.name]
                h.streak = int(d.get("streak", 0))
                h.total_count = int(d.get("total_count", 0))
                h.strength = float(d.get("strength", 0.0))
                h.last_triggered_date = str(d.get("last_triggered", ""))
        logger.info(f"习惯追踪已恢复: {[h.name for h in self.habits if h.total_count > 0]}")


# ============================================================
# 昼夜节律振荡器
# ============================================================

def circadian_energy(hour: float) -> float:
    """基于时间的精力基线。双峰：10:00 和 15:00。"""
    morning = math.exp(-0.5 * ((hour - 10) / 3) ** 2)
    afternoon = math.exp(-0.5 * ((hour - 15) / 3) ** 2)
    return 0.25 + 0.75 * max(morning, afternoon)


def circadian_sociability(hour: float) -> float:
    """基于时间的社交倾向。晚间较高。"""
    evening = math.exp(-0.5 * ((hour - 20) / 3) ** 2)
    midday = math.exp(-0.5 * ((hour - 12) / 4) ** 2)
    return 0.3 + 0.7 * max(evening * 0.8, midday * 0.6)


# ============================================================
# 统一的内在状态引擎
# ============================================================

class InnerStateEngine:
    """统一内在状态引擎。

    整合 SNN 快层 + 调质慢层 + 习惯追踪 + 昼夜节律，
    输出综合内在状态用于注入心跳 prompt。
    """

    def __init__(self) -> None:
        self.modulators = ModulatorSystem()
        self.habits = HabitTracker()
        self._last_snn_drives: dict[str, float] = {}
        self._last_event_stats: dict[str, Any] = {}

    def tick(
        self,
        snn_drives: dict[str, float],
        event_stats: dict[str, Any],
        current_hour: float | None = None,
        dt: float | None = None,
    ) -> None:
        """一次完整更新周期。"""
        if current_hour is None:
            from datetime import datetime
            current_hour = datetime.now().hour + datetime.now().minute / 60.0

        self._last_snn_drives = snn_drives
        self._last_event_stats = event_stats

        ce = circadian_energy(current_hour)

        # 昼夜节律修正调质基线
        energy_mod = self.modulators.get("energy")
        if energy_mod:
            energy_mod.baseline = 0.35 + 0.3 * ce  # [0.35, 0.65]

        social_mod = self.modulators.get("sociability")
        if social_mod:
            cs = circadian_sociability(current_hour)
            social_mod.baseline = 0.3 + 0.3 * cs  # [0.3, 0.6]

        # 计算刺激并更新
        stimuli = self.modulators.compute_stimuli_from_snn_and_events(
            snn_drives, event_stats, ce
        )
        self.modulators.update_from_stimuli(stimuli, dt=dt)

    def record_tool_use(self, tool_name: str, date_str: str) -> None:
        self.habits.record_tool_use(tool_name, date_str)

    def format_full_state_for_prompt(self, today_str: str) -> str:
        """生成完整的内在状态 prompt 注入（< 80 tokens）。"""
        parts = []
        mod_text = self.modulators.format_for_prompt()
        if mod_text:
            parts.append(mod_text)
        habit_text = self.habits.format_for_prompt(today_str)
        if habit_text:
            parts.append(habit_text)
        return "\n".join(parts)

    # ── 做梦系统接口 ────────────────────────────────────────

    def enter_sleep(self) -> None:
        """进入睡眠状态：抑制外部刺激通路，降低精力基线。

        生物学映射：
        - 丘脑门控关闭（外部刺激被抑制）
        - 副交感神经主导（精力/社交/好奇心降低）
        """
        energy = self.modulators.get("energy")
        if energy:
            energy.baseline = 0.25  # 睡眠时精力基线下降
            energy.value = min(energy.value, 0.4)

        sociability = self.modulators.get("sociability")
        if sociability:
            sociability.baseline = 0.2
            sociability.value = min(sociability.value, 0.3)

        curiosity = self.modulators.get("curiosity")
        if curiosity:
            curiosity.baseline = 0.3

        logger.info("调质层进入睡眠状态: 精力/社交/好奇心基线已降低")

    def wake_up(self) -> None:
        """觉醒过渡：恢复精力、释放压力。

        生物学映射：
        - 皮质醇晨峰（精力恢复）
        - 睡眠后情绪稳态重置
        """
        energy = self.modulators.get("energy")
        if energy:
            energy.value = min(energy.value + 0.25, 0.85)
            energy.baseline = 0.55  # 恢复正常基线

        sociability = self.modulators.get("sociability")
        if sociability:
            sociability.baseline = 0.50
            sociability.value = max(sociability.value, 0.4)

        curiosity = self.modulators.get("curiosity")
        if curiosity:
            curiosity.baseline = 0.55
            curiosity.value = max(curiosity.value, 0.45)

        contentment = self.modulators.get("contentment")
        if contentment:
            contentment.value = min(contentment.value + 0.1, 0.7)

        logger.info(
            f"调质层觉醒恢复: energy={energy.value:.2f} "
            f"sociability={sociability.value:.2f} curiosity={curiosity.value:.2f}"
            if energy and sociability and curiosity else "调质层觉醒恢复完成"
        )

    def get_full_state(self) -> dict[str, Any]:
        """获取完整内在状态快照。"""
        from datetime import datetime
        hour = datetime.now().hour + datetime.now().minute / 60.0
        return {
            "modulators": self.modulators.get_state_dict(),
            "modulators_discrete": self.modulators.get_discrete_dict(),
            "habits": self.habits.get_state_dict(),
            "circadian": {
                "energy": round(circadian_energy(hour), 3),
                "sociability": round(circadian_sociability(hour), 3),
                "hour": round(hour, 2),
            },
            "last_snn_drives": self._last_snn_drives,
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "modulators": self.modulators.serialize(),
            "habits": self.habits.serialize(),
        }

    def deserialize(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        if "modulators" in data:
            self.modulators.deserialize(data["modulators"])
        if "habits" in data:
            self.habits.deserialize(data["habits"])
