"use strict";

const $ = (id) => document.getElementById(id);
const numberFormat = new Intl.NumberFormat("zh-CN");
const kindLabels = { group: "群组", channel: "频道", unknown: "待识别" };
const sourceLabels = {
  waiting: "待搜索",
  running: "搜索中",
  done: "已完成",
  limited: "受限",
  error: "出错",
  cancelled: "已停止",
};
const membershipLabels = {
  joined: "已加入",
  already_joined: "已加入",
  pending: "等待审核",
  error: "加入失败",
};

const ui = {
  form: $("search-form"),
  query: $("query"),
  submit: $("search-button"),
  feedback: $("search-feedback"),
  progress: $("progressbar"),
  cancel: $("cancel-search"),
  filters: $("filters"),
  sourceWeight: $("source-weight"),
  resultTools: $("result-tools"),
  results: $("result-list"),
  count: $("result-count"),
  empty: $("empty-state"),
  emptyTitle: $("empty-title"),
  emptyDetail: $("empty-detail"),
  sources: $("source-list"),
  variants: $("query-variants"),
  variantStatus: $("variant-status"),
  history: $("history-list"),
  join: $("join-button"),
  joinNotice: $("join-notice"),
  dialog: $("connection-dialog"),
  connection: $("connection-button"),
  connectionLabel: $("connection-label"),
  connectionDot: $("connection-dot"),
  refresh: $("refresh-status"),
  shutdown: $("shutdown-app"),
  setupTelegram: $("setup-telegram"),
  telegramCredentials: $("telegram-credentials"),
  telegramApiId: $("telegram-api-id"),
  telegramApiHash: $("telegram-api-hash"),
  saveTelegram: $("save-telegram"),
  saveTelegramStatus: $("save-telegram-status"),
  setupTelegramHelp: $("setup-telegram-help"),
  setupAI: $("setup-ai"),
  networkSettings: $("network-settings"),
  telegramProxy: $("telegram-proxy"),
  saveNetwork: $("save-network"),
  detectProxy: $("detect-proxy"),
  saveNetworkStatus: $("save-network-status"),
  aiSettings: $("ai-settings"),
  aiBaseUrl: $("ai-base-url"),
  aiModel: $("ai-model"),
  aiKey: $("ai-key"),
  aiLanguages: $("ai-languages"),
  aiLanguageCount: $("ai-language-count"),
  saveAI: $("save-ai"),
  saveAIStatus: $("save-ai-status"),
  brandSettings: $("brand-settings"),
  brandInput: $("brand-input"),
  promoTitleInput: $("promo-title-input"),
  promoTextInput: $("promo-text-input"),
  promoUrlInput: $("promo-url-input"),
  promoChannelInput: $("promo-channel-input"),
  saveBrand: $("save-brand"),
  brandStatus: $("brand-status"),
  brandName: $("brand-name"),
  footerBrand: $("footer-brand"),
  promo: $("promo"),
  promoTitle: $("promo-title"),
  promoText: $("promo-text"),
  promoLink: $("promo-link"),
  promoClose: $("promo-close"),
  footerPromo: $("footer-promo"),
  footerChannel: $("footer-channel"),
  botList: $("bot-list"),
  botForm: $("bot-form"),
  botInput: $("bot-input"),
  botStatus: $("bot-status"),
  officialSearch: $("official-search"),
  enrichToggle: $("enrich-toggle"),
  searchOptions: $("search-options"),
  botVariants: $("bot-variants"),
  maxVariants: $("max-variants"),
  searchOptionsStatus: $("search-options-status"),
  messagesDialog: $("messages-dialog"),
  messagesTitle: $("messages-title"),
  messagesStatus: $("messages-status"),
  messagesQueries: $("messages-queries"),
  messagesList: $("messages-list"),
};

let connectionStatus = null;
let job = null;
let isStarting = false;
let isCancelling = false;
let isJoining = false;
let currentFilter = "all";
let sourcePreference = readPreference();
let pollTimer = 0;
let pollFailures = 0;
let lastRender = "";
let messageRequest = 0;
const selectedIds = new Set();
const membership = new Map();

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function request(path, init = {}) {
  const response = await fetch(path, { ...init, cache: "no-store" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : "本地服务请求失败。");
    error.status = response.status;
    throw error;
  }
  return data;
}

function post(path, body) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

function searchPath(id, suffix = "") {
  return `/api/search/${encodeURIComponent(id)}${suffix}`;
}

function sourceName(name) {
  if (name === "telegram") return "Telegram 官方搜索";
  if (name.toLowerCase() === "@jisou") return "极搜 @jisou";
  return name;
}

