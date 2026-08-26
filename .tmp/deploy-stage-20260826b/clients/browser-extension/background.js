/*
 * WebAgent Browser Connector — MV3 background service worker.
 *
 * Holds ONE authenticated WebSocket back to the WebAgent server and runs the
 * commands it pushes against the user's real tabs. This is the client half of
 * the connector backend; the server half is app/api/connector_ws.py +
 * app/tools/browser_connector.py.
 *
 * Wire protocol (see the server module docstrings):
 *   server -> us : { type:"cmd", id, bs_id, action, params }
 *   server -> us : { type:"control", action:"shutdown"|"settings", settings? }
 *   us -> server : { type:"result", id, success, result, url, title, error, mime_type? }
 *   us -> server : { type:"hello", version }   on connect
 *   us -> server : { type:"ping" }             keepalive
 *
 * MV3 NOTE: the service worker is killed after ~30s idle. We keep it warm with a
 * chrome.alarms heartbeat and treat the socket dropping as the NORMAL case —
 * reconnect with exponential backoff. This is the central reliability concern.
 */

"use strict";

const VERSION = "0.2.0";

const DEFAULTS = {
  serverUrl: "ws://127.0.0.1:8080",
  token: "",
  autoConnect: true,
  allowScreenshots: true,
  requireEvalApproval: true,
  paused: false,
};

let cfg = Object.assign({}, DEFAULTS);
let ws = null;
let state = "off";          // "off" | "connecting" | "connected"
let backoff = 1000;         // reconnect backoff, ms
let inflight = 0;           // commands currently running
const bsTabs = {};          // bs_id -> chrome tabId
let installationId = "";

// ── config ──────────────────────────────────────────────────────────────────
async function loadCfg() {
  const s = await chrome.storage.local.get(["cfg", "installationId"]);
  cfg = Object.assign({}, DEFAULTS, s.cfg || {});
  installationId = s.installationId || crypto.randomUUID();
  if (!s.installationId) await chrome.storage.local.set({ installationId });
}
async function saveCfg() {
  await chrome.storage.local.set({ cfg });
}

// ── connection ──────────────────────────────────────────────────────────────
function wsUrl() {
  let base = (cfg.serverUrl || "").trim().replace(/\/+$/, "");
  if (base.startsWith("http://")) base = "ws://" + base.slice(7);
  else if (base.startsWith("https://")) base = "wss://" + base.slice(8);
  let u = base + "/api/v1/connector/ws?v=" + encodeURIComponent(VERSION);
  if (cfg.token) u += "&token=" + encodeURIComponent(cfg.token);
  return u;
}

async function helloPayload() {
  let platform = {};
  try { platform = await chrome.runtime.getPlatformInfo(); } catch (e) { /* ignore */ }
  const ua = (self.navigator && self.navigator.userAgent) || "";
  const match = ua.match(/(?:Chrom(?:e|ium)|Edg|OPR)\/([\d.]+)/);
  return {
    type: "hello",
    version: VERSION,
    installation_id: installationId,
    browser: ua.includes("Edg/") ? "Microsoft Edge" : ua.includes("OPR/") ? "Opera" : "Chromium",
    browser_version: match ? match[1] : "",
    platform: platform.os || "",
    mobile: false,
    capabilities: {
      navigate: true, click: true, type: true, read: true,
      screenshot: true, evaluate: true,
    },
    settings: {
      paused: !!cfg.paused,
      auto_connect: !!cfg.autoConnect,
      allow_screenshots: !!cfg.allowScreenshots,
      require_eval_approval: !!cfg.requireEvalApproval,
    },
  };
}

async function announce() {
  if (ws && ws.readyState === 1) send(await helloPayload());
}

