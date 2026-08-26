"""Glyph set — emoji when the terminal supports it, ASCII otherwise.

Why this exists: a double-clicked PyInstaller console .exe runs in the legacy
Windows console host (conhost), which renders emoji as boxes (□). Windows
Terminal renders emoji fine and sets the ``WT_SESSION`` env var, so we detect
that. Most modern *nix terminals handle emoji too.

The launcher opens as a STANDALONE console window (it does not relaunch itself
into Windows Terminal). So a plain double-click gets clean ASCII glyphs; if you
start the exe from inside Windows Terminal, emoji light up automatically.

Override detection with ``WEBAGENT_EMOJI=1`` (force on) or ``=0`` (force off).
"""

from __future__ import annotations

import os
import sys


def emoji_supported() -> bool:
    override = os.environ.get("WEBAGENT_EMOJI")
    if override == "1":
        return True
    if override == "0":
        return False
    if sys.platform != "win32":
        return True  # modern Linux/macOS terminals render emoji
    # Windows: only Windows Terminal (and VS Code's terminal) render emoji.
    return bool(os.environ.get("WT_SESSION") or os.environ.get("WEBAGENT_IN_WT"))


EMOJI = emoji_supported()


def _g(emoji: str, ascii_: str) -> str:
    return emoji if EMOJI else ascii_


class G:
    """Named glyphs. Each resolves to emoji or an ASCII fallback at import."""

    TOOL = _g("🔧", ">>")
    OK = _g("✅", "ok")
    ERR = _g("❌", "x")
    DOT_LIVE = _g("🟢", "*")
    DOT_WARN = _g("🟡", "*")
    DOT_DEAD = _g("🔴", "*")
    BULLET = _g("·", "-")
    SEP = _g("·", "-")
    THINKING = _g("✨", "*")
    WARN = _g("⚠", "!")
    NEW = _g("➕", "+")
    AGENT = _g("🤖", "[agent]")
    ADMIN = _g("🛠", "[admin]")
    PLUG = _g("🔌", "[intg]")
    IMAGE = _g("🖼", "[img]")
    USER = _g("🧑", "you")
    BOT = _g("🤖", "")
    CROSS = _g("✕", "x")
