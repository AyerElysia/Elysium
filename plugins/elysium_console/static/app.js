"use strict";

const viewTitles = {
  overview: "概览",
  timeline: "生命时间线",
  subject: "主体文档",
  memory: "记忆经历",
  world: "世界状态",
  attention: "持续关注",
  workspace: "文件空间",
  minecraft: "Minecraft 陪玩",
  catalog: "数据地图",
};

const state = {
  currentView: "overview",
  loaded: new Set(),
  timeline: [],
  subjects: [],
  selectedSubject: 0,
  memoryItems: [],
  memoryCursor: null,
  memoryFrontier: null,
  worldItems: [],
  worldCursor: null,
  attentionItems: [],
  attentionContinuation: "",
  workspacePath: "",
  workspaceText: null,
  worldValue: null,
  busyCount: 0,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function node(tag, className = "", text = "") {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== "") item.textContent = String(text);
  return item;
}

function empty(container, text = "当前没有可展示的数据") {
  container.replaceChildren(node("div", "empty-state", text));
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes)) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = Math.max(0, bytes);
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 10 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

function formatNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat("zh-CN").format(number) : "—";
}

function formatTime(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function compactId(value, length = 12) {
  const text = String(value || "");
  return text.length > length ? `${text.slice(0, length)}…` : text || "—";
}

function statusOf(value) {
  if (!value || typeof value !== "object") return "unknown";
  return String(value.status || value.state || value.phase || "unknown");
}

function setBusy(active) {
  state.busyCount = Math.max(0, state.busyCount + (active ? 1 : -1));
  $("#loading").classList.toggle("is-active", state.busyCount > 0);
}

function showError(message) {
  const banner = $("#error-banner");
  banner.textContent = String(message || "读取失败");
  banner.classList.remove("is-hidden");
  window.setTimeout(() => banner.classList.add("is-hidden"), 7000);
  const connection = $("#connection-state");
  connection.classList.add("is-error");
  connection.querySelector("span:last-child").textContent = "读取异常";
}

function markConnected() {
  const connection = $("#connection-state");
  connection.classList.remove("is-error");
  connection.querySelector("span:last-child").textContent = "本机只读连接";
}

async function api(path, options = {}) {
  setBusy(true);
  try {
    const method = options.method || "GET";
    const headers = { Accept: "application/json" };
    if (method !== "GET") {
      headers["X-Elysium-Console-Action"] = "minecraft-control-v1";
    }
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers,
    });
    if (!response.ok) {
      let detail = `读取失败（${response.status}）`;
      try {
        const payload = await response.json();
        if (payload.detail) detail = payload.detail;
      } catch (_error) {
        // The status code remains the content-neutral fallback.
      }
      if (response.status === 401) {
        window.location.reload();
      }
      throw new Error(detail);
    }
    markConnected();
    return await response.json();
  } finally {
    setBusy(false);
  }
}

function statCard(label, value, note = "") {
  const card = node("article", "stat-card");
  card.append(node("span", "", label), node("strong", "", value));
  if (note) card.append(node("small", "", note));
  return card;
}

function healthCard(label, value) {
  const item = node("div", "health-item");
  const status = statusOf(value);
  item.append(node("strong", "", label));
  const statusNode = node("div", "health-status", status);
  if (["failed", "error", "unavailable"].includes(status.toLowerCase())) {
    statusNode.classList.add("failed");
  }
  item.append(statusNode);
  const backend = value && typeof value === "object" ? value.backend : "";
  if (backend) item.append(node("small", "", `backend · ${backend}`));
  return item;
}

