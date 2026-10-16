"""One page: a start button and the danmaku received during this connection."""

from __future__ import annotations

from typing import Any

from fastapi.responses import HTMLResponse, JSONResponse

from src.core.components.base.router import BaseRouter

from .listen import get_feed

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>弹幕</title>
  <style>
    body { font: 16px/1.5 sans-serif; margin: 2rem; color: #1c1c1c; background: #fff; }
    button { font: inherit; padding: 0.4rem 0.9rem; margin-right: 0.5rem; }
    #status { margin: 1rem 0; }
    li { margin: 0.35rem 0; }
    .uid { color: #666; }
  </style>
</head>
<body>
  <h1>房间 31581575</h1>
  <button id="start" type="button">开始</button>
  <button id="stop" type="button">停止</button>
    <p id="status"></p>
  <p>OBS 浏览器源：<code>http://127.0.0.1:18000/live-danmaku/subtitle</code>，1920×1080</p>
  <ol id="list"></ol>
  <script>
    const status = document.querySelector("#status");
    const list = document.querySelector("#list");
    async function post(path) {
      const response = await fetch(path, { method: "POST" });
      return response.json();
    }
    function render(state) {
      if (!state.logged_in) status.textContent = "未登录";
      else if (state.status === "connected") status.textContent = "已连接 " + state.detail;
      else if (state.status === "connecting") status.textContent = "正在连接 " + state.detail;
      else if (state.status === "error") status.textContent = state.detail || "连接失败";
      else status.textContent = state.detail || "未开始";
      list.replaceChildren();
      for (const item of state.messages) {
        const row = document.createElement("li");
        row.textContent = item.at + "  " + item.name + "  ";
        if (item.kind !== "reply") {
          const uid = document.createElement("span");
          uid.className = "uid";
          uid.textContent = "uid " + item.uid;
          row.append(uid, "  ");
        }
        row.append(item.text);
        list.append(row);
      }
    }
    document.querySelector("#start").onclick = async () => render(await post("/live-danmaku/start"));
    document.querySelector("#stop").onclick = async () => render(await post("/live-danmaku/stop"));
    async function poll() {
      const response = await fetch("/live-danmaku/state");
      render(await response.json());
    }
    poll();
    setInterval(poll, 1000);
  </script>
</body>
</html>
"""

_SUBTITLE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>字幕</title>
  <style>
    html, body {
      margin: 0;
      width: 100%;
      height: 100%;
      background: transparent;
      overflow: hidden;
    }
    #line {
      position: absolute;
      left: 8%;
      right: 8%;
      bottom: 9%;
      color: #fff;
      text-align: center;
      font: 700 48px/1.35 "Source Han Sans SC", "Noto Sans CJK SC", sans-serif;
      text-shadow:
        0 2px 2px #000,
        2px 0 0 #000, -2px 0 0 #000, 0 2px 0 #000, 0 -2px 0 #000;
      white-space: pre-wrap;
    }
    .cursor {
      display: inline-block;
      width: 0.08em;
      height: 0.9em;
      margin-left: 0.12em;
      vertical-align: -0.05em;
      background: currentColor;
    }
  </style>
</head>
<body>
  <div id="line"></div>
  <script>
    const line = document.querySelector("#line");
    let shown = 0;
    let timer = 0;
    function typeOut(value) {
      clearTimeout(timer);
      line.replaceChildren();
      const text = document.createTextNode("");
      const cursor = document.createElement("span");
      cursor.className = "cursor";
      line.append(text, cursor);
      const characters = Array.from(value);
      let index = 0;
      const step = () => {
        text.data += characters[index];
        index += 1;
        if (index < characters.length) timer = setTimeout(step, 42);
        else cursor.remove();
      };
      if (characters.length === 0) cursor.remove();
      else step();
    }
    async function poll() {
      const response = await fetch("/live-danmaku/state");
      const state = await response.json();
      if (state.status !== "connected") {
        shown = 0;
        clearTimeout(timer);
        line.replaceChildren();
        return;
      }
      let reply = null;
      for (const item of state.messages) {
        if (item.kind === "reply") reply = item;
      }
      if (!reply || reply.revision === shown) return;
      shown = reply.revision;
      typeOut(reply.text);
    }
    poll();
    setInterval(poll, 500);
  </script>
</body>
</html>
"""


class LiveDanmakuRouter(BaseRouter):
    router_name = "live_danmaku"
    router_description = "手动查看 B 站新弹幕"
    custom_route_path = "/live-danmaku"

    def __init__(self, plugin: Any) -> None:
        self._feed = get_feed()
        super().__init__(plugin)

    def register_endpoints(self) -> None:
        @self.app.get("/", response_class=HTMLResponse)
        async def page() -> HTMLResponse:
            return HTMLResponse(_PAGE)

        @self.app.get("/subtitle", response_class=HTMLResponse)
        async def subtitle() -> HTMLResponse:
            return HTMLResponse(_SUBTITLE)

        @self.app.get("/state")
        async def state() -> JSONResponse:
            return JSONResponse(self._feed.snapshot())

        @self.app.post("/start")
        async def start() -> JSONResponse:
            return JSONResponse(await self._feed.start())

        @self.app.post("/stop")
        async def stop() -> JSONResponse:
            return JSONResponse(await self._feed.stop())
