"""The Playbook coordinator — the stateful glue between the watchdog and storage.

The watchdog stays decoupled from the database: it holds one of these objects (or
``None`` on a bare/test watchdog) and calls its small method surface. The
coordinator owns the ``Store`` (the manager's ``webagent.db``), seeds built-in
default remedies, and — when a recurring diagnostic issue earns it — programs a
standing alarm rule via ``monstate`` (the same machinery the agent's ``add_alarm``
tool uses). All ranking/gating decisions live in the pure ``playbook`` module.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import monstate, playbook
from .db import Store


class PlaybookCoordinator:
    def __init__(self, store: Store, log: Optional[Callable[[str], None]] = None) -> None:
        self._store = store
        self._log = log or (lambda s: None)

    # ── recording ────────────────────────────────────────────────────────
    def record(self, key: str, label: str, kind: str,
               match: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Upsert the issue (bump occurrence) and, for a brand-new built-in issue,
        seed its default remedy so there's something to try. Returns the issue row."""
        match = match or {}
        issue = self._store.pb_upsert_issue(
            key, label, kind,
            match_contains=match.get("contains", ""),
            match_level=match.get("level", ""),
            match_category=match.get("category", ""),
        )
        if not self._store.pb_remedies_for(key):
            default = playbook.BUILTIN_DEFAULT_REMEDY.get(key)
            if default:
                self._store.pb_add_remedy(key, default, status="approved", priority=10)
        return issue

    def remedies(self, key: str) -> list[dict[str, Any]]:
        rows = self._store.pb_remedies_for(key)
        # Carry the issue label onto each remedy so the watchdog can build an
        # escalation message without a second lookup.
        issue = self._store.pb_get_issue(key) or {}
        for r in rows:
            r["issue_label"] = issue.get("label", "")
        return rows

    def open_incident(self, key: str, trigger: dict[str, Any]) -> str:
        return self._store.pb_open_incident(key, trigger)

    def close_incident(self, incident_id: str, outcome: str,
                       remedy_id: str = "", notes: str = "") -> None:
        if remedy_id:
            self._store.pb_set_incident_remedy(incident_id, remedy_id)
        self._store.pb_close_incident(incident_id, outcome, notes)

    def tally(self, remedy_id: str, helped: bool) -> None:
        if remedy_id:
            self._store.pb_record_outcome(remedy_id, helped)

    # ── self-programming ─────────────────────────────────────────────────
    def program(self, key: str) -> str:
        """Program an explicit trigger for a recurring issue. For a diagnostic issue
        this creates a standing alarm rule; for a built-in issue it just promotes the
        issue to 'known'. Idempotent (the issue's ``programmed`` flag guards it).
        Returns a short human description of what it did (or '')."""
        issue = self._store.pb_get_issue(key)
        if not issue or issue.get("programmed"):
            return ""
        desc = ""
        if issue.get("kind") == "diagnostic":
            try:
                monstate.add_alarm(
                    contains=issue.get("match_contains", ""),
                    level=issue.get("match_level", ""),
                    category=issue.get("match_category", ""),
                    action="notify", loudness="once",
                    label=f"auto: {issue.get('label', key)}",
                )
                desc = f"programmed an alarm for recurring issue '{issue.get('label', key)}'"
            except ValueError:
                # No usable criteria — still mark known so we don't retry every tick.
                desc = f"marked recurring issue '{issue.get('label', key)}' as known"
        self._store.pb_mark_programmed(key)
        self._log(f"[playbook] {desc}" if desc else "")
        return desc