async function loadOverview() {
  const data = await api("api/v1/overview");
  const workspace = data.workspace || {};
  const memory = data.memory || {};
  const eventLedger = data.event_ledger || {};
  $("#overview-stats").replaceChildren(
    statCard("Life Engine", data.life_engine_status, "当前服务健康状态"),
    statCard("意识实例", formatNumber((data.presence || []).length), "当前与历史在场投影"),
    statCard("写作文件", formatNumber(workspace.files), formatBytes(workspace.bytes)),
    statCard("记忆后端", memory.backend || statusOf(memory), "权威数据保持完整"),
  );

  const presence = $("#presence-list");
  const instances = data.presence || [];
  if (!instances.length) {
    empty(presence, "当前没有意识实例投影");
  } else {
    presence.replaceChildren(...instances.slice(0, 12).map((item) => {
      const row = node("div", "stack-item");
      const body = node("div");
      body.append(
        node("strong", "", item.display_name || item.kind || "意识实例"),
        node("small", "", `${item.status || "unknown"} · ${compactId(item.instance_id, 18)}`),
      );
      row.append(body, node("span", "tag", item.kind || "instance"));
      return row;
    }));
  }

  const health = data.health || {};
  const healthItems = [
    ["事件账本", eventLedger],
    ["记忆系统", memory],
    ["世界状态", health.world_projection || {}],
    ["主体文档", health.subject_document || {}],
    ["持续关注", health.attention_threads || {}],
    ["存储运行时", health.storage_runtime || {}],
  ];
  $("#health-list").replaceChildren(...healthItems.map(([label, value]) => healthCard(label, value)));
}

function renderTimeline() {
  const channel = $("#timeline-channel").value;
  const query = $("#timeline-search").value.trim().toLowerCase();
  const items = state.timeline.filter((item) => {
    const channelMatch = !channel || item.channel === channel;
    const haystack = `${item.event_type} ${item.source} ${item.content?.content || ""}`.toLowerCase();
    return channelMatch && (!query || haystack.includes(query));
  });
  const list = $("#timeline-list");
  if (!items.length) {
    empty(list, "当前筛选下没有生命事件");
    return;
  }
  list.replaceChildren(...items.slice().reverse().map((item) => {
    const card = node("article", "timeline-item");
    const header = node("div", "item-header");
    header.append(
      node("div", "item-title", item.event_type || "event"),
      node("div", "item-meta", `${formatTime(item.timestamp)} · #${item.sequence}`),
    );
    const metadata = node("div", "item-meta");
    metadata.append(
      node("span", "tag", item.channel || "unknown"),
      node("span", "", item.source || "source unknown"),
      node("span", "", compactId(item.source_instance_id, 16)),
    );
    card.append(header, metadata, node("p", "item-content", item.content?.content || "（无正文）"));
    if (item.content && !item.content.complete) {
      card.append(node("div", "projection-note", `当前投影 ${formatBytes(item.content.delivered_bytes)} / 原文 ${formatBytes(item.content.original_bytes)} · ${compactId(item.content.content_sha256, 16)}`));
    }
    return card;
  }));
}

async function loadTimeline() {
  const data = await api("api/v1/timeline?limit=160");
  state.timeline = data.items || [];
  const channels = [...new Set(state.timeline.map((item) => item.channel).filter(Boolean))].sort();
  const select = $("#timeline-channel");
  const current = select.value;
  select.replaceChildren(node("option", "", "全部频道"));
  select.firstChild.value = "";
  for (const channel of channels) {
    const option = node("option", "", channel);
    option.value = channel;
    select.append(option);
  }
  select.value = current;
  renderTimeline();
}

function renderSubject() {
  const tabs = $("#subject-tabs");
  tabs.replaceChildren(...state.subjects.map((document, index) => {
    const button = node("button", "tab-button", document.path);
    button.type = "button";
    button.setAttribute("role", "tab");
    button.classList.toggle("is-active", index === state.selectedSubject);
    button.addEventListener("click", () => {
      state.selectedSubject = index;
      renderSubject();
    });
    return button;
  }));
  const document = state.subjects[state.selectedSubject];
  if (!document) {
    $("#subject-meta").replaceChildren();
    $("#subject-content").textContent = "当前没有主体文档";
    return;
  }
  $("#subject-meta").replaceChildren(
    node("span", "", document.path),
    node("span", "", formatBytes(document.bytes)),
    node("span", "", `sha256 ${compactId(document.sha256, 22)}`),
    node("span", "", "完整权威快照"),
  );
  $("#subject-content").textContent = document.content || "";
}

async function loadSubject() {
  const data = await api("api/v1/subject");
  state.subjects = data.items || [];
  state.selectedSubject = Math.min(state.selectedSubject, Math.max(0, state.subjects.length - 1));
  renderSubject();
}