function isBusy() {
  return isStarting || job?.state === "running" || isJoining;
}

const AVATAR_COLORS = ["#0e6b4d", "#2b6cb0", "#8a5a17", "#8b3a62", "#3f6b8a", "#6b4f9a", "#a35a2b", "#2f7d6b"];

function avatarLetter(title) {
  const text = String(title || "").trim().replace(/^[@#\s]+/, "");
  const match = text.match(/[\p{L}\p{N}]/u);
  return (match ? match[0] : "·").toUpperCase();
}

function avatarColor(id) {
  let hash = 0;
  for (const char of String(id)) hash = (hash * 31 + char.codePointAt(0)) | 0;
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

function readPreference() {
  try {
    return localStorage.getItem("sourcePreference");
  } catch {
    return null;
  }
}

function preferQuery() {
  return sourcePreference ? `?prefer=${encodeURIComponent(sourcePreference)}` : "";
}

function renderSourceWeight() {
  const options = [{ value: "", label: "均衡" }];
  for (const bot of connectionStatus?.bots ?? []) {
    options.push({ value: bot, label: `偏向 ${sourceName(bot)}` });
  }
  options.push({ value: "telegram", label: "偏向 Telegram 官方" });
  if (sourcePreference === null) {
    // Default to the first configured search bot: its results carry the group names.
    sourcePreference = connectionStatus?.bots?.[0] ?? "";
  } else if (!options.some((option) => option.value === sourcePreference)) {
    sourcePreference = "";
  }
  ui.sourceWeight.replaceChildren(...options.map((option) => {
    const node = element("option", "", option.label);
    node.value = option.value;
    return node;
  }));
  ui.sourceWeight.value = sourcePreference;
}

function safeTelegramUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.host !== "t.me" || url.username || url.password) return null;
    const isInvite = /^\/(?:\+|joinchat\/)[A-Za-z0-9_-]+\/?$/.test(url.pathname);
    const isPublicChat = /^\/[A-Za-z0-9_]{3,32}\/?$/.test(url.pathname);
    if ((!isInvite && !isPublicChat) || url.search || url.hash) return null;
    return url.href;
  } catch {
    return null;
  }
}

function safeMessageUrl(value) {
  try {
    const url = new URL(value);
    const path = /^\/(?:c\/\d+|[A-Za-z0-9_]{3,32})\/(?:\d+\/)?\d+\/?$/;
    if (url.origin === "https://t.me" && !url.username && !url.password && !url.search && !url.hash && path.test(url.pathname)) {
      return url.href;
    }
  } catch {
    // Invite-only targets may not expose a permanent link for the matched message.
  }
  return null;
}

function messageTimestamp(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" });
}

function buildMessage(message) {
  const row = element("li", "message-row");
  row.append(element("p", "message-text", message.text || "（无可显示的正文）"));
  const timestamp = messageTimestamp(message.date);
  const url = safeMessageUrl(message.url);
  if (timestamp || url) {
    const footer = element("div", "message-footer");
    if (timestamp) footer.append(element("time", "", timestamp));
    if (url) {
      const link = element("a", "", "查看原帖 ↗");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      footer.append(link);
    }
    row.append(footer);
  }
  return row;
}

async function openMessages(result) {
  if (!job || job.state === "running" || isJoining || !safeTelegramUrl(result.url)) return;
  const requestId = ++messageRequest;
  ui.messagesTitle.textContent = `群内搜索 · ${result.title || result.username || "Telegram"}`;
  ui.messagesStatus.textContent = "正在查找与本次关键词匹配的消息。";
  ui.messagesQueries.hidden = true;
  ui.messagesList.replaceChildren();
  if (!ui.messagesDialog.open) ui.messagesDialog.showModal();

  try {
    const data = await post(searchPath(job.id, "/messages"), { id: result.id });
    if (requestId !== messageRequest || !ui.messagesDialog.open) return;
    const matches = data.results ?? [];
    const feedback = {
      done: matches.length ? `找到 ${matches.length} 条匹配消息。` : "没有找到匹配的消息。",
      requires_join: "加入后才能搜索此处的消息。",
      limited: "暂时无法搜索此处的消息。",
      error: "读取消息时出错。",
    };
    ui.messagesStatus.textContent = data.message || feedback[data.state] || "没有收到查询状态。";
    const usedQueries = data.queries ?? [];
    ui.messagesQueries.hidden = !usedQueries.length;
    ui.messagesQueries.textContent = usedQueries.length ? `检索用词：${usedQueries.join(" · ")}` : "";
    ui.messagesList.replaceChildren(...matches.map(buildMessage));
  } catch (error) {
    if (requestId === messageRequest && ui.messagesDialog.open) ui.messagesStatus.textContent = error.message;
  }
}

