"use strict";

// TTS 参考音频工作台前端。只用原生 DOM，不引入任何依赖。
// 所有文件名一律走 textContent 赋值，避免素材库里的奇怪名字被当成 HTML。

const state = {
  meta: null,
  styles: [],
  styleName: "",
  rootKey: "",
  relPath: "",
  offset: 0,
  limit: 60,
  total: 0,
  clipPath: "",
  roots: [],
  aux: [],
  advanced: {},
  range: [3, 10],
  forceNext: false,
};

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") node.textContent = v;
    else if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c) node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function fmtDuration(sec) {
  if (sec === null || sec === undefined) return "—";
  return `${Number(sec).toFixed(2)} s`;
}

function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try {
      const body = await resp.json();
      detail = body.detail || JSON.stringify(body);
    } catch (e) {
      detail = (await resp.text()) || detail;
    }
    throw new Error(detail);
  }
  return resp.json();
}

function setHint(target, text, kind = "") {
  const node = typeof target === "string" ? $(target) : target;
  if (!node) return;
  node.textContent = text;
  node.className = `hint ${kind}`;
}

function baseName(path) {
  return String(path || "").split("/").pop() || String(path || "");
}

function streamUrl(path, start, duration) {
  const q = new URLSearchParams({ path });
  if (start) q.set("start", String(start));
  if (duration) q.set("duration", String(duration));
  return `/api/stream?${q.toString()}`;
}

function playPath(path, label, start, duration) {
  const player = $("player");
  player.src = streamUrl(path, start, duration);
  $("player-name").textContent = label || path;
  player.play().catch(() => {
    // 浏览器可能因为格式或自动播放策略拒绝，留给用户手动点。
  });
}

// ---------------- 启动信息 ----------------

async function loadMeta() {
  state.meta = await api("/api/meta");
  state.roots = state.meta.roots || [];
  if (Array.isArray(state.meta.main_ref_range)) {
    state.range = state.meta.main_ref_range;
  }
  $("config-path").textContent = state.meta.plugin_config_path || "";
  $("config-path").title = state.meta.plugin_config_path || "";

  const sel = $("root-select");
  sel.innerHTML = "";
  state.roots.forEach((root) => {
    const opt = document.createElement("option");
    opt.value = root.key;
    opt.textContent = root.exists ? root.label : `${root.label}（不存在）`;
    opt.disabled = !root.exists;
    sel.appendChild(opt);
  });

  const first = state.roots.find((r) => r.exists);
  if (first) {
    sel.value = first.key;
    state.rootKey = first.key;
  }
  $("only-valid-check").title = `只看 ${state.range[0]}~${state.range[1]} 秒的片段`;
}

async function checkHealth() {
  const badge = $("server-status");
  badge.textContent = "检测中…";
  badge.className = "badge";
  try {
    const data = await api("/api/health");
    if (data.online) {
      badge.textContent = `GPT-SoVITS 在线 · ${data.server}`;
      badge.className = "badge ok";
    } else {
      badge.textContent = `GPT-SoVITS 未响应 · ${data.error || ""}`;
      badge.className = "badge bad";
    }
  } catch (e) {
    badge.textContent = `健康检查失败：${e.message}`;
    badge.className = "badge bad";
  }
}

// ---------------- 插件风格 ----------------

async function loadStyles(keepSelection = true) {
  const data = await api("/api/styles");
  state.styles = data.items || [];
  state.advanced = data.advanced || {};

  const sel = $("style-select");
  const previous = keepSelection ? sel.value : "";
  sel.innerHTML = "";
  state.styles.forEach((style) => {
    const opt = document.createElement("option");
    opt.value = style.style_name;
    opt.textContent = style.name ? `${style.style_name}（${style.name}）` : style.style_name;
    sel.appendChild(opt);
  });
  if (previous && state.styles.some((s) => s.style_name === previous)) {
    sel.value = previous;
  }
  applyStyle(sel.value);
}

function currentStyle() {
  return state.styles.find((s) => s.style_name === $("style-select").value) || null;
}

function applyStyle(styleName) {
  const style = state.styles.find((s) => s.style_name === styleName);
  if (!style) return;
  state.aux = (style.aux_refer_wav_paths || []).slice();
  $("main-ref").value = style.refer_wav_path || "";
  $("prompt-text").value = style.prompt_text || "";
  $("prompt-language").value = style.prompt_language || "zh";
  renderMainRefInfo(style.main_ref);
  renderAux(style.aux_refs || []);
  setHint("save-hint", "", "");
}

