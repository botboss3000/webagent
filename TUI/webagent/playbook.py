"""Pure decision logic for the self-healing Playbook.

This module holds NO state and touches NO database — it is the brain that sits on
top of the rows in ``db.py`` (``playbook_*`` tables) and the actuators in
``watchdog.py`` / ``app.py``. Keeping it pure means the watchdog and the tools can
be exercised in tests without an event loop, a server, or a real ``Store``.

Responsibilities:
* **fingerprint** a detected condition into a stable issue key so the same problem
  clusters across occurrences (and across TUI restarts);
* describe the **built-in remedy catalog** (the safe actions the watchdog can run);
* **rank** an issue's remedies by how often they actually helped (confidence);
* **gate** whether a chosen remedy may auto-run under the configured
  ``remediation_mode`` + ``autonomy`` (the two compose);
* decide when a recurring issue should **program a trigger** onto itself.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# ── Remedy catalog ───────────────────────────────────────────────────────────
# The built-in, parameter-free remedies the watchdog knows how to execute. Custom
# remedies authored by the agent use kind 'command' (a shell command) or 'note'
# (a written instruction, never auto-run — surfaced as a suggestion).
RESTART = "restart_server"
CLEAR_PORT = "clear_port"
ESCALATE = "escalate_to_agent"
NOTIFY = "notify_only"
COMMAND = "command"
NOTE = "note"

SAFE_KINDS = (RESTART, CLEAR_PORT, ESCALATE, NOTIFY)
CATALOG_KINDS = SAFE_KINDS + (COMMAND, NOTE)

# Built-in issue keys (one per dedicated watchdog detector) → their default remedy.
# These seed an issue's first remedy so the system has something to try before it
# has learned anything; learning then re-ranks against any added remedies.
BUILTIN_DEFAULT_REMEDY = {
    "server_down": RESTART,
    "crash_loop": ESCALATE,
    "port_zombie": CLEAR_PORT,
    "error_spike": ESCALATE,
    "resource_disk": NOTIFY,
    "resource_memory": NOTIFY,
    "resource_cpu": NOTIFY,
    "untracked_server": NOTIFY,
    # More than one process listening on 8080 at once (run.py binds with
    # SO_REUSEADDR, so duplicates can coexist). Trim the extras down to the one
    # tied to the linked checkout — clearing the surplus listeners is a port-clear.
    "duplicate_instances": CLEAR_PORT,
    # A server answering 8080 from a DIFFERENT checkout (a stale duplicate clone,
    # often under temp/ or a *-fresh-install) that shadows the linked repo and
    # serves old code. Needs the agent to identify the right checkout, stop the
    # impostor, and confirm with the user before removing the stray clone.
    "zombie_repo": ESCALATE,
}

BUILTIN_LABELS = {
    "server_down": "Server down",
    "crash_loop": "Server crash-loop",
    "port_zombie": "Port held by a zombie",
    "error_spike": "Error spike",
    "resource_disk": "Disk almost full",
    "resource_memory": "Memory pressure high",
    "resource_cpu": "CPU sustained high",
    "untracked_server": "Untracked server",
    "duplicate_instances": "Multiple servers on 8080",
    "zombie_repo": "Zombie repo shadowing 8080",
}

REMEDY_HUMAN = {
    RESTART: "restart the server",
    CLEAR_PORT: "clear the process holding port 8080",
    ESCALATE: "hand it to the agent to diagnose + fix",
    NOTIFY: "notify only (no automatic action)",
    COMMAND: "run a saved command",
    NOTE: "follow a written instruction (manual)",
}

VALID_MODES = ("document", "safe_auto", "autonomous")


# ── Fingerprinting ───────────────────────────────────────────────────────────
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")
_PATH = re.compile(r"[A-Za-z]:\\[^\s'\":]+|/[^\s'\":]+")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def normalize_message(message: str) -> str:
    """Collapse the volatile parts of an error message (paths, numbers, hex,
    UUIDs) so near-identical errors map to ONE issue key."""
    s = (message or "").strip().lower()
    s = _UUID.sub("<id>", s)
    s = _HEX.sub("<hex>", s)
    s = _PATH.sub("<path>", s)
    s = _NUM.sub("<n>", s)
    s = _WS.sub(" ", s)
    return s[:120]


def diagnostic_key(level: str, category: str, message: str) -> str:
    """A stable key for a diagnostic-driven issue."""
    norm = normalize_message(message)
    # A short, deterministic digest keeps the key compact but distinct. (Python's
    # builtin hash is salted per-process, so use a simple stable rolling hash.)
    h = 0
    for ch in norm:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return f"diag|{(level or '').lower()}|{(category or '').lower()}|{h:08x}"


# ── Confidence + ranking ─────────────────────────────────────────────────────
def confidence(remedy: dict[str, Any]) -> float:
    """How much we trust a remedy = Laplace-smoothed success rate. With no trials
    it sits at 0.5 (genuinely unknown), then moves toward the observed rate as
    evidence accrues. Disabled remedies score 0 so they never win."""
    if remedy.get("status") == "disabled":
        return 0.0
    helped = int(remedy.get("times_helped") or 0)
    didnt = int(remedy.get("times_didnt_help") or 0)
    return (helped + 1.0) / (helped + didnt + 2.0)


def rank_remedies(remedies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order remedies best-first: enabled before disabled, then by confidence,
    then by explicit priority, then by how much evidence backs them."""
    def keyf(r: dict[str, Any]):
        return (
            r.get("status") != "disabled",
            round(confidence(r), 4),
            int(r.get("priority") or 0),
            int(r.get("times_helped") or 0),
        )
    return sorted(remedies, key=keyf, reverse=True)