function renderConnection() {
  const connected = connectionStatus?.telegram?.connected;
  ui.connectionDot.className = connected === true ? "signal connected" : connected === false ? "signal disconnected" : "signal";
  ui.connectionLabel.textContent = connected === true ? "Telegram 已连接" : connected === false ? "Telegram 未连接" : "正在检查连接";
  ui.setupTelegram.textContent = connectionStatus?.telegram?.message || (connected ? "本地账号可用。" : "正在检查本地 Telegram 会话。");
  ui.telegramCredentials.hidden = connected === true || connectionStatus?.telegram?.credentials_saved === true;
  ui.setupTelegramHelp.hidden = connected === true;
  const ai = connectionStatus?.ai;
  ui.setupAI.textContent = ai?.configured ? `已配置 ${ai.model || "在线 AI"}。` : ai ? "尚未配置在线 AI，将使用基础扩词。" : "正在检查关键词扩展配置。";
  // Do not overwrite what the operator is currently typing.
  if (document.activeElement !== ui.telegramProxy) ui.telegramProxy.value = connectionStatus?.proxy || "";
  if (document.activeElement !== ui.aiBaseUrl) ui.aiBaseUrl.value = ai?.base_url || "";
  if (document.activeElement !== ui.aiModel) ui.aiModel.value = ai?.model || "";
  if (document.activeElement !== ui.aiLanguages && !ui.aiLanguages.value) {
    ui.aiLanguages.value = (connectionStatus?.search?.languages ?? []).join(", ");
  }
  if (document.activeElement !== ui.aiLanguageCount) {
    ui.aiLanguageCount.value = connectionStatus?.search?.language_count ?? 5;
  }
  renderBrand();
  renderSources();
  renderSearchOptions();
}

function renderBrand() {
  const brand = connectionStatus?.brand || "极目";
  if (ui.brandName.textContent !== brand) ui.brandName.textContent = brand;
  ui.footerBrand.textContent = brand;
  if (!document.title.startsWith(brand)) document.title = `${brand} · Telegram 多语言群组检索`;

  const promo = connectionStatus?.promo;
  const dismissed = readDismissedPromo();
  const show = Boolean(promo?.url && promo?.title) && dismissed !== promo.url;
  ui.promo.hidden = !show;
  if (show) {
    ui.promoTitle.textContent = promo.title;
    ui.promoText.textContent = promo.text || "";
    ui.promoText.hidden = !promo.text;
    ui.promoLink.href = promo.url;
  }
  ui.footerPromo.hidden = !promo?.url;
  if (promo?.url) {
    ui.footerPromo.href = promo.url;
    ui.footerPromo.textContent = "加入交流群 ↗";
  }
  ui.footerChannel.hidden = !promo?.channel;
  if (promo?.channel) {
    ui.footerChannel.href = promo.channel;
  }
  if (document.activeElement !== ui.brandInput) ui.brandInput.value = brand;
  if (document.activeElement !== ui.promoTitleInput) ui.promoTitleInput.value = promo?.title || "";
  if (document.activeElement !== ui.promoTextInput) ui.promoTextInput.value = promo?.text || "";
  if (document.activeElement !== ui.promoUrlInput) ui.promoUrlInput.value = promo?.url || "";
  if (document.activeElement !== ui.promoChannelInput) ui.promoChannelInput.value = promo?.channel || "";
}

function readDismissedPromo() {
  try {
    return localStorage.getItem("promoDismissed");
  } catch {
    return null;
  }
}

function renderSources() {
  const bots = connectionStatus?.bots ?? [];
  const maxBots = connectionStatus?.search?.max_bots ?? 5;
  if (!bots.length) {
    ui.botList.replaceChildren(element("li", "bot-empty", "还没有添加搜索机器人，只用官方搜索。"));
  } else {
    ui.botList.replaceChildren(...bots.map((bot) => {
      const row = element("li", "bot-row");
      const remove = element("button", "", "×");
      remove.type = "button";
      remove.dataset.bot = bot;
      remove.setAttribute("aria-label", `移除 ${bot}`);
      remove.disabled = isBusy();
      row.append(element("span", "bot-handle", sourceName(bot)), remove);
      return row;
    }));
  }
  ui.botInput.disabled = bots.length >= maxBots;
  ui.botInput.placeholder = bots.length >= maxBots ? `最多 ${maxBots} 个` : "@另一个搜索机器人";
}