function renderMainRefInfo(info) {
  const box = $("main-ref-info");
  box.innerHTML = "";
  if (!info || !info.path) {
    box.appendChild(el("span", { class: "warn", text: "未设置主参考音频" }));
    return;
  }
  if (info.missing) {
    box.appendChild(el("span", { class: "bad", text: info.error || "文件不存在" }));
    return;
  }
  const ok = info.main_ref_ok;
  box.appendChild(
    el("span", {
      class: ok ? "ok" : "bad",
      text: `${fmtDuration(info.duration)}${ok ? " 合法" : ` 超出 ${state.range[0]}~${state.range[1]} 秒`}`,
    })
  );
  if (info.samplerate) {
    box.appendChild(el("span", { class: "dim", text: `${info.samplerate} Hz / ${info.channels || 1} ch` }));
  }
}

function renderAux(infos) {
  const list = $("aux-list");
  list.innerHTML = "";
  if (!state.aux.length) {
    list.appendChild(el("li", { class: "dim", text: "没有辅助参考音频（辅助音频不受 3~10 秒限制）" }));
    return;
  }
  state.aux.forEach((path, idx) => {
    const info = infos[idx] || {};
    const li = el("li");
    li.appendChild(el("span", { class: "aux-name", text: baseName(path), title: path }));
    li.appendChild(
      el("span", {
        class: info.missing ? "bad" : "dim",
        text: info.missing ? "文件不存在" : fmtDuration(info.duration),
      })
    );
    const play = el("button", { class: "mini", text: "试听", type: "button" });
    play.addEventListener("click", () => playPath(path, baseName(path)));
    const drop = el("button", { class: "mini danger", text: "移除", type: "button" });
    drop.addEventListener("click", () => {
      state.aux.splice(idx, 1);
      renderAux(infos.filter((_, i) => i !== idx));
      setHint("save-hint", "已改动，记得保存", "warn");
    });
    li.appendChild(play);
    li.appendChild(drop);
    list.appendChild(li);
  });
}

function setMainRef(path) {
  $("main-ref").value = path;
  probeOne(path).then(renderMainRefInfo);
  loadSidecar(path);
  setHint("save-hint", "已改动，记得保存", "warn");
}

function addAux(path) {
  if (state.aux.includes(path)) {
    setHint("save-hint", "该辅助音频已存在", "warn");
    return;
  }
  state.aux.push(path);
  probeMany(state.aux).then(renderAux);
  setHint("save-hint", "已改动，记得保存", "warn");
}

// /api/probe 返回的是 { 路径: 信息 } 的字典，这里按传入顺序还原成数组。
function normalizeProbe(item, path) {
  const info = item || { path, name: baseName(path), error: "未返回结果" };
  if (info.missing === undefined) info.missing = Boolean(info.error) && info.duration === undefined;
  return info;
}

async function probeOne(path) {
  const data = await api(`/api/probe?paths=${encodeURIComponent(path)}`);
  return normalizeProbe((data.items || {})[path], path);
}

async function probeMany(paths) {
  if (!paths.length) return [];
  const data = await api(`/api/probe?paths=${encodeURIComponent(paths.join("\n"))}`);
  const items = data.items || {};
  return paths.map((p) => normalizeProbe(items[p], p));
}

async function loadSidecar(path) {
  const box = $("main-ref-sidecar");
  box.textContent = "";
  try {
    const data = await api(`/api/sidecar?path=${encodeURIComponent(path)}`);
    if (data.text) {
      box.textContent = `发现同名文本：${data.text}`;
      const use = el("button", { class: "mini", text: "填入参考文本", type: "button" });
      use.addEventListener("click", () => {
        $("prompt-text").value = data.text;
        setHint("save-hint", "已改动，记得保存", "warn");
      });
      box.appendChild(use);
    }
  } catch (e) {
    /* 没有旁车文本不是错误 */
  }
}