def best_remedy(remedies: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The top-ranked remedy that is not disabled, or None."""
    for r in rank_remedies(remedies):
        if r.get("status") != "disabled":
            return r
    return None


# ── Gating: may this remedy auto-run right now? ──────────────────────────────
def gate(remedy: dict[str, Any], *, mode: str, autonomy: str, auto_restart: bool,
         conf: float, threshold: float) -> bool:
    """Decide whether a remedy may run WITHOUT asking. ``remediation_mode`` is the
    master switch; the existing ``autonomy`` level still governs the underlying
    action, so BOTH must permit it.

    * ``document`` → never auto-runs (record + suggest only).
    * ``safe_auto`` → built-in safe remedies only, subject to autonomy.
    * ``autonomous`` → also approved custom commands, if confidence clears the bar.
    """
    if mode not in ("safe_auto", "autonomous"):
        return False
    kind = remedy.get("kind")
    if kind == RESTART:
        return auto_restart and autonomy in ("auto_restart", "self_heal")
    if kind == CLEAR_PORT:
        return autonomy in ("auto_restart", "self_heal")
    if kind == ESCALATE:
        return autonomy == "self_heal"
    if kind == NOTIFY:
        return True
    if kind == COMMAND:
        # An approved command may run if it is still unproven (give it a chance to
        # earn its record) OR it has cleared the confidence bar. Once it has been
        # tried a couple of times and is below the bar, it stops auto-running.
        tried = int(remedy.get("times_tried") or 0)
        proven_bad = tried >= 2 and conf < threshold
        return (mode == "autonomous" and remedy.get("status") == "approved"
                and autonomy in ("auto_restart", "self_heal") and not proven_bad)
    # NOTE and anything unknown: never auto-run.
    return False


def should_program_trigger(issue: dict[str, Any], program_after: int) -> bool:
    """A recurring DIAGNOSTIC issue earns an explicit standing alarm rule once it
    has happened enough times — this is the system 'programming a trigger onto
    itself' so the problem is loudly + explicitly detected from then on. Built-in
    issues already have dedicated detectors, so they're never alarm-programmed —
    they just graduate to 'known' status instead."""
    return (issue.get("kind") == "diagnostic"
            and not issue.get("programmed")
            and int(issue.get("occurrences") or 0) >= max(1, program_after))


def keyword(message: str) -> str:
    """Pick a stable, representative word from an error message to use as an alarm
    'contains' fragment when programming a trigger. Returns '' if none stands out."""
    best = ""
    for raw in re.findall(r"[A-Za-z]{5,}", message or ""):
        w = raw.lower()
        if w in ("error", "errors", "failed", "failure", "exception", "traceback",
                 "warning", "during", "while", "which", "there", "their", "cannot"):
            continue
        if len(w) > len(best):
            best = w
    return best
