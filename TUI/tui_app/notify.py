"""How the Server Manager reaches the user — a small, pluggable notifier.

v1 channel: **desktop** (a native OS notification on the machine running the
manager). The design is deliberately channel-based so Telegram / email / an
in-webapp banner can be added later as new ``_send_*`` methods without touching
the watchdog that calls ``notify()``.

Two guarantees:
* **It never raises.** A failed toast must never take down the watchdog.
* **The transcript always gets the line.** Whatever the channel does, the
  manager also logs the alert into the chat via the ``log`` callback, so the
  user sees it even if the OS toast is suppressed.

Desktop delivery is best-effort and dependency-free: Windows uses a PowerShell
tray balloon (no extra package), macOS uses ``osascript``, Linux uses
``notify-send`` if present.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from typing import Callable, Iterable, Optional

_IS_WIN = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"
_NO_WINDOW = {"creationflags": 0x08000000} if _IS_WIN else {}  # CREATE_NO_WINDOW


# Win10/11 tray balloon via WinForms NotifyIcon — no third-party module needed.
# Title/message are passed through the environment (WT_TITLE / WT_MSG) so no user
# text is ever interpolated into the script (no quoting/injection issues). The
# process sleeps briefly so the balloon has time to show, then disposes the icon.
_WIN_PS = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "Add-Type -AssemblyName System.Drawing;"
    "$n = New-Object System.Windows.Forms.NotifyIcon;"
    "$n.Icon = [System.Drawing.SystemIcons]::Information;"
    "$n.BalloonTipTitle = $env:WT_TITLE;"
    "$n.BalloonTipText = $env:WT_MSG;"
    "$n.Visible = $true;"
    "$n.ShowBalloonTip(8000);"
    "Start-Sleep -Seconds 9;"
    "$n.Dispose();"
)


class Notifier:
    """Sends alerts to the configured channels and always echoes to the transcript.

    ``log`` is the app's transcript writer (a plain callable taking one string);
    it may be ``None`` (e.g. when the notifier is used outside the TUI)."""

    def __init__(self, log: Optional[Callable[[str], None]] = None) -> None:
        self._log = log

    async def notify(self, title: str, message: str,
                     channels: Iterable[str] = ("desktop",)) -> None:
        """Deliver one notification. Best-effort per channel; never raises.
        An empty channels iterable means *nothing* goes to any channel (only the
        transcript log is written). Pass a non-empty list to send."""
        chans = {str(c).strip().lower() for c in channels if str(c).strip()}
        # Always surface it in the chat transcript first.
        if self._log is not None:
            try:
                self._log(f"🔔 {title} — {message}")
            except Exception:
                pass
        for chan in chans:
            try:
                if chan == "desktop":
                    await asyncio.to_thread(self._send_desktop, title, message)
                # Future channels (telegram/email/webapp) slot in here.
            except Exception:
                # A broken channel must not break the others or the caller.
                pass

    # ── channels ─────────────────────────────────────────────────────────
    def _send_desktop(self, title: str, message: str) -> None:
        if _IS_WIN:
            env = dict(os.environ, WT_TITLE=title[:120], WT_MSG=message[:250])
            # Fire-and-forget: the script sleeps ~9s for the balloon; don't block.
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                 "-ExecutionPolicy", "Bypass", "-Command", _WIN_PS],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, **_NO_WINDOW,
            )
            return
        if _IS_MAC:
            script = f'display notification {_osa(message)} with title {_osa(title)}'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
            return
        # Linux / other: notify-send if available (no-op otherwise).
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title[:120], message[:250]],
                           capture_output=True, timeout=10)


def _osa(s: str) -> str:
    """Quote a string as an AppleScript literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