async function saveStyle(force = false) {
  const style = currentStyle();
  if (!style) return;
  const body = {
    refer_wav_path: $("main-ref").value.trim(),
    aux_refer_wav_paths: state.aux,
    prompt_text: $("prompt-text").value.trim(),
    prompt_language: $("prompt-language").value,
  };
  setHint("save-hint", "保存中…", "");
  try {
    const url = `/api/styles/${encodeURIComponent(style.style_name)}${force ? "?force=true" : ""}`;
    const data = await api(url, { method: "PATCH", body: JSON.stringify(body) });
    const parts = [`已写入 ${(data.changed || []).join("、") || "无改动"}`];
    if (data.backup) parts.push(`备份 ${baseName(data.backup)}`);
    if (data.hint) parts.push(data.hint);
    (data.warnings || []).forEach((w) => parts.push(w));
    setHint("save-hint", parts.join(" · "), data.warnings && data.warnings.length ? "warn" : "ok");
    await loadStyles(true);
  } catch (e) {
    if (!force && /3|10|范围|秒/.test(e.message)) {
      setHint("save-hint", `${e.message}（如确认要写入，再点一次保存即可强制）`, "bad");
      state.forceNext = true;
      return;
    }
    setHint("save-hint", e.message, "bad");
  }
}

// ---------------- 素材库浏览 ----------------

function renderBreadcrumb() {
  const box = $("breadcrumb");
  box.innerHTML = "";
  const root = state.roots.find((r) => r.key === state.rootKey);
  const home = el("button", { class: "crumb", text: root ? root.label : "根目录", type: "button" });
  home.addEventListener("click", () => browse(state.rootKey, ""));
  box.appendChild(home);

  let acc = "";
  (state.relPath ? state.relPath.split("/") : []).filter(Boolean).forEach((part) => {
    acc = acc ? `${acc}/${part}` : part;
    const target = acc;
    box.appendChild(el("span", { class: "sep", text: "/" }));
    const btn = el("button", { class: "crumb", text: part, type: "button" });
    btn.addEventListener("click", () => browse(state.rootKey, target));
    box.appendChild(btn);
  });
}

function renderPager() {
  const from = state.total === 0 ? 0 : state.offset + 1;
  const to = Math.min(state.offset + state.limit, state.total);
  $("page-info").textContent = `${from}-${to} / ${state.total}`;
  $("page-prev").disabled = state.offset <= 0;
  $("page-next").disabled = state.offset + state.limit >= state.total;
}

async function browse(rootKey, relPath, offset = 0) {
  state.rootKey = rootKey || state.rootKey;
  state.relPath = relPath || "";
  state.offset = offset;
  const body = $("file-body");
  setHint("browse-status", "读取中…", "");

  const q = new URLSearchParams({
    root: state.rootKey,
    path: state.relPath,
    offset: String(state.offset),
    limit: String(state.limit),
  });
  const search = $("search-input").value.trim();
  if (search) q.set("search", search);
  if ($("recursive-check").checked) q.set("recursive", "true");
  if ($("only-valid-check").checked) {
    q.set("min_duration", String(state.range[0]));
    q.set("max_duration", String(state.range[1]));
  }

  let data;
  try {
    data = await api(`/api/browse?${q.toString()}`);
  } catch (e) {
    body.innerHTML = "";
    setHint("browse-status", e.message, "bad");
    return;
  }

  state.total = data.total_files || 0;
  body.innerHTML = "";

  if (!$("recursive-check").checked && state.relPath) {
    const up = state.relPath.split("/").slice(0, -1).join("/");
    const tr = el("tr", { class: "dirrow" });
    const cell = el("td", { colspan: "4" });
    const btn = el("button", { class: "linkish", text: "⬆ 上一级", type: "button" });
    btn.addEventListener("click", () => browse(state.rootKey, up));
    cell.appendChild(btn);
    tr.appendChild(cell);
    body.appendChild(tr);
  }

  (data.dirs || []).forEach((d) => {
    const tr = el("tr", { class: "dirrow" });
    const cell = el("td", { colspan: "4" });
    const btn = el("button", { class: "linkish", text: `📁 ${d.name}`, type: "button" });
    btn.addEventListener("click", () => browse(state.rootKey, d.rel_path));
    cell.appendChild(btn);
    tr.appendChild(cell);
    body.appendChild(tr);
  });

  (data.files || []).forEach((info) => body.appendChild(fileRow(info)));

  const root = state.roots.find((r) => r.key === state.rootKey);
  $("root-note").textContent = root && root.note ? root.note : "";
  renderBreadcrumb();
  renderPager();

  if (!(data.files || []).length && !(data.dirs || []).length) {
    setHint("browse-status", "这里没有音频文件", "warn");
  } else {
    setHint("browse-status", `${state.total} 个音频${search ? "（已按关键词过滤）" : ""}`, "");
  }
}