function renderSearchOptions() {
  const search = connectionStatus?.search;
  if (!search) return;
  if (document.activeElement !== ui.officialSearch) ui.officialSearch.checked = search.official !== false;
  if (document.activeElement !== ui.enrichToggle) ui.enrichToggle.checked = search.enrich !== false;
  if (document.activeElement !== ui.botVariants) ui.botVariants.value = search.bot_variants ?? 4;
  if (document.activeElement !== ui.maxVariants) ui.maxVariants.value = search.max_variants ?? 14;
}

async function refreshConnection() {
  ui.refresh.disabled = true;
  try {
    connectionStatus = await request("/api/status");
  } catch (error) {
    connectionStatus = { telegram: { connected: false, message: error.message }, ai: null, bots: [] };
  } finally {
    ui.refresh.disabled = false;
    renderConnection();
    renderSourceWeight();
    renderDetails();
  }
}

function renderDetails() {
  const sourceEntries = Object.entries(job?.sources ?? {});
  if (!sourceEntries.length) {
    for (const name of ["telegram", ...(connectionStatus?.bots ?? [])]) {
      sourceEntries.push([name, { state: "waiting", message: "" }]);
    }
  }
  ui.sources.replaceChildren(...sourceEntries.map(([name, info]) => {
    const status = Object.hasOwn(sourceLabels, info.state) ? info.state : "waiting";
    const row = element("li", "source-row");
    const dot = element("span", `source-signal ${status}`);
    dot.setAttribute("aria-hidden", "true");
    row.append(dot, element("span", "source-name", sourceName(name)), element("span", `source-state ${status}`, sourceLabels[status]));
    if (info.message) row.append(element("span", "source-message", info.message));
    return row;
  }));

  const variants = job?.variants ?? [];
  ui.variants.replaceChildren(...variants.map((variant) => element("li", "", variant)));
  ui.variantStatus.textContent = job?.expansion_message ||
    (job?.used_ai === true ? "由在线 AI 扩展。" : variants.length ? "已按基础规则扩展。" : job?.state === "running" ? "正在扩展关键词。" : "开始搜索后显示。");
}

async function loadHistory() {
  try {
    const data = await request("/api/history");
    const entries = data.searches ?? [];
    if (!entries.length) {
      ui.history.replaceChildren(element("li", "assistive-note", "搜索完成后记录在这里。"));
      return;
    }
    ui.history.replaceChildren(...entries.map((entry) => {
      const item = element("li", "");
      const button = element("button", "");
      button.type = "button";
      button.dataset.searchId = entry.id;
      button.setAttribute("aria-label", `重新打开搜索 ${entry.query}`);
      button.append(
        element("span", "history-query", entry.query),
        element("span", "history-meta", `${entry.created_at} · ${entry.count} 个候选`),
      );
      item.append(button);
      return item;
    }));
  } catch {
    ui.history.replaceChildren();
  }
}

async function restoreSearch(searchId) {
  if (isJoining) return;
  clearTimeout(pollTimer);
  try {
    const restored = await post(`/api/history/${encodeURIComponent(searchId)}/restore`);
    job = await request(searchPath(restored.id) + preferQuery());
    currentFilter = "all";
    selectedIds.clear();
    membership.clear();
    pollFailures = 0;
    lastRender = "";
    ui.feedback.textContent = "";
    ui.joinNotice.hidden = true;
    syncFilters();
    renderJob();
  } catch (error) {
    ui.feedback.textContent = error.message;
  }
}

function getJoinStatus(result) {
  return membership.get(result.id) ?? {
    state: result.joined ? "already_joined" : result.join_state,
    message: result.join_message,
  };
}

function selectable(result) {
  const state = getJoinStatus(result).state;
  return Boolean(safeTelegramUrl(result.url)) && (!state || state === "error");
}

function updateJoinButton() {
  const count = selectedIds.size;
  ui.join.disabled = !count || job?.state === "running" || isJoining;
  ui.join.textContent = isJoining ? "正在加入…" : `加入所选${count ? `（${count}）` : ""}`;
  ui.join.title = count && job?.state === "running" ? "等本次搜索结束后加入所选。" : "";
}

