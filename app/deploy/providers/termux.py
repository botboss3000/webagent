"""Deploy target: a Linux computer, or an Android phone/tablet via Termux.

Unlike the cloud targets, there is no machine to CREATE here — the "server" is a
device the admin already owns (a Linux box, or an Android device running the free
Termux app), which is normally NOT reachable from this server. So this target
never connects anywhere: clicking *Deploy* GENERATES one copy-paste command the
admin runs once in a terminal (or in Termux).

Termux is essentially a small Linux userland on Android, so ONE command works in
both places — it detects which it is on. The only Termux-specific "customization"
is that a phone can't reliably build webAgent's heavy native dependencies
(sqlcipher, compiled wheels), so on Termux the command installs everything inside
an Ubuntu ``proot-distro`` sandbox; on a plain Linux box it installs straight onto
the system. Both paths live in the repo's ``deploy/termux-setup.sh``, which builds
the venv + installs the Playwright-free dependency set (``req_no_playwright.txt``)
and keeps the server alive in the background with auto-restart (plus a wake-lock on
a phone, via ``start_server_termux.sh``).

This is a "manual" target (``manual = True``): no cloud key, nothing billable,
nothing to tear down remotely. The flag tells the Deploy panel to drop the
cloud-key section + the billable-resource confirm dialog, and to render the
generated command in a copy-to-clipboard box instead of a "last server" line.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Tuple

from app.deploy.base import BaseDeployProvider, done, ev
from app.deploy.bootstrap import DEFAULT_BRANCH, DEFAULT_REPO_URL

FEATURE = {
    "id": "termux",
    "display_name": "Linux / Termux",
    "category": "deploy",
    "status": "beta",
    "summary": "Install webAgent on a Linux computer, or on an Android phone/tablet via Termux.",
    "requires": ["A Linux computer, or the free Termux app on Android", "~2 GB free storage"],
}

_PORT = 8080  # run.py is fixed to :8080

# Shown in the live command when the admin hasn't typed a URL / token yet, so the
# box always renders the SHAPE of the command (the row updates it live as they
# type). Deliberately obvious "fill me in" tokens, valid-looking so the command
# still reads naturally.
PLACEHOLDER_REPO = "https://github.com/YOUR-NAME/YOUR-REPO"
PLACEHOLDER_TOKEN = "YOUR_ACCESS_TOKEN"

# Characters that never legitimately appear in a repo URL / token but would break
# (or worse, inject into) the one-liner if spliced in. Used to reject risky input.
_BAD_URL = "'\";\n\r\\ &|`$(){}<>"
_BAD_TOKEN = "'\";\n\r\\ &|`$(){}<>@/ "


def _safe(value: str, fallback: str) -> str:
    """Tidy a repo/branch the admin typed. The command runs on their own phone,
    but a URL/branch never legitimately contains shell-breaking characters, so we
    fall back to the default rather than splice anything risky into the one-liner.
    """
    v = (value or "").strip() or fallback
    if any(c in v for c in _BAD_URL):
        return fallback
    return v


def _strip_scheme(url: str) -> str:
    for s in ("https://", "http://"):
        if url.startswith(s):
            return url[len(s):]
    return url


class TermuxProvider(BaseDeployProvider):
    id = "termux"
    display_name = "Linux / Termux"
    icon = "terminal"
    summary = ("Install webAgent on a Linux computer, or on an Android phone or tablet "
               "using the free Termux app. You copy one command and paste it into a "
               "terminal (or Termux) — there is no cloud account and nothing to pay for.")
    requires = [
        "A Linux computer, or the free Termux app on an Android device",
        "About 2 GB of free storage",
        "A network connection, if you want to reach the app from another device",
    ]
    # No cloud key, nothing billable, install is copy-paste — the panel adapts.
    manual = True

    # The Deploy panel renders a BESPOKE row for Termux (its own GitHub-URL +
    # public/private + token fields, terminal command + QR), not the generic
    # provider form, so these lists are intentionally empty — the row drives
    # `build_command()` below via POST /admin/deploy/termux/command. The non-secret
    # github_url + visibility persist via the store; the token is transient.
    config_fields: List[Dict[str, Any]] = []
    credential_fields: List[Dict[str, Any]] = []      # no cloud key
    credential_required: List[str] = []

    def available(self) -> Tuple[bool, str]:
        return True, ""

    # ── command generation (the single source of truth) ──
    def build_command(self, github_url: str, visibility: str = "public",
                      token: str = "", branch: str = "") -> Dict[str, Any]:
        """Build everything the phone row needs from the admin's inputs.

        ALWAYS succeeds (never ``{ok: False}``): the row shows this command live
        as the admin types, so a blank URL / not-yet-typed token must still render
        the SHAPE of the command rather than an error. Missing pieces become
        obvious fill-in placeholders (``PLACEHOLDER_REPO`` / ``PLACEHOLDER_TOKEN``)
        and are flagged so the row can nudge the admin to finish them.

        Returns ``{ok: True, command, clone_display, steps, instructions,
        reach_note, private, placeholder_repo, placeholder_token, warning}``. The
        command installs git + proot-distro, clones the repo to ``$HOME/webagent``,
        and hands off to the repo's ``deploy/termux-setup.sh`` (Ubuntu build +
        launch + keep-alive). For a PRIVATE repo the access token is embedded in
        the clone URL so the phone can fetch it; the token is never stored here.
        """
        typed = (github_url or "").strip()
        placeholder_repo = not typed
        repo = PLACEHOLDER_REPO if placeholder_repo else _safe(typed, PLACEHOLDER_REPO)
        branch = _safe(branch, DEFAULT_BRANCH)
        private = str(visibility or "public").lower() == "private"

        clone_url = repo
        warning = ""
        placeholder_token = False
        if private:
            tok = (token or "").strip()
            if tok and any(c in tok for c in _BAD_TOKEN):
                # Don't splice a risky token; show the placeholder and warn instead.
                warning = "That token contains characters that aren't valid in a GitHub token."
                tok = ""
            if not tok:
                tok = PLACEHOLDER_TOKEN
                placeholder_token = True
            # Embed the token in the clone URL (the proven way to fetch a private
            # repo non-interactively). repo is always https-shaped here (a real
            # https URL or the placeholder), so the splice reads naturally.
            clone_url = "https://" + tok + "@" + _strip_scheme(repo)

        # ONE command for both Termux and plain Linux: install git with whatever
        # package manager is present (Termux's `pkg`, or apt/dnf/pacman with sudo
        # on a Linux box — sudo only when not already root), clone the repo, then
        # hand off to the setup script, which detects Termux vs Linux and installs
        # accordingly. `:` is a no-op for the "git already installed" case.
        command = (
            'SUDO=; [ "$(id -u 2>/dev/null)" = 0 ] || SUDO=sudo; '
            'if command -v git >/dev/null 2>&1; then :; '
            'elif command -v pkg >/dev/null 2>&1; then pkg install -y git; '
            'elif command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update && $SUDO apt-get install -y git; '
            'elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y git; '
            'elif command -v pacman >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm git; fi; '
            f'{{ [ -d "$HOME/webagent/.git" ] || git clone --depth 1 --branch {branch} {clone_url} "$HOME/webagent"; }} && '
            'bash "$HOME/webagent/deploy/termux-setup.sh"'
        )
        # A display copy that hides the token (for any logging / non-QR display).
        clone_display = command.replace(clone_url, repo) if private else command

        steps = [
            "On a phone: install the free Termux app, then open it. On a Linux computer: open a terminal.",
            "Scan the QR code or paste the command, then press Enter.",
            "The first run takes a few minutes while it installs everything (on a phone it also "
            "sets up a small Ubuntu environment).",
            "When it finishes, open http://localhost:8080 on that device, or http://DEVICE-IP:8080 "
            "from another device on the same network (the script prints the address).",
        ]
        instructions = (
            "On a phone the command installs webAgent inside a small Ubuntu environment (the reliable "
            "way to run the full app on Android); on a Linux computer it installs straight onto the "
            "system. Either way it keeps running in the background and restarts itself if it stops. On "
            "a phone it also installs the Server Manager — type 'webagent' in Termux to inspect, "
            "restart or diagnose the install. To stop it later: on a phone paste 'proot-distro login "
            "ubuntu -- pkill -f run.py', on Linux paste 'pkill -f run.py'.")
        reach_note = "http://localhost:8080 on the device · http://DEVICE-IP:8080 on the same network"
        return {"ok": True, "command": command, "clone_display": clone_display,
                "steps": steps, "instructions": instructions, "reach_note": reach_note,
                "private": private, "placeholder_repo": placeholder_repo,
                "placeholder_token": placeholder_token, "warning": warning}

    # ── test (nothing to connect to) ──
    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True,
                "detail": "Termux installs by pasting a command on the phone — there is nothing to test."}

    # ── deploy = stream the (public) command, for the generic contract ──
    async def deploy(self, config: Dict[str, Any], creds: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        plan = self.build_command(config.get("github_url") or DEFAULT_REPO_URL,
                                  config.get("visibility") or "public")
        if not plan.get("ok"):
            yield done({"ok": False, "message": plan.get("error", "Could not build the command.")})
            return
        yield ev("Command ready — copy it into Termux on the phone.", phase="ready", level="ok")
        yield done({"ok": True, "server": "", "state": "manual",
                    "command": plan["command"], "instructions": plan["instructions"],
                    "public_url": "", "message": "Install command generated."})

    async def destroy(self, config: Dict[str, Any], creds: Dict[str, Any],
                      record: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        yield done({"ok": True, "deleted": False,
                    "message": "Termux runs on your own device — stop it with: proot-distro login ubuntu -- pkill -f run.py"})


PROVIDER = TermuxProvider()
