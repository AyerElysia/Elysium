"""参考音频工作台的后端。

只监听 127.0.0.1，没有任何鉴权：任何能访问该端口的进程都能读素材、改插件配置、
调 TTS。不要把它暴露到公网或 0.0.0.0。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .audio_io import AudioToolError, import_clip, read_sidecar_text, transcode_to_ogg
from .library import (
    BROWSER_SAFE_EXTS,
    MAIN_REF_MAX_SECONDS,
    MAIN_REF_MIN_SECONDS,
    AudioLibrary,
    DurationCache,
)
from .plugin_config import PluginConfig
from .settings import BACKUP_DIR, CACHE_PATH, StudioSettings

STATIC_DIR = Path(__file__).resolve().parent / "static"

settings = StudioSettings.load()
cache = DurationCache(CACHE_PATH)
library = AudioLibrary(settings.library_roots(), cache)
plugin = PluginConfig(Path(settings.plugin_config_path))

app = FastAPI(title="TTS 参考音频工作台", docs_url=None, redoc_url=None)

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    page = STATIC_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="前端文件缺失")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/api/meta")
async def api_meta() -> dict[str, Any]:
    """启动时给前端的一次性信息：素材库、时长规则、TTS 地址。"""
    return {
        "roots": [r.to_dict() for r in library.roots],
        "main_ref_range": [MAIN_REF_MIN_SECONDS, MAIN_REF_MAX_SECONDS],
        "tts_server": settings.tts_server or plugin.server_url(),
        "ref_output_dir": settings.ref_output_dir,
        "plugin_config_path": str(plugin.path),
    }


@app.get("/api/browse")
async def api_browse(
    root: str,
    path: str = "",
    search: str = "",
    min_duration: float | None = None,
    max_duration: float | None = None,
    offset: int = 0,
    limit: int = 60,
    recursive: bool = False,
) -> dict[str, Any]:
    """浏览某个素材库目录。时长只在需要时探测，避免大目录卡住。"""
    try:
        result = await asyncio.to_thread(
            library.browse,
            root,
            path,
            search=search,
            min_duration=min_duration,
            max_duration=max_duration,
            offset=offset,
            limit=max(1, min(limit, 300)),
            recursive=recursive,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result.to_dict()

def _guard_path(raw: str) -> Path:
    """把前端传来的绝对路径限制在已配置的素材库 / ref 目录内。"""
    if not raw:
        raise HTTPException(status_code=400, detail="缺少路径")
    path = Path(raw)
    if not library.contains(str(path)):
        raise HTTPException(status_code=403, detail="路径不在已配置的素材库内")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return path


@app.get("/api/probe")
async def api_probe(paths: str) -> dict[str, Any]:
    """批量探测时长，前端用来给已选中的参考音频补信息。多个路径用换行分隔。"""
    targets = [Path(p) for p in paths.split("\n") if p.strip()]
    allowed = [p for p in targets if library.contains(str(p))]
    infos = await asyncio.to_thread(library.probe_many, allowed)
    result = {info.path: info.to_dict() for info in infos}
    for p in targets:
        result.setdefault(str(p), {"path": str(p), "name": p.name, "error": "路径不在素材库内"})
    return {"items": result}


@app.get("/api/stream")
async def api_stream(path: str, start: float | None = None, duration: float | None = None) -> Response:
    """试听。浏览器能直接放的原样返回，放不了的（wma 等）转 ogg 后返回。"""
    target = _guard_path(path)
    ext = target.suffix.lower()
    need_cut = start is not None or duration is not None

    if ext in BROWSER_SAFE_EXTS and not need_cut:
        return FileResponse(target, filename=target.name)

    try:
        tmp = await asyncio.to_thread(transcode_to_ogg, target, start, duration)
    except AudioToolError as e:
        raise HTTPException(status_code=500, detail=f"转码失败: {e}") from e
    try:
        data = tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)
    headers = {"Content-Disposition": f"inline; filename*=UTF-8''{quote(target.stem + '.ogg')}"}
    return Response(content=data, media_type="audio/ogg", headers=headers)


@app.get("/api/sidecar")
async def api_sidecar(path: str) -> dict[str, str]:
    """读参考音频旁边的文本，方便一键填 prompt_text。"""
    target = _guard_path(path)
    return {"path": str(target), "text": await asyncio.to_thread(read_sidecar_text, target)}

def _decorate_ref(path: str) -> dict[str, Any]:
    """给风格里引用的音频补上时长与合法性，前端直接显示红/绿。"""
    p = Path(path)
    if not path:
        return {"path": "", "missing": True}
    if not p.is_file():
        return {"path": path, "name": p.name, "missing": True, "error": "文件不存在"}
    try:
        info = library.info_for(p).to_dict()
    except Exception as e:  # 探测失败不该让整页挂掉
        return {"path": path, "name": p.name, "error": str(e)}
    info["missing"] = False
    return info


@app.get("/api/styles")
async def api_styles() -> dict[str, Any]:
    """读取插件里的全部风格，附带每个参考音频的时长与 3~10 秒判定。"""
    try:
        styles = await asyncio.to_thread(plugin.styles)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    items: list[dict[str, Any]] = []
    for style in styles:
        data = style.to_dict()
        data["main_ref"] = await asyncio.to_thread(_decorate_ref, style.refer_wav_path)
        data["aux_refs"] = [await asyncio.to_thread(_decorate_ref, p) for p in style.aux_refer_wav_paths]
        items.append(data)
    return {"items": items, "advanced": plugin.advanced(), "path": str(plugin.path)}


class StylePatchBody(BaseModel):
    """只暴露和参考音频有关的字段，模型路径等不从这里改。"""

    refer_wav_path: str | None = None
    aux_refer_wav_paths: list[str] | None = None
    prompt_text: str | None = None
    prompt_language: str | None = None
    speed_factor: float | None = None

@app.patch("/api/styles/{style_name}")
async def api_patch_style(style_name: str, body: StylePatchBody, force: bool = False) -> dict[str, Any]:
    """把选好的参考音频写回插件 TOML。

    主参考先按 3~10 秒校验：不合规直接拒绝（force=true 可强行写入），
    免得改完配置要等到 bot 真的发语音时才报错。
    """
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="没有要修改的字段")

    warnings: list[str] = []
    main_ref = updates.get("refer_wav_path")
    if main_ref:
        info = await asyncio.to_thread(_decorate_ref, str(main_ref))
        if info.get("missing"):
            raise HTTPException(status_code=400, detail="主参考音频不存在")
        duration = info.get("duration")
        if duration is None:
            warnings.append("主参考音频时长探测失败，未能校验 3~10 秒规则")
        elif not info.get("main_ref_ok"):
            msg = f"主参考音频时长 {duration:.2f}s，不在 {MAIN_REF_MIN_SECONDS}~{MAIN_REF_MAX_SECONDS}s 内，引擎会拒绝"
            if not force:
                raise HTTPException(status_code=400, detail=msg)
            warnings.append(msg)

    for aux in updates.get("aux_refer_wav_paths") or []:
        if not Path(str(aux)).is_file():
            raise HTTPException(status_code=400, detail=f"辅助参考音频不存在: {aux}")

    try:
        result = await asyncio.to_thread(plugin.patch_style, style_name, updates, BACKUP_DIR)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    result["warnings"] = warnings
    result["hint"] = "配置已写入，需要重载插件或重启 bot 才会生效"
    return result

class ImportBody(BaseModel):
    """把素材库里的一段音频裁剪导入 ref 目录。"""

    path: str
    name: str = ""
    start: float = 0.0
    end: float | None = None
    prompt_text: str = ""


@app.post("/api/import")
async def api_import(body: ImportBody) -> dict[str, Any]:
    """裁剪导入。输出单声道 wav，落到 ref 目录，参考文本写同名 .txt。"""
    src = _guard_path(body.path)
    stem = body.name.strip() or src.stem
    try:
        result = await asyncio.to_thread(
            import_clip,
            src,
            Path(settings.ref_output_dir),
            stem,
            body.start,
            body.end,
            settings.import_samplerate,
            body.prompt_text,
        )
    except AudioToolError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    info = await asyncio.to_thread(_decorate_ref, result.path)
    return {
        "path": result.path,
        "duration": result.duration,
        "sidecar": result.sidecar,
        "info": info,
        "main_ref_ok": bool(info.get("main_ref_ok")),
    }

class PreviewBody(BaseModel):
    """试听合成。参数刻意跟插件的 _call_tts_api 对齐，听到的就是 bot 的效果。"""

    text: str = Field(min_length=1, max_length=800)
    ref_audio_path: str
    prompt_text: str = ""
    prompt_lang: str = "zh"
    text_lang: str = "zh"
    aux_ref_audio_paths: list[str] = Field(default_factory=list)
    style_name: str = ""
    switch_weights: bool = True
    speed_factor: float | None = None


async def _switch_weights(session: aiohttp.ClientSession, base: str, style_name: str) -> list[str]:
    """切换到该风格的模型权重。失败只记 warning，因为服务可能已经加载了同一份。"""
    notes: list[str] = []
    try:
        style = await asyncio.to_thread(plugin.find_style, style_name)
    except KeyError:
        return [f"风格 {style_name} 不存在，沿用服务当前权重"]

    for endpoint, weights in (("set_gpt_weights", style.gpt_weights), ("set_sovits_weights", style.sovits_weights)):
        if not weights:
            continue
        try:
            async with session.get(f"{base}/{endpoint}", params={"weights_path": weights}) as resp:
                if resp.status != 200:
                    notes.append(f"{endpoint} 返回 {resp.status}: {(await resp.text())[:200]}")
        except aiohttp.ClientError as e:
            notes.append(f"{endpoint} 调用失败: {e}")
    return notes

@app.post("/api/preview")
async def api_preview(body: PreviewBody) -> Response:
    """用选中的参考音频合成一段试听，直接返回音频流。"""
    main_ref = _guard_path(body.ref_audio_path)
    info = await asyncio.to_thread(_decorate_ref, str(main_ref))
    if info.get("duration") is not None and not info.get("main_ref_ok"):
        raise HTTPException(
            status_code=400,
            detail=f"主参考时长 {info['duration']:.2f}s 不在 {MAIN_REF_MIN_SECONDS}~{MAIN_REF_MAX_SECONDS}s 内，引擎会拒绝",
        )

    aux = [str(_guard_path(p)) for p in body.aux_ref_audio_paths]
    advanced = await asyncio.to_thread(plugin.advanced)
    base = (settings.tts_server or await asyncio.to_thread(plugin.server_url)).rstrip("/")

    payload: dict[str, Any] = {
        **advanced,
        "text": body.text,
        "text_lang": body.text_lang,
        "ref_audio_path": str(main_ref),
        "prompt_text": body.prompt_text,
        "prompt_lang": body.prompt_lang,
        "streaming_mode": False,
    }
    if aux:
        payload["aux_ref_audio_paths"] = aux
    if body.speed_factor is not None:
        payload["speed_factor"] = body.speed_factor

    warnings: list[str] = []
    timeout = aiohttp.ClientTimeout(total=300)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if body.switch_weights and body.style_name:
                warnings = await _switch_weights(session, base, body.style_name)
            async with session.post(f"{base}/tts", json=payload) as resp:
                data = await resp.read()
                if resp.status != 200:
                    raise HTTPException(
                        status_code=502,
                        detail=f"TTS 服务返回 {resp.status}: {data.decode('utf-8', 'replace')[:400]}",
                    )
    except aiohttp.ClientError as e:
        raise HTTPException(status_code=502, detail=f"连不上 TTS 服务 {base}: {e}") from e

    media = str(advanced.get("media_type") or "wav").lower()
    mime = {"ogg": "audio/ogg", "aac": "audio/aac", "raw": "audio/wav"}.get(media, "audio/wav")
    headers = {"X-Studio-Warnings": quote("; ".join(warnings))} if warnings else {}
    return Response(content=data, media_type=mime, headers=headers)

@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    """探一下 GPT-SoVITS 在不在，前端顶栏显示状态。"""
    base = (settings.tts_server or await asyncio.to_thread(plugin.server_url)).rstrip("/")
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base}/tts") as resp:
                # 缺参数时返回 400，说明服务活着；连不上才是真挂了。
                return {"server": base, "online": True, "status": resp.status}
    except aiohttp.ClientError as e:
        return {"server": base, "online": False, "error": str(e)}


class RootBody(BaseModel):
    """新增素材库。"""

    key: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")
    label: str = ""
    path: str
    note: str = ""


@app.post("/api/roots")
async def api_add_root(body: RootBody) -> dict[str, Any]:
    """挂一个新素材库进来（比如以后重新训练时的数据集目录）。"""
    target = Path(body.path).expanduser()
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在: {target}")

    roots = [r for r in settings.roots if r.get("key") != body.key]
    roots.append(
        {
            "key": body.key,
            "label": body.label.strip() or body.key,
            "path": str(target.resolve()),
            "note": body.note.strip(),
        }
    )
    settings.roots = roots
    await asyncio.to_thread(settings.save)
    library.set_roots(settings.library_roots())
    return {"roots": [r.to_dict() for r in library.roots]}


@app.delete("/api/roots/{key}")
async def api_remove_root(key: str) -> dict[str, Any]:
    """摘掉一个素材库。只改本工具的设置，不动磁盘上的文件。"""
    remain = [r for r in settings.roots if r.get("key") != key]
    if len(remain) == len(settings.roots):
        raise HTTPException(status_code=404, detail=f"没有素材库 {key}")
    settings.roots = remain
    await asyncio.to_thread(settings.save)
    library.set_roots(settings.library_roots())
    return {"roots": [r.to_dict() for r in library.roots]}


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn

    print(f"参考音频工作台: http://{settings.host}:{settings.port}")
    print(f"插件配置: {plugin.path}")
    print("提示: 无鉴权，仅监听本机，不要改成 0.0.0.0 或做端口转发")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
