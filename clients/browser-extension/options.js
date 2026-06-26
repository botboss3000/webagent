"use strict";

const DEFAULTS = {
  serverUrl: "ws://127.0.0.1:8080",
  token: "",
  autoConnect: true,
  allowScreenshots: true,
  requireEvalApproval: true,
  paused: false,
};

const $ = (id) => document.getElementById(id);

async function load() {
  const s = await chrome.storage.local.get("cfg");
  const cfg = Object.assign({}, DEFAULTS, s.cfg || {});
  $("serverUrl").value = cfg.serverUrl;
  $("token").value = cfg.token;
  $("autoConnect").checked = !!cfg.autoConnect;
  $("allowScreenshots").checked = !!cfg.allowScreenshots;
  $("requireEvalApproval").checked = !!cfg.requireEvalApproval;
}

async function save() {
  const s = await chrome.storage.local.get("cfg");
  const cfg = Object.assign({}, DEFAULTS, s.cfg || {}, {
    serverUrl: $("serverUrl").value.trim(),
    token: $("token").value.trim(),
    autoConnect: $("autoConnect").checked,
    allowScreenshots: $("allowScreenshots").checked,
    requireEvalApproval: $("requireEvalApproval").checked,
  });
  await chrome.storage.local.set({ cfg });
  chrome.runtime.sendMessage({ type: "config_changed" });
  const tag = $("saved");
  tag.classList.add("show");
  setTimeout(() => tag.classList.remove("show"), 1500);
}

$("save").addEventListener("click", save);
load();