function connect() {
  if (state === "connected" || state === "connecting") return;
  if (!cfg.serverUrl) return;
  // The server no longer has a tokenless/open access mode. Do not hammer the
  // authenticated endpoint with guaranteed-to-fail handshakes before pairing.
  if (!cfg.token) {
    state = "off";
    updateBadge();
    return;
  }
  state = "connecting";
  updateBadge();
  let sock;
  try {
    sock = new WebSocket(wsUrl());
  } catch (e) {
    state = "off";
    scheduleReconnect();
    return;
  }
  ws = sock;
  sock.onopen = async () => {
    state = "connected";
    backoff = 1000;
    updateBadge();
    if (ws === sock) send(await helloPayload());
  };
  sock.onmessage = (ev) => onMessage(ev.data);
  sock.onclose = () => {
    if (ws === sock) ws = null;
    state = "off";
    updateBadge();
    scheduleReconnect();
  };
  sock.onerror = () => {
    try { sock.close(); } catch (e) { /* ignore */ }
  };
}

function disconnect() {
  cfg.autoConnect = false;
  saveCfg();
  if (ws) { try { ws.close(); } catch (e) { /* ignore */ } }
  ws = null;
  state = "off";
  updateBadge();
}

function scheduleReconnect() {
  if (!cfg.autoConnect || !cfg.token) return;
  chrome.alarms.create("reconnect", { when: Date.now() + backoff });
  backoff = Math.min(backoff * 2, 30000);
}

function send(obj) {
  try {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  } catch (e) { /* ignore */ }
}

// ── command handling ─────────────────────────────────────────────────────────
async function onMessage(raw) {
  let msg;
  try { msg = JSON.parse(raw); } catch (e) { return; }
  if (msg.type === "welcome") return;
  if (msg.type === "control") {
    await handleControl(msg);
    return;
  }
  if (msg.type !== "cmd") return;

  if (cfg.paused) {
    reply(msg.id, { success: false, error: "paused by user", url: "", title: "" });
    return;
  }

  inflight++;
  updateBadge();
  try {
    const out = await dispatch(msg.action, msg.bs_id, msg.params || {});
    reply(msg.id, out);
  } catch (e) {
    reply(msg.id, { success: false, error: String((e && e.message) || e), url: "", title: "" });
  } finally {
    inflight = Math.max(0, inflight - 1);
    updateBadge();
  }
}

async function handleControl(msg) {
  if (msg.action === "shutdown") {
    cfg.autoConnect = false;
    await saveCfg();
    send({ type: "control_ack", action: "shutdown", ok: true });
    try { await chrome.alarms.clear("reconnect"); } catch (e) { /* ignore */ }
    if (ws) { try { ws.close(1000, "Stopped from WebAgent Control"); } catch (e) { /* ignore */ } }
    ws = null;
    state = "off";
    updateBadge();
    return;
  }
  if (msg.action === "settings" && msg.settings && typeof msg.settings === "object") {
    const s = msg.settings;
    if (typeof s.paused === "boolean") cfg.paused = s.paused;
    if (typeof s.auto_connect === "boolean") cfg.autoConnect = s.auto_connect;
    if (typeof s.allow_screenshots === "boolean") cfg.allowScreenshots = s.allow_screenshots;
    if (typeof s.require_eval_approval === "boolean") cfg.requireEvalApproval = s.require_eval_approval;
    await saveCfg();
    updateBadge();
    send({ type: "control_ack", action: "settings", ok: true });
    await announce();
  }
}

function reply(id, out) {
  send(Object.assign({ type: "result", id: id }, out));
}