function buildResult(result) {
  const row = element("li", "result-row");
  const title = result.title || result.username || "未命名群组或频道";
  const check = element("input", "result-select");
  check.type = "checkbox";
  check.disabled = !selectable(result) || isJoining;
  check.checked = selectedIds.has(result.id);
  check.dataset.focusKey = `select-${result.id}`;
  check.setAttribute("aria-label", `选择 ${title}`);
  check.addEventListener("change", () => {
    if (check.checked) selectedIds.add(result.id);
    else selectedIds.delete(result.id);
    updateJoinButton();
  });

  const body = element("div", "result-main");
  const heading = element("div", "result-heading");
  const kind = Object.hasOwn(kindLabels, result.kind) ? result.kind : "unknown";
  heading.append(element("h3", "result-title", title), element("span", `result-kind ${kind}`, kindLabels[kind]));
  if (result.username) heading.append(element("span", "result-handle", `@${String(result.username).replace(/^@/, "")}`));
  if (Number.isFinite(result.members) && result.members >= 0) {
    heading.append(element("span", "result-members", `${numberFormat.format(result.members)} 位成员`));
  }
  body.append(heading);
  if (result.description) {
    const description = element("p", "result-description", result.description);
    description.title = result.description;
    body.append(description);
  }
  const evidence = element("p", "result-evidence");
  for (const source of result.sources ?? []) evidence.append(element("span", "result-source", sourceName(source)));
  const matches = result.matched_queries ?? [];
  if (matches.length) {
    const matched = element("span", "result-matches", `命中 ${matches.slice(0, 3).join(" · ")}${matches.length > 3 ? ` 等 ${matches.length} 词` : ""}`);
    if (matches.length > 3) matched.title = matches.join(" · ");
    evidence.append(matched);
  }
  if (evidence.hasChildNodes()) body.append(evidence);

  const { state, message } = getJoinStatus(result);
  if (Object.hasOwn(membershipLabels, state)) body.append(element("p", `result-status ${state}`, message || membershipLabels[state]));
  const avatar = element("span", "result-avatar", avatarLetter(title));
  avatar.style.background = `linear-gradient(135deg, ${avatarColor(result.id)}, ${avatarColor(result.id)}cc)`;
  avatar.setAttribute("aria-hidden", "true");
  row.append(check, avatar, body);
  const url = safeTelegramUrl(result.url);
  if (url) {
    const actions = element("div", "result-actions");
    const link = element("a", "result-open", "打开 ↗");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.dataset.focusKey = `open-${result.id}`;
    link.setAttribute("aria-label", `在 Telegram 打开 ${title}`);
    const messages = element("button", "result-open", "群内搜索");
    messages.type = "button";
    messages.disabled = job?.state === "running" || isJoining;
    messages.dataset.focusKey = `messages-${result.id}`;
    messages.setAttribute("aria-label", `搜索 ${title} 的群内消息`);
    messages.setAttribute("aria-haspopup", "dialog");
    messages.addEventListener("click", () => openMessages(result));
    actions.append(link, messages);
    row.append(actions);
  }
  return row;
}

function emptyMessage(visible, results) {
  if (visible.length) return;
  if (results.length) {
    ui.emptyTitle.textContent = "当前筛选没有结果";
    ui.emptyDetail.textContent = "选择「全部」查看本次搜索的其他候选。";
    return;
  }
  const messages = {
    running: ["正在搜索", "先找到的群组和频道会陆续显示。"],
    done: ["没有找到群组或频道", "可以试试更常用的简称或更宽泛的主题。"],
    cancelled: ["搜索已停止", "可以修改关键词后重新搜索。"],
    error: ["搜索未完成", "请查看来源状态，或稍后重试。"],
  };
  const [title, detail] = messages[job?.state] ?? ["输入主题，开始搜索", "结果将显示群组和频道的名称、来源与链接。"];
  ui.emptyTitle.textContent = title;
  ui.emptyDetail.textContent = detail;
}

function renderResults() {
  const results = job?.results ?? [];
  const allowedIds = new Set(results.filter(selectable).map((row) => row.id));
  for (const id of selectedIds) {
    if (!allowedIds.has(id)) selectedIds.delete(id);
  }
  const visible = currentFilter === "all" ? results : results.filter((row) => row.kind === currentFilter);
  const count = visible.length === results.length ? `${results.length} 个候选` : `${visible.length} / ${results.length} 个候选`;
  const progress = { running: " · 搜索中", cancelled: " · 已停止", error: " · 未完成" }[job?.state] ?? "";
  const countText = job ? count + progress : "";
  if (ui.count.textContent !== countText) ui.count.textContent = countText;
  ui.resultTools.hidden = !results.length;
  const searching = job?.state === "running" && !results.length;
  ui.progress.hidden = job?.state !== "running";
  ui.empty.hidden = Boolean(visible.length) || searching;
  updateJoinButton();
  if (searching) {
    if (!ui.results.querySelector(".skeleton-row")) {
      ui.results.replaceChildren(...Array.from({ length: 4 }, () => element("li", "skeleton-row")));
    }
    lastRender = "";
    return;
  }
  emptyMessage(visible, results);

  // Status polling should not disturb a selection or a keyboard user's focus.
  const fingerprint = JSON.stringify([visible, [...membership], isJoining, job?.state]);
  if (fingerprint === lastRender) return;
  lastRender = fingerprint;
  const focused = ui.results.contains(document.activeElement) ? document.activeElement.dataset.focusKey : undefined;
  ui.results.replaceChildren(...visible.map(buildResult));
  if (focused) {
    const next = [...ui.results.querySelectorAll("[data-focus-key]")].find((node) => node.dataset.focusKey === focused && !node.disabled);
    next?.focus({ preventScroll: true });
  }
}

