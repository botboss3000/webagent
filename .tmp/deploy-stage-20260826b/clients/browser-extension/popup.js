"use strict";

function ask(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

function render(s) {
  if (!s) return;
  const dot = document.getElementById("dot");
  dot.className = "dot" + (s.paused ? " paused" : s.state === "connected" ? " on" : s.state === "connecting" ? " connecting" : "");
  document.getElementById("status").textContent =
    s.paused ? "Paused" : s.state === "connected" ? "Connected" : s.state === "connecting" ? "Connecting…" : "Off";
  document.getElementById("inflight").textContent = String(s.inflight || 0);
  document.getElementById("server").textContent = s.serverUrl || "—";
  document.getElementById("pause").textContent = s.paused ? "Resume" : "Pause";
  document.getElementById("conn").textContent = s.state === "off" ? "Connect" : "Disconnect";
}

async function refresh() { render(await ask({ type: "get_state" })); }

document.getElementById("pause").addEventListener("click", async () => {
  const s = await ask({ type: "get_state" });
  render(await ask({ type: "set_pause", paused: !s.paused }));
});

document.getElementById("conn").addEventListener("click", async () => {
  const s = await ask({ type: "get_state" });
  render(await ask({ type: s.state === "off" ? "reconnect" : "disconnect" }));
});

document.getElementById("opts").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

refresh();
setInterval(refresh, 1500);