async function dispatch(action, bsId, p) {
  if (action === "navigate") {
    const tab = await getTab(bsId, true);
    let url = p.url || "about:blank";
    if (url !== "about:blank" && !/:\/\//.test(url)) url = "https://" + url;
    await chrome.tabs.update(tab.id, { url, active: true });
    await waitComplete(tab.id, p.timeout_ms || 30000);
    const t = await chrome.tabs.get(tab.id);
    return { success: true, result: "Navigated to " + url, url: t.url, title: t.title };
  }

  const tab = await getTab(bsId, false);

  if (action === "screenshot") {
    if (!cfg.allowScreenshots) {
      return { success: false, error: "screenshots disabled by user", url: tab.url, title: tab.title };
    }
    try { await chrome.tabs.update(tab.id, { active: true }); } catch (e) { /* ignore */ }
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    return { success: true, result: dataUrl, url: tab.url, title: tab.title, mime_type: "image/png" };
  }
  if (action === "title") return { success: true, result: tab.title || "", url: tab.url, title: tab.title };
  if (action === "url") return { success: true, result: tab.url || "", url: tab.url, title: tab.title };
  if (action === "wait") {
    await sleep(p.timeout_ms || 5000);
    const t = await chrome.tabs.get(tab.id);
    return { success: true, result: "Waited " + (p.timeout_ms || 5000) + "ms", url: t.url, title: t.title };
  }
  if (action === "close") {
    try { await chrome.tabs.remove(tab.id); } catch (e) { /* ignore */ }
    delete bsTabs[bsId];
    return { success: true, result: "Browser tab closed", url: "", title: "" };
  }

  if (action === "evaluate") {
    if (!p.js) return { success: false, error: "js required for evaluate", url: tab.url, title: tab.title };
    if (cfg.requireEvalApproval) {
      const ok = await requestApproval(tab, p.js);
      if (!ok) return { success: false, error: "evaluate denied by user", url: tab.url, title: tab.title };
    }
    const r = await runInPage(tab.id, "MAIN", pageEval, [p.js]);
    return { success: true, result: r === undefined ? "" : String(r), url: tab.url, title: tab.title };
  }

  // DOM actions run in the ISOLATED world (still shares the page's live DOM).
  if (action === "click") {
    const r = await runInPage(tab.id, "ISOLATED", domClick, [p.selector]);
    const t = await chrome.tabs.get(tab.id);
    return Object.assign({ url: t.url, title: t.title }, r);
  }
  if (action === "type") {
    const r = await runInPage(tab.id, "ISOLATED", domType, [p.selector, p.text || ""]);
    const t = await chrome.tabs.get(tab.id);
    return Object.assign({ url: t.url, title: t.title }, r);
  }
  if (action === "get_text") {
    const r = await runInPage(tab.id, "ISOLATED", domGetText, [p.selector || "body"]);
    return Object.assign({ url: tab.url, title: tab.title }, r);
  }
  if (action === "get_html") {
    const r = await runInPage(tab.id, "ISOLATED", domGetHtml, [p.selector || null]);
    return Object.assign({ url: tab.url, title: tab.title }, r);
  }

  return { success: false, error: "unknown action: " + action, url: tab.url, title: tab.title };
}

// ── tab mapping ──────────────────────────────────────────────────────────────
async function getTab(bsId, create) {
  const known = bsTabs[bsId];
  if (known) {
    try {
      const t = await chrome.tabs.get(known);
      if (t) return t;
    } catch (e) {
      delete bsTabs[bsId];
    }
  }
  if (create) {
    const t = await chrome.tabs.create({ url: "about:blank", active: true });
    bsTabs[bsId] = t.id;
    return t;
  }
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tabs && tabs[0]) {
    bsTabs[bsId] = tabs[0].id;
    return tabs[0];
  }
  throw new Error("no active tab to drive");
}

// ── in-page execution ────────────────────────────────────────────────────────
async function runInPage(tabId, world, func, args) {
  const res = await chrome.scripting.executeScript({ target: { tabId }, world, func, args });
  return res && res[0] ? res[0].result : undefined;
}