function renderJob() {
  const running = job?.state === "running";
  ui.submit.disabled = isStarting || running || isJoining;
  ui.cancel.hidden = !running;
  ui.cancel.disabled = isCancelling;
  ui.cancel.textContent = isCancelling ? "正在停止…" : "停止搜索";
  renderDetails();
  renderResults();
}

async function refreshSnapshot() {
  if (!job) return;
  try {
    const snapshot = await request(searchPath(job.id) + preferQuery());
    if (!job || snapshot.id !== job.id) return;
    job = snapshot;
    renderJob();
  } catch (error) {
    ui.feedback.textContent = error.message;
  }
}

function schedulePoll(id, delay) {
  clearTimeout(pollTimer);
  if (job?.id === id && job.state === "running") {
    pollTimer = window.setTimeout(() => poll(id), delay);
  }
}

async function poll(id) {
  if (job?.id !== id || job.state !== "running") return;
  try {
    const snapshot = await request(searchPath(id) + preferQuery());
    if (job?.id !== id || job.state !== "running") return;
    job = snapshot;
    pollFailures = 0;
    ui.feedback.textContent = job.error || "";
    renderJob();
    if (job.state !== "running") loadHistory();
    schedulePoll(id, 700);
  } catch (error) {
    if (job?.id !== id || job.state !== "running") return;
    pollFailures += 1;
    if (error.status === 404 || pollFailures >= 5) {
      job.state = "error";
      ui.feedback.textContent = error.message;
      renderJob();
    } else {
      ui.feedback.textContent = "暂时无法取得进度，正在重试。";
      schedulePoll(id, 800 * pollFailures);
    }
  }
}

async function submitSearch(event) {
  event.preventDefault();
  const query = ui.query.value.trim();
  if (!query || isStarting || job?.state === "running" || isJoining) return;
  clearTimeout(pollTimer);
  isStarting = true;
  ui.feedback.textContent = "";
  renderJob();
  try {
    if (connectionStatus?.telegram?.connected !== true) {
      await refreshConnection();
      if (!connectionStatus?.telegram?.connected) {
        if (!ui.dialog.open) ui.dialog.showModal();
        return;
      }
    }
    const response = await post("/api/search", { query });
    job = { id: response.id, query, state: "running", results: [], variants: [], sources: {} };
    currentFilter = "all";
    selectedIds.clear();
    membership.clear();
    pollFailures = 0;
    ui.joinNotice.hidden = true;
    syncFilters();
    schedulePoll(job.id, 0);
  } catch (error) {
    ui.feedback.textContent = error.message;
    if (error.status === 503) await refreshConnection();
  } finally {
    isStarting = false;
    renderJob();
  }
}

async function cancelSearch() {
  if (job?.state !== "running" || isCancelling) return;
  const id = job.id;
  isCancelling = true;
  renderJob();
  try {
    await post(searchPath(id, "/cancel"));
    schedulePoll(id, 0);
  } catch (error) {
    ui.feedback.textContent = error.message;
  } finally {
    isCancelling = false;
    renderJob();
  }
}

async function joinSelected() {
  if (!job || job.state === "running" || isJoining || !selectedIds.size) return;
  const ids = job.results.filter((result) => selectedIds.has(result.id) && selectable(result)).map((result) => result.id);
  if (!ids.length) return;

  isJoining = true;
  ui.joinNotice.hidden = true;
  renderJob();
  try {
    const data = await post(searchPath(job.id, "/join"), { ids });
    const counts = { joined: 0, already_joined: 0, pending: 0, error: 0 };
    const submitted = new Set(ids);
    for (const item of data.results ?? []) {
      if (!submitted.has(item.id) || !Object.hasOwn(counts, item.state)) continue;
      membership.set(item.id, item);
      counts[item.state] += 1;
      if (item.state !== "error") selectedIds.delete(item.id);
    }
    const summary = [
      counts.joined && `已加入 ${counts.joined} 个`,
      counts.already_joined && `已是成员 ${counts.already_joined} 个`,
      counts.pending && `待审核 ${counts.pending} 个`,
      counts.error && `失败 ${counts.error} 个`,
    ].filter(Boolean).join("，");
    ui.joinNotice.textContent = summary || "未收到加入结果。";
    ui.joinNotice.classList.toggle("is-error", Boolean(counts.error) || !summary);
  } catch (error) {
    ui.joinNotice.textContent = error.message;
    ui.joinNotice.classList.add("is-error");
  } finally {
    isJoining = false;
    ui.joinNotice.hidden = false;
    renderJob();
  }
}

