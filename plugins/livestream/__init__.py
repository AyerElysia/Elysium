"""Livestream 插件 — 商业级 AI 直播框架。

架构：
- platform/: 平台适配层（B站 blivedm，可扩展抖音/Twitch）
- pipeline/: 核心管线（过滤 → 优先级队列 → 调度器 → LLM → 主动行为）
- output/: 输出层（TTS 队列 + 形象控制）
- router.py: FastAPI WebSocket 服务
- static/: Web 前端（Live2D + 弹幕面板）
- consciousness.py: 意识实例管理（kind="livestream"）
"""