function renderMemory() {
  const list = $("#memory-list");
  if (!state.memoryItems.length) {
    empty(list, "当前没有经历记录");
  } else {
    list.replaceChildren(...state.memoryItems.slice().reverse().map((ref) => {
      const experience = ref.experience || {};
      const projection = experience.content || {};
      const card = node("article", "data-card");
      card.append(
        node("h3", "", `${experience.event_type || "experience"} · ${formatTime(experience.occurred_at)}`),
        node("p", "", projection.content || "（无正文）"),
      );
      const footer = node("footer");
      footer.append(
        node("span", "", `position ${ref.ingest_position}`),
        node("span", "", experience.channel || "unknown"),
        node("span", "", compactId(ref.occurrence_id, 20)),
        node("span", "", ref.is_alias ? "alias" : "canonical"),
      );
      if (!projection.complete) footer.append(node("span", "", `投影 ${formatBytes(projection.delivered_bytes)} / ${formatBytes(projection.original_bytes)}`));
      card.append(footer);
      return card;
    }));
  }
  $("#memory-more").disabled = !state.memoryCursor;
}

async function loadMemory(reset = true) {
  if (reset) {
    const summary = await api("api/v1/memory");
    const health = summary.health || {};
    const legacy = summary.statistics?.legacy || {};
    $("#memory-stats").replaceChildren(
      statCard("健康状态", statusOf(health), health.backend || "memory authority"),
      statCard("旧图节点", formatNumber(legacy.nodes ?? legacy.node_count), "只读兼容投影，不代表全部记忆"),
      statCard("旧图关系", formatNumber(legacy.edges ?? legacy.edge_count), "权威经历由不可变账本承载"),
    );
    state.memoryItems = [];
    state.memoryCursor = null;
    state.memoryFrontier = null;
  }
  const query = new URLSearchParams({ limit: "40" });
  if (!reset && state.memoryCursor) {
    query.set("after_position", String(state.memoryCursor.ingest_position));
    query.set("after_occurrence_id", state.memoryCursor.occurrence_id);
    if (state.memoryFrontier) {
      query.set("through_position", String(state.memoryFrontier.ingest_position));
      query.set("through_occurrence_id", state.memoryFrontier.occurrence_id);
    }
  }
  const data = await api(`api/v1/memory/experiences?${query}`);
  if (reset) state.memoryFrontier = data.frontier;
  state.memoryItems.push(...(data.items || []));
  state.memoryCursor = data.has_more ? data.next_cursor : null;
  renderMemory();
}

function valuePreview(item) {
  if (!item.value_inlined) return "大值：按引用读取";
  try {
    const value = typeof item.value === "string" ? item.value : JSON.stringify(item.value, null, 2);
    return value.length > 180 ? `${value.slice(0, 180)}…` : value;
  } catch (_error) {
    return "值已内联";
  }
}

function renderWorld() {
  const table = $("#world-table");
  if (!state.worldItems.length) {
    const row = node("tr");
    const cell = node("td", "", "当前没有世界断言");
    cell.colSpan = 5;
    row.append(cell);
    table.replaceChildren(row);
  } else {
    table.replaceChildren(...state.worldItems.map((item) => {
      const row = node("tr");
      row.append(
        node("td", "", formatTime(item.observed_at)),
        node("td", "", item.domain || "—"),
        node("td", "", `${item.subject || "—"}\n${item.predicate || "—"}`),
        node("td", "", item.status || "—"),
      );
      const valueCell = node("td");
      valueCell.append(node("div", "value-preview", valuePreview(item)));
      const button = node("button", "value-button", item.value_inlined ? "查看原值" : `读取 ${formatBytes(item.value_bytes)}`);
      button.type = "button";
      button.addEventListener("click", () => openWorldValue(item));
      valueCell.append(button);
      row.append(valueCell);
      return row;
    }));
  }
  $("#world-more").disabled = !state.worldCursor;
}

