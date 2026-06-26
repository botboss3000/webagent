"""Deploy target: Android phone / tablet via Termux (copy-paste install).

Unlike the cloud targets, there is no machine to CREATE here — the "server" is
the user's own Android device running the free Termux app, which is normally NOT
reachable from this server (mobile network / behind NAT / no fixed address). So
this target never connects anywhere: clicking *Deploy* GENERATES one copy-paste
command the admin runs once inside Termux on the phone.

That command installs webAgent inside an Ubuntu ``proot-distro`` environment —
the reliable way to run the full Python stack on Android, since native Termux
trips over the heavy native dependencies (sqlcipher, Playwright, compiled
wheels). It uses the repo's own ``deploy/termux-setup.sh`` to build the venv +
install the Playwright-free dependency set (``req_no_playwright.txt``), then
keeps the server alive in the background with a wake-lock + auto-restart (the
repo's existing ``start_server_termux.sh``).

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
    "display_name": "Android phone (Termux)",
    "category": "deploy",
    "status": "beta",
    "summary": "Install webAgent on an Android phone or tablet running Termux.",
    "requires": ["The free Termux app (from F-Droid)", "~2 GB free storage"],
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
    display_name = "Android phone (Termux)"
    icon = "smartphone"
    summary = ("Install webAgent on an Android phone or tablet using the free Termux "
               "app. You copy one command and paste it into Termux — there is no cloud "
               "account and nothing to pay for.")
    requires = [
        "The free Termux app (install it from F-Droid, not the Play Store)",
        "About 2 GB of free storage on the device",
        "Wi-Fi, if you want to reach the app from a computer on the same network",
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

        command = (
            "pkg update -y && pkg install -y git proot-distro && "
            f'{{ [ -d "$HOME/webagent/.git" ] || git clone --depth 1 --branch {branch} {clone_url} "$HOME/webagent"; }} && '
            'bash "$HOME/webagent/deploy/termux-setup.sh"'
        )
        # A display copy that hides the token (for any logging / non-QR display).
        clone_display = command.replace(clone_url, repo) if private else command

        steps = [
            "Install the free Termux app on the phone (from F-Droid, not the Play Store).",
            "Open Termux and either scan the QR code or paste the command, then press Enter.",
            "First run takes a few minutes — it downloads Ubuntu and the dependencies.",
            "When it finishes, open http://localhost:8080 on the phone, or http://PHONE-IP:8080 "
            "from another device on the same Wi-Fi (the script prints the phone's IP).",
        ]
        instructions = (
            "The command installs webAgent inside an Ubuntu environment on the phone (the reliable "
            "way to run the full app on Android), then keeps it running in the background with a "
            "wake-lock and restarts it if it ever stops. To stop it later, paste in Termux: "
            "proot-distro login ubuntu -- pkill -f run.py")
        reach_note = "http://localhost:8080 on the phone · http://PHONE-IP:8080 on the same Wi-Fi"
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