function syncFilters() {
  for (const button of ui.filters.querySelectorAll("button[data-kind]")) {
    button.setAttribute("aria-pressed", String(button.dataset.kind === currentFilter));
  }
}

async function saveTelegramCredentials(event) {
  event.preventDefault();
  ui.saveTelegram.disabled = true;
  ui.saveTelegramStatus.hidden = false;
  ui.saveTelegramStatus.textContent = "正在保存到本机…";
  try {
    await post("/api/settings/telegram", {
      api_id: ui.telegramApiId.value.trim(),
      api_hash: ui.telegramApiHash.value.trim(),
    });
    ui.telegramCredentials.reset();
    ui.saveTelegramStatus.textContent = "已保存。现在运行 telegram-login.cmd 完成一次登录。";
    await refreshConnection();
  } catch (error) {
    ui.saveTelegramStatus.textContent = error.message;
  } finally {
    ui.saveTelegram.disabled = false;
  }
}

async function saveNetworkSettings(event) {
  event.preventDefault();
  ui.saveNetwork.disabled = true;
  ui.saveNetworkStatus.hidden = false;
  ui.saveNetworkStatus.textContent = "正在保存…";
  try {
    await post("/api/settings", { telegram_proxy: ui.telegramProxy.value.trim() });
    ui.saveNetworkStatus.textContent = "已保存。正在重新连接 Telegram…";
    await refreshConnection();
    ui.saveNetworkStatus.textContent = "已保存。";
  } catch (error) {
    ui.saveNetworkStatus.textContent = error.message;
  } finally {
    ui.saveNetwork.disabled = false;
  }
}

async function saveAISettings(event) {
  event.preventDefault();
  ui.saveAI.disabled = true;
  ui.saveAIStatus.hidden = false;
  ui.saveAIStatus.textContent = "正在保存…";
  try {
    await post("/api/settings", {
      ai_base_url: ui.aiBaseUrl.value.trim(),
      ai_model: ui.aiModel.value.trim(),
      ai_key: ui.aiKey.value.trim(),
      ai_languages: ui.aiLanguages.value.trim(),
      ai_language_count: Number(ui.aiLanguageCount.value) || 5,
    });
    ui.aiKey.value = "";
    ui.saveAIStatus.textContent = "已保存，下一次搜索生效。";
    await refreshConnection();
  } catch (error) {
    ui.saveAIStatus.textContent = error.message;
  } finally {
    ui.saveAI.disabled = false;
  }
}

async function saveBrandSettings(event) {
  event.preventDefault();
  ui.saveBrand.disabled = true;
  ui.brandStatus.hidden = false;
  ui.brandStatus.textContent = "正在保存…";
  try {
    await post("/api/settings", {
      brand: ui.brandInput.value.trim(),
      promo_title: ui.promoTitleInput.value.trim(),
      promo_text: ui.promoTextInput.value.trim(),
      promo_url: ui.promoUrlInput.value.trim(),
      promo_channel: ui.promoChannelInput.value.trim(),
    });
    ui.brandStatus.textContent = "已保存。";
    await refreshConnection();
  } catch (error) {
    ui.brandStatus.textContent = error.message;
  } finally {
    ui.saveBrand.disabled = false;
  }
}

async function saveBots(bots) {
  ui.botStatus.hidden = false;
  ui.botStatus.textContent = "正在保存…";
  try {
    await post("/api/settings", { search_bots: bots });
    await refreshConnection();
    ui.botStatus.textContent = "已保存，下一次搜索生效。";
  } catch (error) {
    ui.botStatus.textContent = error.message;
  }
}

async function addBot(event) {
  event.preventDefault();
  const value = ui.botInput.value.trim();
  if (!value) return;
  const bots = [...(connectionStatus?.bots ?? [])];
  const name = value.startsWith("@") ? value : `@${value}`;
  if (bots.some((bot) => bot.toLowerCase() === name.toLowerCase())) {
    ui.botStatus.hidden = false;
    ui.botStatus.textContent = "这个机器人已经在列表里了。";
    return;
  }
  ui.botInput.value = "";
  await saveBots([...bots, name]);
}

