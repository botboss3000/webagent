"""Shared helpers for MANUAL deploy targets (Windows, macOS, …).

A "manual" deploy target installs webAgent onto a device the admin already owns by
handing back a single copy-paste command — no cloud account, nothing billable
(see ``app/deploy/base.BaseDeployProvider.manual``). Every such target needs the
SAME pre-command logic before its platform-specific shell line: tidy the repo URL
the admin typed, and — for a private repo — splice an access token into the clone
URL so the device can fetch it. That logic lives here so each platform provider
(``app/deploy/providers/windows.py``, ``app/deploy/providers/macos.py``) only has
to describe its own one-liner.

The Linux/Termux target (``app/deploy/providers/termux.py``) predates this module
and keeps its own copy of the same helpers; the browser mirrors all of them in
``ui/admin-tools/app-config/app-settings/deploy.js`` so the command box renders
LIVE even before the server is restarted.
"""

from __future__ import annotations

from typing import Any, Dict

# Shown in the live command when the admin hasn't typed a URL / token yet, so the
# box always renders the SHAPE of the command. Deliberately obvious "fill me in"
# tokens, valid-looking so the command still reads naturally.
PLACEHOLDER_REPO = "https://github.com/YOUR-NAME/YOUR-REPO"
PLACEHOLDER_TOKEN = "YOUR_ACCESS_TOKEN"

# Characters that never legitimately appear in a repo URL / token but would break
# (or worse, inject into) the one-liner if spliced in. Used to reject risky input.
BAD_URL = "'\";\n\r\\ &|`$(){}<>"
BAD_TOKEN = "'\";\n\r\\ &|`$(){}<>@/ "


def _safe(value: str, fallback: str) -> str:
    """Tidy a repo/branch the admin typed. The command runs on the admin's own
    machine, but a URL/branch never legitimately contains shell-breaking
    characters, so fall back to the placeholder rather than splice anything risky.
    """
    v = (value or "").strip() or fallback
    if any(c in v for c in BAD_URL):
        return fallback
    return v


def strip_scheme(url: str) -> str:
    for s in ("https://", "http://"):
        if url.startswith(s):
            return url[len(s):]
    return url


def resolve_clone(github_url: str, visibility: str = "public", token: str = "") -> Dict[str, Any]:
    """Work out the clone target from the admin's inputs — shared by every manual
    platform provider (only the surrounding shell command differs per platform).

    NEVER errors: the row shows the command live as the admin types, so a blank
    URL / not-yet-typed token must still resolve to the SHAPE of the command.
    Missing pieces become obvious fill-in placeholders and are flagged so the row
    can nudge the admin to finish them.

    Returns ``{repo, clone_url, private, placeholder_repo, placeholder_token,
    warning}``. ``clone_url`` is what goes into ``git clone`` — for a PRIVATE repo
    the access token is embedded (``https://TOKEN@host/...``) so the device can
    fetch it; the token is never stored.
    """
    typed = (github_url or "").strip()
    placeholder_repo = not typed
    repo = PLACEHOLDER_REPO if placeholder_repo else _safe(typed, PLACEHOLDER_REPO)
    private = str(visibility or "public").lower() == "private"

    clone_url = repo
    warning = ""
    placeholder_token = False
    if private:
        tok = (token or "").strip()
        if tok and any(c in tok for c in BAD_TOKEN):
            warning = "That token contains characters that aren't valid in a GitHub token."
            tok = ""
        if not tok:
            tok = PLACEHOLDER_TOKEN
            placeholder_token = True
        # repo is always https-shaped here (a real https URL or the placeholder),
        # so the splice reads naturally.
        clone_url = "https://" + tok + "@" + strip_scheme(repo)

    return {"repo": repo, "clone_url": clone_url, "private": private,
            "placeholder_repo": placeholder_repo, "placeholder_token": placeholder_token,
            "warning": warning}
