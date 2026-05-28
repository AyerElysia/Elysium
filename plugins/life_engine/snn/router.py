"""SNN + 调质层可视化 Web 端点 v2。"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from fastapi.responses import HTMLResponse, JSONResponse

from src.app.plugin_system.base import BaseRouter

from ..service.static_page import render_dashboard

if TYPE_CHECKING:
    from ..core.plugin import LifeEnginePlugin

class SNNRouter(BaseRouter):
    """SNN 状态可视化路由。"""

    router_name = "snn"
    router_description = "SNN & Neuromod Dashboard"
    custom_route_path = "/snn"

    def register_endpoints(self) -> None:
        @self.app.get("/", response_class=HTMLResponse)
        async def get_dashboard() -> Any:
            """返回可视化面板 HTML。"""
            return render_dashboard("snn_dashboard.html", "Dashboard")

        @self.app.get("/api/state")
        async def get_state() -> Any:
            """返回完整系统状态。"""
            plugin: "LifeEnginePlugin" = self.plugin  # type: ignore
            service = plugin.service
            net = service._snn_network
            inner = service._inner_state

            result: dict[str, Any] = {"timestamp": time.time()}

            # SNN 状态
            if net:
                result["snn"] = {
                    "status": "active",
                    "tick_count": net.tick_count,
                    "real_step_count": net._real_step_count,
                    "drives": net.get_drive_dict(),
                    "drives_discrete": net.get_drive_discrete(),
                    "hidden_v": net.hidden.v.tolist(),
                    "hidden_spikes": net.hidden.spikes.tolist(),
                    "output_v": net.output.v.tolist(),
                    "output_spikes": net.output.spikes.tolist(),
                    "output_ema": net._output_ema.tolist(),
                    "health": net.get_health(),
                }
            else:
                result["snn"] = {"status": "disabled"}

            # 调质层状态
            if inner:
                result["neuromod"] = inner.get_full_state()
            else:
                result["neuromod"] = {"status": "disabled"}

            # 桥接层
            bridge = service._snn_bridge
            if bridge:
                result["bridge"] = bridge.get_snapshot()

            # 做梦系统
            dream = service._dream_scheduler
            if dream:
                result["dream"] = dream.get_state()
            else:
                result["dream"] = {"status": "disabled"}

            return result

        @self.app.get("/api/weights")
        async def get_weights() -> Any:
            """返回权重矩阵数据。"""
            plugin: "LifeEnginePlugin" = self.plugin  # type: ignore
            net = plugin.service._snn_network
            if not net:
                return JSONResponse(content={"status": "disabled"}, status_code=503)
            return {
                "syn_in_hid_W": net.syn_in_hid.W.tolist(),
                "syn_hid_out_W": net.syn_hid_out.W.tolist(),
                "syn_in_hid_stats": net.syn_in_hid.get_weight_stats(),
                "syn_hid_out_stats": net.syn_hid_out.get_weight_stats(),
            }

        @self.app.get("/api/dream")
        async def get_dream_state() -> Any:
            """返回做梦系统状态。"""
            plugin: "LifeEnginePlugin" = self.plugin  # type: ignore
            dream = plugin.service._dream_scheduler
            if not dream:
                return JSONResponse(content={"status": "disabled"}, status_code=503)
            return {
                "state": dream.get_state(),
                "history": dream.get_dream_history(limit=10),
            }