async function loadWorld(reset = true) {
  if (reset) {
    state.worldItems = [];
    state.worldCursor = null;
  }
  const query = new URLSearchParams({
    limit: "50",
    include_retracted: String($("#world-retracted").checked),
  });
  if (!reset && state.worldCursor) {
    query.set("after_observed_at", state.worldCursor.observedAt);
    query.set("after_assertion_id", state.worldCursor.assertionId);
  }
  const data = await api(`api/v1/world?${query}`);
  state.worldItems.push(...(data.items || []));
  state.worldCursor = data.has_more ? {
    observedAt: data.next_after_observed_at,
    assertionId: data.next_after_assertion_id,
  } : null;
  renderWorld();
}

async function loadWorldValue(append = false) {
  if (!state.worldValue) return;
  const offset = append ? state.worldValue.nextOffset : 0;
  const data = await api(`api/v1/world/assertions/${encodeURIComponent(state.worldValue.id)}/value?offset_bytes=${offset}`);
  const chunk = data.chunk || {};
  if (append) {
    $("#value-content").textContent += chunk.content || "";
  } else {
    $("#value-content").textContent = chunk.content || "";
  }
  state.worldValue.nextOffset = chunk.next_offset_bytes || 0;
  state.worldValue.complete = Boolean(chunk.complete);
  $("#value-meta").replaceChildren(
    node("span", "", compactId(state.worldValue.id, 32)),
    node("span", "", `${formatBytes(state.worldValue.nextOffset)} / ${formatBytes(chunk.total_bytes)}`),
    node("span", "", `sha256 ${compactId(chunk.full_sha256, 22)}`),
  );
  $("#value-more").classList.toggle("is-hidden", state.worldValue.complete);
}

async function openWorldValue(item) {
  state.worldValue = { id: item.assertion_id, nextOffset: 0, complete: false };
  $("#value-content").textContent = "";
  $("#value-dialog").showModal();
  try {
    await loadWorldValue(false);
  } catch (error) {
    showError(error.message);
  }
}

function renderAttention() {
  const list = $("#attention-list");
  if (!state.attentionItems.length) {
    empty(list, "当前没有主体持续关注线索");
  } else {
    list.replaceChildren(...state.attentionItems.map((item) => {
      const card = node("article", "data-card");
      card.append(
        node("h3", "", item.statement_excerpt || "（公开表述为空）"),
        node("p", "", item.excerpt_complete ? "完整公开表述" : "当前为有界摘录；权威事件保持完整"),
      );
      const footer = node("footer");
      footer.append(
        node("span", "tag", item.status || "unknown"),
        node("span", "", `revision ${item.revision}`),
        node("span", "", `event ${item.last_event_position}`),
        node("span", "", compactId(item.thread_id, 22)),
      );
      card.append(footer);
      return card;
    }));
  }
  $("#attention-more").disabled = !state.attentionContinuation;
}

async function loadAttention(reset = true) {
  if (reset) {
    state.attentionItems = [];
    state.attentionContinuation = "";
  }
  const query = new URLSearchParams({ limit: "32" });
  const status = $("#attention-status").value;
  if (status) query.append("statuses", status);
  if (!reset && state.attentionContinuation) query.set("continuation", state.attentionContinuation);
  const data = await api(`api/v1/attention?${query}`);
  const page = data.page || {};
  state.attentionItems.push(...(page.items || []));
  state.attentionContinuation = page.continuation || "";
  renderAttention();
}

function renderBreadcrumb() {
  const container = $("#workspace-breadcrumb");
  const parts = state.workspacePath ? state.workspacePath.split("/") : [];
  const nodes = [];
  const root = node("button", "crumb", "根目录");
  root.type = "button";
  root.addEventListener("click", () => loadWorkspace(""));
  nodes.push(root);
  let current = "";
  for (const part of parts) {
    nodes.push(node("span", "", "/"));
    current = current ? `${current}/${part}` : part;
    const path = current;
    const button = node("button", "crumb", part);
    button.type = "button";
    button.addEventListener("click", () => loadWorkspace(path));
    nodes.push(button);
  }
  container.replaceChildren(...nodes);
}

