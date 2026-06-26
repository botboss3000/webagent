"""Resolve concrete colors from the active Textual theme for Rich-Text chrome.

Rich `Text` styles can't read `$css` variables, so anything drawn as Rich Text
(the banner, HUD, status dot, walker, message accents, slider) resolves the
active theme's concrete colors through here. Callers refresh on mount and on the
app's ``theme_changed_signal`` so the Rich-drawn chrome re-colors live alongside
the CSS-driven chrome.
"""

from __future__ import annotations

# Used before any theme is active (and as a safety net). Mirrors the "lime"
# default so the launcher looks right on the very first frame.
_FALLBACK = {
    "primary": "#b6f135",
    "secondary": "#46d4ff",
    "accent": "#46d4ff",
    "tool": "#ff9d2f",
    "success": "#7be06a",
    "error": "#ff5f56",
    "dim": "#56657a",
    "fg": "#cdd6c4",
    "panel": "#0b1019",
    "bg": "#080b12",
}


def chrome_colors(app) -> dict[str, str]:
    """Concrete hex colors for Rich-Text chrome, from the app's active theme."""
    tv = getattr(app, "theme_variables", None) or {}

    def g(key: str, default: str, alt: str | None = None) -> str:
        v = tv.get(key)
        if v:
            return v
        if alt:
            v = tv.get(alt)
            if v:
                return v
        return default

    return {
        "primary": g("primary", _FALLBACK["primary"]),
        "secondary": g("secondary", _FALLBACK["secondary"]),
        "accent": g("accent", _FALLBACK["accent"], alt="bar-accent"),
        "tool": g("tool", _FALLBACK["tool"], alt="warning"),
        "success": g("success", _FALLBACK["success"]),
        "error": g("error", _FALLBACK["error"]),
        "dim": g("dim", _FALLBACK["dim"]),
        "fg": g("foreground", _FALLBACK["fg"]),
        "panel": g("panel", _FALLBACK["panel"]),
        "bg": g("background", _FALLBACK["bg"]),
    }
