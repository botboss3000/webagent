"use strict";

const id = new URLSearchParams(location.search).get("id");
let decided = false;

async function init() {
  const key = "approval_" + id;
  const s = await chrome.storage.session.get(key);
  const info = s[key] || {};
  document.getElementById("where").textContent = (info.title ? info.title + " — " : "") + (info.url || "");
  document.getElementById("code").textContent = info.js || "(no code)";
}

function decide(allow) {
  if (decided) return;
  decided = true;
  chrome.runtime.sendMessage({ type: "approval_result", id, allow });
  chrome.storage.session.remove("approval_" + id);
  window.close();
}

document.getElementById("allow").addEventListener("click", () => decide(true));
document.getElementById("deny").addEventListener("click", () => decide(false));
// Closing the window without choosing = deny.
window.addEventListener("unload", () => { if (!decided) chrome.runtime.sendMessage({ type: "approval_result", id, allow: false }); });

init();