function renderWorkspace(data) {
  state.workspacePath = data.path || "";
  renderBreadcrumb();
  $("#workspace-count").textContent = `${formatNumber(data.total_visible_items)} items`;
  const list = $("#workspace-list");
  const items = data.items || [];
  if (!items.length) {
    empty(list, "此目录为空");
    return;
  }
  list.replaceChildren(...items.map((item) => {
    const button = node("button", "file-row");
    button.type = "button";
    button.append(
      node("span", "file-kind", item.kind === "directory" ? "DIR" : "FILE"),
      node("span", "", item.name),
      node("span", "file-size", item.kind === "file" ? formatBytes(item.bytes) : ""),
    );
    if (item.kind === "directory") {
      button.addEventListener("click", () => loadWorkspace(item.path));
    } else if (item.text_readable) {
      button.addEventListener("click", () => loadWorkspaceText(item.path, false));
    } else {
      button.disabled = true;
      button.title = item.media_kind ? "媒体文件仅展示元数据" : "当前只读取严格 UTF-8 文本";
    }
    return button;
  }));
}

async function loadWorkspace(path = state.workspacePath) {
  const data = await api(`api/v1/workspace?path=${encodeURIComponent(path)}&limit=500`);
  renderWorkspace(data);
}

async function loadWorkspaceText(path, append) {
  const previous = append ? state.workspaceText : null;
  const offset = previous ? previous.nextOffset : 0;
  const data = await api(`api/v1/workspace/text?path=${encodeURIComponent(path)}&offset_bytes=${offset}`);
  if (append && previous && previous.path === path) {
    $("#workspace-reader").textContent += data.content || "";
  } else {
    $("#workspace-reader").textContent = data.content || "";
  }
  state.workspaceText = {
    path,
    nextOffset: data.next_offset_bytes,
    totalBytes: data.total_bytes,
    complete: Boolean(data.complete),
    sha256: data.sha256,
  };
  $("#workspace-reader-title").textContent = path;
  $("#workspace-reader-meta").textContent = `${formatBytes(data.next_offset_bytes)} / ${formatBytes(data.total_bytes)}`;
  $("#workspace-more-text").classList.toggle("is-hidden", Boolean(data.complete));
}

async function loadCatalog() {
  const data = await api("api/v1/catalog");
  const grid = $("#catalog-grid");
  const domains = data.domains || [];
  if (!domains.length) {
    empty(grid, "当前没有数据域信息");
    return;
  }
  grid.replaceChildren(...domains.map((item) => {
    const card = node("article", "catalog-card");
    const detail = node("details");
    detail.append(node("summary", "", "查看完整健康元数据"));
    const metadata = node("pre", "document-content");
    metadata.textContent = JSON.stringify(item.health || {}, null, 2);
    detail.append(metadata);
    card.append(
      node("div", "domain-key", item.domain),
      node("h3", "", item.title),
      node("p", "", item.authority),
      node("footer", "", `health · ${statusOf(item.health)} · view ${item.view}`),
      detail,
    );
    return card;
  }));
}

function minecraftDetail(label, value) {
  const row = node("div", "stack-item");
  row.append(node("strong", "", label), node("span", "tag", value || "—"));
  return row;
}

function renderMinecraft(data) {
  const active = Boolean(data.active);
  const consciousness = data.consciousness || {};
  const observation = data.latest_observation || {};
  const tasks = observation.bot_tasks?.high_level || {};
  const activeTask = tasks.active || null;
  const readiness = data.readiness || (data.available ? "idle" : "disabled");
  $("#minecraft-state-tag").textContent = active ? "正在世界中" : readiness;
  $("#minecraft-stats").replaceChildren(
    statCard("陪玩身体", active ? (data.body_name || "在线") : "未加入", active ? "独立玩家身体" : "等待你启动"),
    statCard("游戏连接", data.bridge_connected ? "已连接" : "未连接", data.game_instance_id ? compactId(data.game_instance_id, 18) : "bridge"),
    statCard("专属意识", consciousness.running ? "在场" : "未运行", consciousness.phase || "not started"),
    statCard("游戏事件", formatNumber(data.body_event_count), "持久化后才确认"),
  );
  $("#minecraft-detail").replaceChildren(
    minecraftDetail("就绪状态", readiness),
    minecraftDetail("当前任务", activeTask ? `${activeTask.kind} · ${activeTask.phase}` : "无"),
    minecraftDetail("会话", compactId(data.session_id, 22)),
    minecraftDetail("最近错误", data.last_error || "无"),
  );
  $("#minecraft-start").disabled = active || !data.available;
  $("#minecraft-stop").disabled = !active;
}

