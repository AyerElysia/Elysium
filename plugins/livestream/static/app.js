(() => {
  "use strict";
  const PROTOCOL = 1;
  const base = location.pathname.endsWith("/") ? location.pathname : `${location.pathname}/`;
  const stage = document.querySelector("#stage");
  const statusDot = document.querySelector("#status-dot");
  const statusText = document.querySelector("#status-text");
  const subtitle = document.querySelector("#subtitle");
  const events = document.querySelector("#audience-events");
  const expression = document.querySelector("#expression-label");
  const startButton = document.querySelector("#start");
  const stopButton = document.querySelector("#stop");
  const interruptButton = document.querySelector("#interrupt");
  const sayButton = document.querySelector("#say");
  const sayText = document.querySelector("#say-text");
  const stageView = new URLSearchParams(location.search).get("view") === "stage";
  document.body.dataset.view = stageView ? "stage" : "operator";
  const clientKey = `elysium.livestream.client.${stageView ? "stage" : "operator"}`;
  const clientId = localStorage.getItem(clientKey) || crypto.randomUUID();
  localStorage.setItem(clientKey, clientId);

  let socket = null;
  let reconnectTimer = null;
  let pendingOffer = null;
  let currentPlayback = null;
  let audioContext = null;
  const receiptKey = "elysium.livestream.receipts.v1";

  function loadReceipts() {
    try { return JSON.parse(localStorage.getItem(receiptKey) || "{}"); }
    catch (_) { return {}; }
  }
  function rememberReceipt(receipt) {
    const known = loadReceipts();
    known[receipt.playback_id] = receipt;
    const entries = Object.entries(known).slice(-300);
    localStorage.setItem(receiptKey, JSON.stringify(Object.fromEntries(entries)));
  }
  async function ticket() {
    const response = await fetch(`${base}ticket`, { method: "POST", credentials: "same-origin" });
    if (!response.ok) throw new Error(`ticket ${response.status}`);
    return (await response.json()).ticket;
  }
  async function authorizedPost(path, body) {
    const oneTime = await ticket();
    const response = await fetch(`${base}${path}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Authorization": `Bearer ${oneTime}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.message || `request ${response.status}`);
    return data;
  }
  function send(type, payload = {}) {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ version: PROTOCOL, type, payload }));
    }
  }
  function setStatus(text, kind = "ready") {
    statusText.textContent = text;
    statusDot.className = kind;
  }
  async function unlockAudio() {
    audioContext ||= new AudioContext();
    await audioContext.resume();
  }
  async function connect() {
    clearTimeout(reconnectTimer);
    try {
      const oneTime = await ticket();
      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${scheme}//${location.host}${base}ws?ticket=${encodeURIComponent(oneTime)}&client_id=${encodeURIComponent(clientId)}&primary=${stageView ? "1" : "0"}`;
      socket = new WebSocket(url);
      socket.binaryType = "arraybuffer";
      socket.onopen = () => setStatus("舞台已连接");
      socket.onmessage = event => {
        try {
          if (event.data instanceof ArrayBuffer) void handleAudio(event.data);
          else handleMessage(JSON.parse(event.data));
        } catch (error) {
          setStatus(`舞台协议错误：${error.message}`, "error");
          socket.close(1008, "stage protocol error");
        }
      };
      socket.onerror = () => setStatus("舞台连接异常", "error");
      socket.onclose = () => {
        setStatus("舞台已断开，正在重连", "error");
        startButton.disabled = true;
        reconnectTimer = setTimeout(connect, 1500);
      };
    } catch (error) {
      setStatus(`连接失败：${error.message}`, "error");
      reconnectTimer = setTimeout(connect, 2500);
    }
  }
  function handleMessage(message) {
    if (message.version !== PROTOCOL) throw new Error("unsupported stage protocol");
    if (message.type === "ping") return send("pong");
    if (message.type === "stage.ready") {
      setStatus(message.payload.primary ? "主舞台已就绪" : "观察舞台已就绪");
      startButton.disabled = message.payload.runtime_status === "running" || !message.payload.primary_stage_connected;
      stopButton.disabled = message.payload.runtime_status !== "running";
      interruptButton.disabled = stopButton.disabled;
      sayButton.disabled = stopButton.disabled;
      return;
    }
    if (message.type === "audio.offer") {
      const cached = loadReceipts()[message.payload.playback_id];
      pendingOffer = { ...message.payload, cached };
      if (cached) send("playback.receipt", cached);
      return;
    }
    if (message.type === "playback.interrupt") return interruptPlayback(message.payload);
    if (message.type === "audience.event") return showAudienceEvent(message.payload);
  }
  async function sha256(buffer) {
    const digest = await crypto.subtle.digest("SHA-256", buffer);
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  }
  async function handleAudio(buffer) {
    const offer = pendingOffer;
    pendingOffer = null;
    if (!offer || offer.cached) return;
    const startedAt = Date.now() / 1000;
    try {
      if (buffer.byteLength !== offer.size_bytes) throw new Error("audio size mismatch");
      if (await sha256(buffer) !== offer.audio_sha256) throw new Error("audio hash mismatch");
      await unlockAudio();
      const decoded = await audioContext.decodeAudioData(buffer.slice(0));
      const source = audioContext.createBufferSource();
      source.buffer = decoded;
      source.connect(audioContext.destination);
      const playback = { offer, source, startedAt, finished: false, durationMs: Math.round(decoded.duration * 1000) };
      currentPlayback = playback;
      subtitle.textContent = offer.text;
      expression.textContent = [offer.cues?.expression, offer.cues?.motion, offer.cues?.scene].filter(Boolean).join(" · ") || "自然表达";
      stage.dataset.speaking = "true";
      source.onended = () => finishPlayback(playback, "completed", "");
      source.start();
    } catch (error) {
      finishPlayback({ offer, startedAt, finished: false, durationMs: 0 }, "failed", error.message);
    }
  }
  function finishPlayback(playback, outcome, detail) {
    if (playback.finished) return;
    playback.finished = true;
    const endedAt = Date.now() / 1000;
    const receipt = {
      playback_id: playback.offer.playback_id,
      utterance_id: playback.offer.utterance_id,
      chunk_id: playback.offer.chunk_id,
      outcome,
      started_at: playback.startedAt,
      ended_at: endedAt,
      played_ms: Math.max(0, Math.round((endedAt - playback.startedAt) * 1000)),
      duration_ms: playback.durationMs,
      detail: detail || "",
    };
    rememberReceipt(receipt);
    send("playback.receipt", receipt);
    if (currentPlayback === playback) currentPlayback = null;
    stage.dataset.speaking = "false";
    if (outcome !== "completed") subtitle.textContent = "";
  }
  function interruptPlayback(payload) {
    const playback = currentPlayback;
    if (!playback || playback.offer.utterance_id !== payload.utterance_id) return;
    playback.source.stop();
    finishPlayback(playback, "interrupted", payload.reason || "interrupted");
  }
  function showAudienceEvent(payload) {
    const row = document.createElement("div");
    row.className = "audience-event";
    row.textContent = `${payload.user_name || "观众"}：${payload.content || `[${payload.kind}]`}`;
    events.prepend(row);
    while (events.children.length > 6) events.lastElementChild.remove();
    setTimeout(() => row.remove(), 12000);
  }
  async function refreshHealth() {
    try {
      const response = await fetch(`${base}health`, { cache: "no-store" });
      const health = await response.json();
      const running = health.status === "running";
      startButton.disabled = socket?.readyState !== WebSocket.OPEN || running || !health.primary_stage_connected;
      stopButton.disabled = !running;
      interruptButton.disabled = !running;
      sayButton.disabled = !running;
      if (health.degraded_reasons?.length) setStatus("运行降级，请查看日志", "error");
    } catch (_) { /* WebSocket state remains authoritative for the stage. */ }
  }
  startButton.onclick = async () => { try { await unlockAudio(); await authorizedPost("api/start"); await refreshHealth(); } catch (e) { setStatus(e.message, "error"); } };
  stopButton.onclick = async () => { try { await authorizedPost("api/stop"); await refreshHealth(); } catch (e) { setStatus(e.message, "error"); } };
  interruptButton.onclick = async () => { try { await authorizedPost("api/interrupt"); } catch (e) { setStatus(e.message, "error"); } };
  sayButton.onclick = async () => { const text = sayText.value.trim(); if (!text) return; try { await unlockAudio(); await authorizedPost("api/say", { text }); sayText.value = ""; } catch (e) { setStatus(e.message, "error"); } };
  sayText.onkeydown = event => { if (event.key === "Enter") sayButton.click(); };
  void connect();
  setInterval(refreshHealth, 2000);
})();