async function saveSearchOptions(event) {
  event.preventDefault();
  ui.searchOptionsStatus.hidden = false;
  ui.searchOptionsStatus.textContent = "正在保存…";
  try {
    await post("/api/settings", {
      official_search: ui.officialSearch.checked,
      enrich: ui.enrichToggle.checked,
      bot_variants: Number(ui.botVariants.value) || 4,
      max_variants: Number(ui.maxVariants.value) || 14,
    });
    await refreshConnection();
    ui.searchOptionsStatus.textContent = "已保存，下一次搜索生效。";
  } catch (error) {
    ui.searchOptionsStatus.textContent = error.message;
  }
}

ui.form.addEventListener("submit", submitSearch);
ui.cancel.addEventListener("click", cancelSearch);
ui.join.addEventListener("click", joinSelected);
ui.filters.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-kind]");
  if (!button) return;
  currentFilter = button.dataset.kind;
  syncFilters();
  renderResults();
});
ui.connection.addEventListener("click", () => {
  if (!ui.dialog.open) ui.dialog.showModal();
});
ui.history.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-search-id]");
  if (button) restoreSearch(button.dataset.searchId);
});
ui.sourceWeight.addEventListener("change", () => {
  sourcePreference = ui.sourceWeight.value;
  try {
    localStorage.setItem("sourcePreference", sourcePreference);
  } catch {
    // Private mode may refuse storage; the choice still applies to this page.
  }
  lastRender = "";
  refreshSnapshot();
});
ui.refresh.addEventListener("click", refreshConnection);
ui.detectProxy.addEventListener("click", async () => {
  ui.detectProxy.disabled = true;
  ui.saveNetworkStatus.hidden = false;
  ui.saveNetworkStatus.textContent = "正在检测本机代理端口，约 5 秒…";
  try {
    const data = await request("/api/proxy/detect");
    if (data.suggest) {
      ui.telegramProxy.value = data.suggest;
      ui.saveNetworkStatus.textContent = `检测到 ${data.found.join("、")}，正在保存…`;
      await post("/api/settings", { telegram_proxy: data.suggest });
      await refreshConnection();
      ui.saveNetworkStatus.textContent = `已使用 ${data.suggest}`;
    } else if (data.direct) {
      ui.saveNetworkStatus.textContent = "可以直连 Telegram，不需要代理。";
    } else {
      ui.saveNetworkStatus.textContent = "没有检测到可用代理，请手动填写端口。";
    }
  } catch (error) {
    ui.saveNetworkStatus.textContent = error.message;
  } finally {
    ui.detectProxy.disabled = false;
  }
});
ui.shutdown.addEventListener("click", async () => {
  if (!window.confirm("退出后搜索服务会停止，网页将无法继续使用。确定退出？")) return;
  ui.shutdown.disabled = true;
  try {
    await post("/api/shutdown");
    document.body.innerHTML = '<main class="frame" style="padding:80px 0;text-align:center">' +
      '<h1 style="font-family:var(--serif);font-size:24px">极目已退出</h1>' +
      '<p style="color:var(--muted);margin-top:10px">可以关闭这个页面了。下次从桌面或开始菜单的「极目」重新打开。</p></main>';
  } catch {
    ui.shutdown.disabled = false;
    ui.feedback.textContent = "退出失败，请用任务管理器结束 pythonw.exe。";
  }
});
ui.telegramCredentials.addEventListener("submit", saveTelegramCredentials);
ui.networkSettings.addEventListener("submit", saveNetworkSettings);
ui.aiSettings.addEventListener("submit", saveAISettings);
ui.brandSettings.addEventListener("submit", saveBrandSettings);
ui.promoClose.addEventListener("click", () => {
  const url = connectionStatus?.promo?.url;
  if (!url) return;
  try {
    localStorage.setItem("promoDismissed", url);
  } catch {
    // Storage may be unavailable; just hide it for this visit.
  }
  ui.promo.hidden = true;
});
ui.searchOptions.addEventListener("submit", saveSearchOptions);
ui.botForm.addEventListener("submit", addBot);
ui.officialSearch.addEventListener("change", () => ui.searchOptions.requestSubmit());
ui.enrichToggle.addEventListener("change", () => ui.searchOptions.requestSubmit());
ui.botList.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-bot]");
  if (!button || button.disabled) return;
  const bots = (connectionStatus?.bots ?? []).filter((bot) => bot !== button.dataset.bot);
  saveBots(bots);
});

renderConnection();
renderJob();
refreshConnection();
loadHistory();
if (location.hash === "#setup") ui.dialog.showModal();