function fileRow(info) {
  const tr = el("tr");

  const nameCell = el("td", { class: "namecell" });
  nameCell.appendChild(el("span", { class: "fname", text: info.name, title: info.path }));
  if (info.needs_transcode) {
    nameCell.appendChild(el("span", { class: "tag", text: info.ext.replace(".", "") }));
  }
  tr.appendChild(nameCell);

  const durCell = el("td");
  if (info.error) {
    durCell.appendChild(el("span", { class: "bad", text: "读取失败", title: info.error }));
  } else {
    durCell.appendChild(
      el("span", {
        class: info.main_ref_ok ? "ok" : "warn",
        text: fmtDuration(info.duration),
        title: info.main_ref_ok ? "可直接作为主参考" : `不在 ${state.range[0]}~${state.range[1]} 秒内`,
      })
    );
  }
  tr.appendChild(durCell);
  tr.appendChild(el("td", { class: "dim", text: fmtSize(info.size) }));

  const actions = el("td", { class: "actions" });
  const listen = el("button", { class: "mini", text: "试听", type: "button" });
  listen.addEventListener("click", () => playPath(info.path, info.name));

  const asMain = el("button", { class: "mini primary", text: "设为主参考", type: "button" });
  asMain.disabled = !info.main_ref_ok;
  if (!info.main_ref_ok) asMain.title = "时长不合法，请先裁剪";
  asMain.addEventListener("click", () => setMainRef(info.path));

  const asAux = el("button", { class: "mini", text: "加为辅助", type: "button" });
  asAux.addEventListener("click", () => addAux(info.path));

  const crop = el("button", { class: "mini", text: "裁剪", type: "button" });
  crop.addEventListener("click", () => loadClip(info));

  actions.append(listen, asMain, asAux, crop);
  tr.appendChild(actions);
  return tr;
}

// ---------------- 裁剪导入 ----------------

function loadClip(info) {
  state.clipPath = info.path;
  $("clip-source").textContent = info.name;
  $("clip-source").title = info.path;
  $("clip-start").value = "0";
  const end = info.duration && info.duration > state.range[1] ? state.range[1] : info.duration || "";
  $("clip-end").value = end === "" ? "" : Number(end).toFixed(2);
  $("clip-name").value = info.name.replace(/\.[^.]+$/, "");
  setHint("clip-hint", `源时长 ${fmtDuration(info.duration)}，裁到 ${state.range[0]}~${state.range[1]} 秒之间`, "");
  loadClipText(info.path);
}

async function loadClipText(path) {
  try {
    const data = await api(`/api/sidecar?path=${encodeURIComponent(path)}`);
    if (data.text) $("clip-text").value = data.text;
  } catch (e) {
    /* 没有旁车文本就算了 */
  }
}

function clipRange() {
  const start = parseFloat($("clip-start").value) || 0;
  const rawEnd = $("clip-end").value.trim();
  const end = rawEnd === "" ? null : parseFloat(rawEnd);
  return { start, end };
}

function previewClip() {
  if (!state.clipPath) {
    setHint("clip-hint", "先在右侧列表点「裁剪」选一个文件", "warn");
    return;
  }
  const { start, end } = clipRange();
  const duration = end !== null && end > start ? end - start : null;
  playPath(state.clipPath, `${baseName(state.clipPath)} 片段`, start, duration);
  if (duration !== null) {
    const ok = duration >= state.range[0] && duration <= state.range[1];
    setHint("clip-hint", `片段 ${duration.toFixed(2)} 秒${ok ? "，可作主参考" : "，超出主参考范围"}`, ok ? "ok" : "warn");
  }
}

async function importClip() {
  if (!state.clipPath) {
    setHint("clip-hint", "先在右侧列表点「裁剪」选一个文件", "warn");
    return;
  }
  const { start, end } = clipRange();
  const body = {
    path: state.clipPath,
    name: $("clip-name").value.trim(),
    start,
    end,
    prompt_text: $("clip-text").value.trim(),
  };
  setHint("clip-hint", "转码中…", "");
  try {
    const data = await api("/api/import", { method: "POST", body: JSON.stringify(body) });
    const bits = [`已生成 ${baseName(data.path)}`, fmtDuration(data.duration)];
    if (data.sidecar) bits.push("已写参考文本");
    bits.push(data.main_ref_ok ? "可作主参考" : "不在主参考范围内");
    setHint("clip-hint", bits.join(" · "), data.main_ref_ok ? "ok" : "warn");
    if (data.main_ref_ok) {
      setMainRef(data.path);
      if (body.prompt_text) $("prompt-text").value = body.prompt_text;
    }
    playPath(data.path, baseName(data.path));
  } catch (e) {
    setHint("clip-hint", e.message, "bad");
  }
}

