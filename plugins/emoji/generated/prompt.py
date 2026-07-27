"""生成表情包 prompt preset。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmojiStylePreset:
    """生成表情包风格预设。"""

    name: str
    aliases: tuple[str, ...]
    aspect_ratio: str
    prompt: str
    negative: str = ""


STYLE_PRESETS: dict[str, EmojiStylePreset] = {
    "chibi_sticker": EmojiStylePreset(
        name="chibi_sticker",
        aliases=("chibi", "sticker", "表情", "表情包", "q版"),
        aspect_ratio="square",
        prompt=(
            "super cute chibi sticker, large expressive eyes, simplified body, "
            "rounded silhouette, centered character, clean pastel background, "
            "high readability as a chat sticker"
        ),
    ),
    "soft_illustration": EmojiStylePreset(
        name="soft_illustration",
        aliases=("soft", "温柔", "插画", "illustration"),
        aspect_ratio="square",
        prompt=(
            "soft emotional anime illustration, gentle lighting, delicate pastel colors, "
            "quiet intimate atmosphere, storybook-like warmth"
        ),
    ),
    "meme_reaction": EmojiStylePreset(
        name="meme_reaction",
        aliases=("meme", "梗图", "reaction", "吐槽"),
        aspect_ratio="square",
        prompt=(
            "expressive reaction image, exaggerated comedic anime expression, "
            "clear silhouette, dynamic pose, punchy composition, still polished and cute"
        ),
    ),
    "sleepy_goodnight": EmojiStylePreset(
        name="sleepy_goodnight",
        aliases=("sleepy", "晚安", "困困", "goodnight"),
        aspect_ratio="square",
        prompt=(
            "sleepy bedtime sticker illustration, cozy pillow, warm night light, "
            "soft blanket, tender goodnight mood, calm and safe atmosphere"
        ),
    ),
    "angry_cute": EmojiStylePreset(
        name="angry_cute",
        aliases=("angry", "生气", "凶凶", "叉腰"),
        aspect_ratio="square",
        prompt=(
            "cute angry chibi reaction, puffed cheeks, hands on hips, not threatening, "
            "playful scolding mood, crisp sticker composition"
        ),
    ),
    "workbench": EmojiStylePreset(
        name="workbench",
        aliases=("work", "代码", "工作台", "debug", "调试"),
        aspect_ratio="landscape",
        prompt=(
            "cozy digital workbench scene, anime girl companion looking at logs and code, "
            "monitor glow, tidy desk, focused but warm expression"
        ),
    ),
}


def resolve_style(style: str | None) -> EmojiStylePreset:
    """根据模型给出的风格名或别名解析预设。"""

    raw = str(style or "").strip().lower()
    if raw in STYLE_PRESETS:
        return STYLE_PRESETS[raw]
    for preset in STYLE_PRESETS.values():
        if raw in {alias.lower() for alias in preset.aliases}:
            return preset
    return STYLE_PRESETS["chibi_sticker"]


def list_style_names() -> list[str]:
    """返回可用风格名。"""

    return list(STYLE_PRESETS)