async function loadMinecraft() {
  const data = await api("api/v1/minecraft");
  renderMinecraft(data);
}

async function runMinecraftPreflight() {
  const data = await api("api/v1/minecraft/preflight");
  $("#minecraft-result").textContent = JSON.stringify(data.result || data, null, 2);
  await loadMinecraft();
}

async function runMinecraftStart() {
  const goal = $("#minecraft-goal").value.trim();
  const query = new URLSearchParams({ goal });
  const data = await api(`api/v1/minecraft/start?${query}`, { method: "POST" });
  $("#minecraft-result").textContent = JSON.stringify(data.result || data, null, 2);
  await loadMinecraft();
}

async function runMinecraftStop() {
  const data = await api("api/v1/minecraft/stop", { method: "POST" });
  $("#minecraft-result").textContent = JSON.stringify(data.result || data, null, 2);
  await loadMinecraft();
}

const loaders = {
  overview: loadOverview,
  timeline: loadTimeline,
  subject: loadSubject,
  memory: () => loadMemory(true),
  world: () => loadWorld(true),
  attention: () => loadAttention(true),
  workspace: () => loadWorkspace(""),
  minecraft: loadMinecraft,
  catalog: loadCatalog,
};

async function activateView(view, force = false) {
  if (!viewTitles[view]) return;
  state.currentView = view;
  $$(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === view));
  $$(".view").forEach((item) => item.classList.toggle("is-active", item.id === `view-${view}`));
  $("#page-title").textContent = viewTitles[view];
  if (window.location.hash.slice(1) !== view) {
    window.history.replaceState(null, "", `#${view}`);
  }
  if (!force && state.loaded.has(view)) return;
  try {
    await loaders[view]();
    state.loaded.add(view);
  } catch (error) {
    showError(error.message);
  }
}

function bindEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => activateView(button.dataset.view)));
  $("#refresh-button").addEventListener("click", () => activateView(state.currentView, true));
  $("#timeline-channel").addEventListener("change", renderTimeline);
  $("#timeline-search").addEventListener("input", renderTimeline);
  $("#memory-more").addEventListener("click", async () => {
    try { await loadMemory(false); } catch (error) { showError(error.message); }
  });
  $("#world-more").addEventListener("click", async () => {
    try { await loadWorld(false); } catch (error) { showError(error.message); }
  });
  $("#world-retracted").addEventListener("change", async () => {
    try { await loadWorld(true); } catch (error) { showError(error.message); }
  });
  $("#attention-more").addEventListener("click", async () => {
    try { await loadAttention(false); } catch (error) { showError(error.message); }
  });
  $("#attention-status").addEventListener("change", async () => {
    try { await loadAttention(true); } catch (error) { showError(error.message); }
  });
  $("#workspace-more-text").addEventListener("click", async () => {
    if (!state.workspaceText) return;
    try { await loadWorkspaceText(state.workspaceText.path, true); } catch (error) { showError(error.message); }
  });
  $("#minecraft-preflight").addEventListener("click", async () => {
    try { await runMinecraftPreflight(); } catch (error) { showError(error.message); }
  });
  $("#minecraft-start").addEventListener("click", async () => {
    try { await runMinecraftStart(); } catch (error) { showError(error.message); }
  });
  $("#minecraft-stop").addEventListener("click", async () => {
    try { await runMinecraftStop(); } catch (error) { showError(error.message); }
  });
  $("#dialog-close").addEventListener("click", () => $("#value-dialog").close());
  $("#value-more").addEventListener("click", async () => {
    try { await loadWorldValue(true); } catch (error) { showError(error.message); }
  });
  window.addEventListener("hashchange", () => activateView(window.location.hash.slice(1) || "overview"));
}

bindEvents();
activateView(window.location.hash.slice(1) || "overview");
window.setInterval(() => {
  if (state.currentView === "minecraft") {
    loadMinecraft().catch((error) => showError(error.message));
  }
}, 3000);
