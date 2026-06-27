"""Deploy target: a Windows PC.

Like the Linux/Termux target (``app/deploy/providers/termux.py``), there is no
machine to CREATE here — the "server" is a PC the admin already owns. So this
target never connects anywhere: it GENERATES one command the admin pastes into
**PowerShell** once. The command installs git if missing (via winget), clones the
repo to ``%USERPROFILE%\\webagent``, then runs the repo's
``deploy/windows-setup.ps1`` — which installs ``uv`` (Astral's Python toolchain),
syncs the dependencies, and registers a **Scheduled Task** so webAgent runs in the
background, restarts itself if it stops, and starts again when the user logs in.

This is a "manual" target (``manual = True``): no cloud key, nothing billable,
nothing to tear down remotely. The flag tells the Deploy panel to render this as a
bespoke row (its own GitHub-URL + public/private + token fields, live command +
QR) instead of the cloud-provider dropdown. The shared clone-URL logic lives in
``app/deploy/manual_common.py``; the browser mirrors ``build_command`` in
``deploy.js`` so the box renders live before any server restart.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Tuple

from app.deploy import manual_common as mc
from app.deploy.base import BaseDeployProvider, done, ev
from app.deploy.bootstrap import DEFAULT_BRANCH, DEFAULT_REPO_URL

FEATURE = {
    "id": "windows",
    "display_name": "Windows",
    "category": "deploy",
    "status": "beta",
    "summary": "Install webAgent on a Windows PC by pasting one command into PowerShell.",
    "requires": ["A Windows 10 or 11 PC", "~2 GB free storage"],
}

# The PowerShell one-liner. ``__CLONE__`` / ``__BRANCH__`` are substituted below
# (NOT str.format — the script is full of `$var`, `{}`, quotes that would collide).
# Keep BYTE-IDENTICAL to deploy.js `_winBuild`.
_TEMPLATE = (
    "$ErrorActionPreference='Stop'; "
    "$repo='__CLONE__'; $dir=\"$env:USERPROFILE\\webagent\"; "
    "if(-not(Get-Command git -EA SilentlyContinue)){Write-Host 'Installing Git...'; "
    "try{winget install --id Git.Git -e --source winget "
    "--accept-package-agreements --accept-source-agreements --silent}catch{}; "
    "$env:Path=[Environment]::GetEnvironmentVariable('Path','Machine')+';'"
    "+[Environment]::GetEnvironmentVariable('Path','User')}; "
    "if(-not(Get-Command git -EA SilentlyContinue)){"
    "Write-Host 'Git is required. Install it from https://git-scm.com/download/win then run this again.'; "
    "return}; "
    "if(-not(Test-Path \"$dir\\.git\")){git clone --depth 1 --branch __BRANCH__ $repo \"$dir\"}; "
    "powershell -NoProfile -ExecutionPolicy Bypass -File \"$dir\\deploy\\windows-setup.ps1\""
)

# Run-only command: start the server when webAgent is ALREADY installed — no
# clone, no rebuild. Static (no repo URL / token). Prefers the “webAgent”
# Scheduled Task the installer registers; falls back to the keep-alive ps1 if the
# task isn't there. Keep BYTE-IDENTICAL to deploy.js `_RUN_WINDOWS`.
RUN_COMMAND = (
    "if(Get-ScheduledTask -TaskName webAgent -EA SilentlyContinue){Start-ScheduledTask -TaskName webAgent}"
    "else{powershell -NoProfile -ExecutionPolicy Bypass -File \"$env:USERPROFILE\\webagent\\deploy\\start_server_windows.ps1\"}"
)


class WindowsProvider(BaseDeployProvider):
    id = "windows"
    display_name = "Windows"
    icon = "monitor"
    summary = ("Install webAgent on a Windows PC. You copy one command and paste it "
               "into PowerShell — there is no cloud account and nothing to pay for.")
    requires = [
        "A Windows 10 or 11 PC",
        "About 2 GB of free storage",
        "A network connection, if you want to reach the app from another device",
    ]
    # No cloud key, nothing billable, install is copy-paste — the panel adapts.
    manual = True

    # The Deploy panel renders a BESPOKE row for this target (its own fields +
    # live command + QR), not the generic provider form, so these are empty — the
    # row drives `build_command()` via POST /admin/deploy/command. The non-secret
    # github_url + visibility persist via the store; the token is transient.
    config_fields: List[Dict[str, Any]] = []
    credential_fields: List[Dict[str, Any]] = []
    credential_required: List[str] = []

    def available(self) -> Tuple[bool, str]:
        return True, ""

    # ── command generation (the single source of truth) ──
    def build_command(self, github_url: str, visibility: str = "public",
                      token: str = "", branch: str = "") -> Dict[str, Any]:
        """Build everything the Windows row needs. ALWAYS succeeds (never
        ``{ok: False}``) — see ``manual_common.resolve_clone`` for why. Returns
        ``{ok, command, clone_display, steps, instructions, reach_note, private,
        placeholder_repo, placeholder_token, warning}``."""
        r = mc.resolve_clone(github_url, visibility, token)
        branch = mc._safe(branch, DEFAULT_BRANCH)
        command = _TEMPLATE.replace("__CLONE__", r["clone_url"]).replace("__BRANCH__", branch)
        # A display copy that hides the token (for any logging / non-QR display).
        clone_display = command.replace(r["clone_url"], r["repo"]) if r["private"] else command

        steps = [
            "Open PowerShell: click Start, type 'PowerShell', and open it.",
            "Paste the command and press Enter. (If Windows offers to install Git, allow it.)",
            "The first run takes a few minutes while it downloads Python and installs everything.",
            "When it finishes, open http://localhost:8080 on this PC, or http://THIS-PC-IP:8080 "
            "from another device on the same network.",
        ]
        instructions = (
            "webAgent installs into a folder in your user profile and runs in the background as a "
            "Scheduled Task named 'webAgent' — it starts automatically when you log in and restarts "
            "itself if it stops. To stop it, run in PowerShell: 'Stop-ScheduledTask -TaskName webAgent'. "
            "To stop it starting on login: 'Unregister-ScheduledTask -TaskName webAgent -Confirm:$false'.")
        reach_note = "http://localhost:8080 on this PC · http://THIS-PC-IP:8080 on the same network"
        return {"ok": True, "command": command, "clone_display": clone_display,
                "run_command": RUN_COMMAND,
                "steps": steps, "instructions": instructions, "reach_note": reach_note,
                "private": r["private"], "placeholder_repo": r["placeholder_repo"],
                "placeholder_token": r["placeholder_token"], "warning": r["warning"]}

    # ── test (nothing to connect to) ──
    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True,
                "detail": "Windows installs by pasting a command into PowerShell — there is nothing to test."}

    # ── deploy = stream the (public) command, for the generic contract ──
    async def deploy(self, config: Dict[str, Any], creds: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        plan = self.build_command(config.get("github_url") or DEFAULT_REPO_URL,
                                  config.get("visibility") or "public")
        yield ev("Command ready — paste it into PowerShell on the PC.", phase="ready", level="ok")
        yield done({"ok": True, "server": "", "state": "manual",
                    "command": plan["command"], "instructions": plan["instructions"],
                    "public_url": "", "message": "Install command generated."})

    async def destroy(self, config: Dict[str, Any], creds: Dict[str, Any],
                      record: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        yield done({"ok": True, "deleted": False,
                    "message": "webAgent runs on your own PC — stop it with: Stop-ScheduledTask -TaskName webAgent"})


PROVIDER = WindowsProvider()
