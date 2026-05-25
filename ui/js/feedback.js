/**
 * In-app feedback form.
 *
 *   1. On load, fetch /api/v1/feedback/config to learn whether feedback is
 *      enabled on this deployment and what the Turnstile site key is.
 *   2. If enabled, reveal the "Send feedback" item in the user dropdown.
 *   3. When opened, lazy-load the Turnstile script (if a site key is
 *      configured) and render an invisible widget.
 *   4. On submit, POST {type, body, email, turnstile_token} to
 *      /api/v1/feedback. The FastAPI route forwards to the Cloudflare relay.
 */

const CONFIG_URL = "/api/v1/feedback/config";
const SUBMIT_URL = "/api/v1/feedback";

let configPromise = null;
let turnstileWidgetId = null;
let turnstileLoaded = false;

function $(id) { return document.getElementById(id); }

function loadConfig() {
  if (!configPromise) {
    configPromise = fetch(CONFIG_URL, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : { enabled: false, turnstile_site_key: "" }))
      .catch(() => ({ enabled: false, turnstile_site_key: "" }));
  }
  return configPromise;
}

function loadTurnstileScript() {
  if (turnstileLoaded) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    s.async = true;
    s.defer = true;
    s.onload = () => { turnstileLoaded = true; resolve(); };
    s.onerror = () => reject(new Error("Failed to load Turnstile"));
    document.head.appendChild(s);
  });
}

async function ensureTurnstileWidget(siteKey) {
  if (!siteKey) return;
  await loadTurnstileScript();
  if (turnstileWidgetId !== null) return;
  const host = $("feedback-turnstile");
  if (!host || !window.turnstile) return;
  turnstileWidgetId = window.turnstile.render(host, {
    sitekey: siteKey,
    appearance: "interaction-only",
    size: "flexible",
  });
}

function resetTurnstile() {
  if (turnstileWidgetId !== null && window.turnstile) {
    try { window.turnstile.reset(turnstileWidgetId); } catch (_) {}
  }
}

function getTurnstileToken() {
  if (turnstileWidgetId !== null && window.turnstile) {
    try { return window.turnstile.getResponse(turnstileWidgetId) || ""; } catch (_) { return ""; }
  }
  return "";
}

function setStatus(msg, kind) {
  const el = $("feedback-status");
  if (!el) return;
  el.textContent = msg || "";
  el.className = "feedback-status" + (kind ? " " + kind : "");
}

function openModal() {
  const m = $("feedback-modal");
  if (!m) return;
  m.style.display = "flex";
  setStatus("");
  loadConfig().then((cfg) => {
    if (cfg.turnstile_site_key) ensureTurnstileWidget(cfg.turnstile_site_key).catch(() => {});
  });
  setTimeout(() => { $("feedback-body")?.focus(); }, 50);
}

function closeModal() {
  const m = $("feedback-modal");
  if (!m) return;
  m.style.display = "none";
}

async function handleSubmit(e) {
  e.preventDefault();
  const typeRadio = document.querySelector('input[name="feedback-type"]:checked');
  const type = typeRadio ? typeRadio.value : "bug";
  const body = $("feedback-body")?.value.trim() || "";
  const email = $("feedback-email")?.value.trim() || "";
  const turnstile_token = getTurnstileToken();

  if (!body) {
    setStatus("Please describe your feedback before sending.", "error");
    return;
  }

  const cfg = await loadConfig();
  if (cfg.turnstile_site_key && !turnstile_token) {
    setStatus("Please complete the captcha first.", "error");
    return;
  }

  const btn = $("feedback-submit-btn");
  if (btn) btn.disabled = true;
  setStatus("Sending…");

  try {
    const r = await fetch(SUBMIT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        type,
        body,
        email: email || null,
        turnstile_token: turnstile_token || null,
      }),
    });
    if (r.status === 429) {
      setStatus("Too many submissions. Please try again later.", "error");
      resetTurnstile();
      return;
    }
    if (!r.ok) {
      let detail = "";
      try { detail = (await r.json())?.detail || ""; } catch (_) {}
      setStatus(detail || "Could not send feedback. Please try again.", "error");
      resetTurnstile();
      return;
    }
    const data = await r.json().catch(() => ({}));
    setStatus(
      data.issue_url
        ? "Thanks — your feedback was received."
        : "Thanks — your feedback was received.",
      "success",
    );
    $("feedback-form")?.reset();
    resetTurnstile();
    setTimeout(closeModal, 1400);
  } catch (_) {
    setStatus("Network error. Please try again.", "error");
    resetTurnstile();
  } finally {
    if (btn) btn.disabled = false;
  }
}

function wireUp() {
  const openBtn = $("btn-send-feedback");
  const form = $("feedback-form");
  const bodyEl = $("feedback-body");
  const countEl = $("feedback-body-count");

  if (openBtn) {
    openBtn.addEventListener("click", () => {
      // Close the user dropdown if it's open.
      const dd = $("user-dropdown-menu");
      if (dd) dd.style.display = "none";
      openModal();
    });
  }

  if (form) form.addEventListener("submit", handleSubmit);
  if (bodyEl && countEl) {
    bodyEl.addEventListener("input", () => { countEl.textContent = String(bodyEl.value.length); });
  }

  document.querySelectorAll("[data-feedback-close]").forEach((el) => {
    el.addEventListener("click", closeModal);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("feedback-modal")?.style.display !== "none") closeModal();
  });

  // Reveal the button only if feedback is enabled on this deployment.
  loadConfig().then((cfg) => {
    if (cfg.enabled && openBtn) openBtn.style.display = "";
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", wireUp);
} else {
  wireUp();
}
