"""life_engine 静态面板响应工具。"""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import HTMLResponse

_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def render_dashboard(filename: str, missing_title: str) -> HTMLResponse:
    """读取 static 下的 dashboard HTML 并包装为响应。"""
    dashboard_path = _STATIC_DIR / filename
    if not dashboard_path.is_file():
        return HTMLResponse(
            content=f"<h1>{missing_title} HTML not found!</h1>",
            status_code=404,
        )
    return HTMLResponse(content=dashboard_path.read_text(encoding="utf-8"))