// These run INSIDE the page (serialized by chrome.scripting). Keep them
// self-contained — they may only use their args + page globals.
function domClick(selector) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: "no element matches: " + selector };
  el.scrollIntoView({ block: "center", inline: "center" });
  el.click();
  return { success: true, result: "Clicked " + selector };
}
function domType(selector, text) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: "no element matches: " + selector };
  el.focus();
  try { el.value = ""; } catch (e) { /* ignore */ }
  if ("value" in el) {
    el.value = text;
  } else {
    el.textContent = text;
  }
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return { success: true, result: "Typed into " + selector };
}
function domGetText(selector) {
  const el = document.querySelector(selector);
  return { success: true, result: el ? (el.innerText || "") : "" };
}
function domGetHtml(selector) {
  if (!selector) return { success: true, result: document.documentElement.outerHTML };
  const el = document.querySelector(selector);
  return { success: true, result: el ? el.innerHTML : "" };
}
function pageEval(code) {
  // Runs in the page's MAIN world. Subject to the page's CSP (eval may be
  // blocked on strict sites — that surfaces as an error to the agent).
  // eslint-disable-next-line no-eval
  return String(eval(code));
}

// ── evaluate approval popup ──────────────────────────────────────────────────
let approvalSeq = 0;
const approvals = {};

async function requestApproval(tab, js) {
  const id = "ap" + (++approvalSeq);
  await chrome.storage.session.set({ ["approval_" + id]: { js, url: tab.url, title: tab.title } });
  return new Promise((resolve) => {
    approvals[id] = resolve;
    chrome.windows.create({
      url: chrome.runtime.getURL("approve.html?id=" + id),
      type: "popup", width: 480, height: 380,
    });
    // Default-deny if the user doesn't decide in time.
    setTimeout(() => {
      if (approvals[id]) { approvals[id](false); delete approvals[id]; }
    }, 60000);
  });
}

// ── badge + popup messaging ──────────────────────────────────────────────────
function updateBadge() {
  let text = "OFF";
  let color = "#9ca3af";
  if (cfg.paused) { text = "PAU"; color = "#d97706"; }
  else if (state === "connected") {
    if (inflight > 0) { text = String(inflight); color = "#2563eb"; }
    else { text = "ON"; color = "#16a34a"; }
  } else if (state === "connecting") { text = "..."; color = "#d97706"; }
  try {
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color });
  } catch (e) { /* ignore */ }
}

function snapshot() {
  return { state, paused: cfg.paused, inflight, serverUrl: cfg.serverUrl, hasToken: !!cfg.token, version: VERSION };
}

chrome.runtime.onMessage.addListener((m, sender, sendResponse) => {
  if (!m || !m.type) return;
  if (m.type === "approval_result") {
    if (approvals[m.id]) { approvals[m.id](!!m.allow); delete approvals[m.id]; }
    sendResponse({ ok: true });
    return true;
  }
  if (m.type === "get_state") { sendResponse(snapshot()); return true; }
  if (m.type === "set_pause") { cfg.paused = !!m.paused; saveCfg().then(announce); updateBadge(); sendResponse(snapshot()); return true; }
  if (m.type === "reconnect") { cfg.autoConnect = true; saveCfg(); connect(); sendResponse(snapshot()); return true; }
  if (m.type === "disconnect") { disconnect(); sendResponse(snapshot()); return true; }
  if (m.type === "config_changed") { loadCfg().then(async () => { if (cfg.autoConnect) connect(); updateBadge(); await announce(); sendResponse(snapshot()); }); return true; }
  return false;
});

// ── lifecycle ────────────────────────────────────────────────────────────────
function ensureAlarms() {
  // 0.5 min is the MV3 floor. Combined with WebSocket traffic this keeps the
  // worker warm; the alarm also re-drives a reconnect if the socket is down.
  chrome.alarms.create("ping", { periodInMinutes: 0.5 });
}

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "ping") {
    if (state === "connected") send({ type: "ping" });
    else if (cfg.autoConnect) connect();
  } else if (a.name === "reconnect") {
    connect();
  }
});

async function init() {
  await loadCfg();
  ensureAlarms();
  if (cfg.autoConnect) connect();
  updateBadge();
}

chrome.runtime.onInstalled.addListener(() => { init(); });
chrome.runtime.onStartup.addListener(() => { init(); });
// Also run when the worker is woken for any reason.
init();