// ---------------- 试听合成 ----------------

async function synthesize() {
  const style = currentStyle();
  const text = $("tts-text").value.trim();
  if (!text) {
    setHint("tts-hint", "先写一句要合成的话", "warn");
    return;
  }
  const refPath = $("main-ref").value.trim();
  if (!refPath) {
    setHint("tts-hint", "还没有选主参考音频", "warn");
    return;
  }
  const body = {
    text,
    ref_audio_path: refPath,
    prompt_text: $("prompt-text").value.trim(),
    prompt_lang: $("prompt-language").value || "zh",
    // 要合成的文本按中文处理；参考文本的语言由上面的下拉框单独决定。
    text_lang: "zh",
    aux_ref_audio_paths: state.aux.slice(),
    style_name: style ? style.style_name : "",
    switch_weights: $("switch-weights").checked,
  };

  const btn = $("synthesize");
  btn.disabled = true;
  setHint("tts-hint", "合成中，第一次切权重会慢一些…", "");
  try {
    const resp = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      let detail = `${resp.status}`;
      try {
        detail = (await resp.json()).detail || detail;
      } catch (e) {
        detail = (await resp.text()) || detail;
      }
      throw new Error(detail);
    }
    const warn = resp.headers.get("X-Studio-Warnings") || "";
    const blob = await resp.blob();
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = URL.createObjectURL(blob);
    const player = $("player");
    player.src = state.previewUrl;
    $("player-name").textContent = `试听：${text.slice(0, 20)}`;
    player.play().catch(() => {});
    const decoded = warn ? decodeURIComponent(warn) : "";
    setHint("tts-hint", decoded ? `合成完成（${decoded}）` : "合成完成", decoded ? "warn" : "ok");
  } catch (e) {
    setHint("tts-hint", e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

// ---------------- 事件绑定 ----------------

function bind() {
  $("style-select").addEventListener("change", (e) => applyStyle(e.target.value));
  $("reload-style").addEventListener("click", () => loadStyles(true));
  $("save-style").addEventListener("click", () => {
    const force = state.forceNext;
    state.forceNext = false;
    saveStyle(force);
  });
  $("main-ref-play").addEventListener("click", () => {
    const path = $("main-ref").value.trim();
    if (path) playPath(path, baseName(path));
  });
  $("main-ref").addEventListener("change", (e) => {
    const path = e.target.value.trim();
    if (path) setMainRef(path);
  });
  $("prompt-text").addEventListener("input", () => setHint("save-hint", "已改动，记得保存", "warn"));
  $("prompt-language").addEventListener("change", () => setHint("save-hint", "已改动，记得保存", "warn"));

  $("root-select").addEventListener("change", (e) => {
    state.rootKey = e.target.value;
    browse(state.rootKey, "");
  });
  let timer = null;
  $("search-input").addEventListener("input", () => {
    clearTimeout(timer);
    // 换关键词就回到第一页，不然容易停在空页上。
    timer = setTimeout(() => browse(state.rootKey, state.relPath, 0), 300);
  });
  $("recursive-check").addEventListener("change", () => browse(state.rootKey, state.relPath, 0));
  $("only-valid-check").addEventListener("change", () => browse(state.rootKey, state.relPath, 0));
  $("page-prev").addEventListener("click", () => {
    browse(state.rootKey, state.relPath, Math.max(0, state.offset - state.limit));
  });
  $("page-next").addEventListener("click", () => {
    if (state.offset + state.limit < state.total) {
      browse(state.rootKey, state.relPath, state.offset + state.limit);
    }
  });

  $("clip-preview").addEventListener("click", previewClip);
  $("clip-import").addEventListener("click", importClip);
  $("synthesize").addEventListener("click", synthesize);
  $("server-status").addEventListener("click", checkHealth);
}

async function init() {
  bind();
  try {
    await loadMeta();
  } catch (e) {
    setHint("browse-status", `加载配置失败：${e.message}`, "bad");
    return;
  }
  try {
    await loadStyles(false);
  } catch (e) {
    setHint("save-hint", `读取插件配置失败：${e.message}`, "bad");
  }
  if (state.rootKey) await browse(state.rootKey, "");
  checkHealth();
}

document.addEventListener("DOMContentLoaded", init);
