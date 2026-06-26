"""The 23 reference color schemes as live Textual themes.

Ported from the `WEBAGENT TUI` reference (`themes.js`). Each reference theme is a
flat token set; we map those tokens onto Textual's native theme slots so that
**picking a theme re-skins the whole launcher live** (no restart) and adds the
`-light-mode` / `-dark-mode` body classes automatically.

Reference token  -> Textual slot
    primary       -> primary
    secondary     -> secondary
    tool          -> warning   (Textual has no "tool" slot; also exposed as $tool)
    error         -> error
    success       -> success
    barAccent     -> accent
    fg            -> foreground
    bg            -> background
    bg2           -> surface
    panel         -> panel

Tokens Textual has no native slot for (dim, tool, barBg, barFg, barAccent,
borderDim, sel, border) are passed through each theme's ``variables`` dict so the
stylesheet can reference them as ``$dim``, ``$tool``, ``$bar-bg``, ``$bar-fg``,
``$bar-accent``, ``$border-dim``, ``$sel``. ``LauncherApp.get_theme_variable_defaults``
declares fallbacks for all of them so the CSS always parses (even before a custom
theme is active — required by Textual, see App.get_theme_variable_defaults docstring).

Usage:
    from .themes import build_themes, THEME_ORDER, DEFAULT_THEME
    for theme in build_themes():
        app.register_theme(theme)
    app.theme = cfg.theme_name if cfg.theme_name in THEME_ORDER else DEFAULT_THEME
"""

from __future__ import annotations

from textual.theme import Theme

DEFAULT_THEME = "lime"

# Themes whose background is light — flagged so Textual flips its auto-contrast
# text and sets the `-light-mode` body class.
LIGHT_THEMES = {"paper", "latte", "solarlight", "rosedawn", "mint", "sorbet", "skylight"}

# Order used by cycle / shuffle and the theme menu (mirrors themes.js THEME_ORDER).
THEME_ORDER = [
    "lime", "amber", "cyberpunk", "gruvbox", "dracula", "solarized", "nord",
    "matrix", "synthwave", "tokyonight", "catppuccin", "monokai", "onedark",
    "ayu", "ocean", "hotdog", "paper", "latte", "solarlight", "rosedawn",
    "mint", "sorbet", "skylight",
]

# id -> flat reference token set. Keys mirror themes.js exactly.
# (amber.bg2 in the source was a malformed 8-digit value "#15100604"; corrected
#  here to a sensible near-black brown so the surface isn't near-transparent.)
REF_THEMES: dict[str, dict[str, str]] = {
    "lime": dict(label="WebAgent Lime", tags="default",
        bg="#080b12", bg2="#0c111c", panel="#0b1019", fg="#cdd6c4", dim="#56657a",
        primary="#b6f135", secondary="#46d4ff", tool="#ff9d2f", success="#7be06a", error="#ff5f56",
        barBg="#1717d4", barFg="#b6f135", barAccent="#46d4ff", border="#b6f135", borderDim="#1d2a3a", sel="#16351f"),
    "amber": dict(label="Amber Phosphor", tags="retro CRT",
        bg="#0c0a05", bg2="#150f06", panel="#120d05", fg="#ffb000", dim="#7a5512",
        primary="#ffb000", secondary="#ffd98a", tool="#ff8a00", success="#ffd000", error="#ff5b3a",
        barBg="#3a2600", barFg="#ffd98a", barAccent="#ffb000", border="#b87800", borderDim="#3a2a08", sel="#3a2a00"),
    "cyberpunk": dict(label="Cyberpunk", tags="magenta / cyan",
        bg="#0d0221", bg2="#150733", panel="#11052b", fg="#d7d0e8", dim="#6a4f8f",
        primary="#ff2e88", secondary="#00e5ff", tool="#ffd319", success="#05ffa1", error="#ff2e63",
        barBg="#1a0a3a", barFg="#00e5ff", barAccent="#ff2e88", border="#ff2e88", borderDim="#2a1147", sel="#2a0f3f"),
    "gruvbox": dict(label="Gruvbox", tags="warm dark",
        bg="#1d2021", bg2="#282828", panel="#242424", fg="#ebdbb2", dim="#928374",
        primary="#b8bb26", secondary="#83a598", tool="#fe8019", success="#b8bb26", error="#fb4934",
        barBg="#3c3836", barFg="#ebdbb2", barAccent="#fabd2f", border="#a89984", borderDim="#3c3836", sel="#3c3836"),
    "dracula": dict(label="Dracula", tags="purple night",
        bg="#21222c", bg2="#282a36", panel="#262837", fg="#f8f8f2", dim="#6272a4",
        primary="#ff79c6", secondary="#8be9fd", tool="#ffb86c", success="#50fa7b", error="#ff5555",
        barBg="#44475a", barFg="#f8f8f2", barAccent="#bd93f9", border="#6272a4", borderDim="#3a3c4e", sel="#3a3c52"),
    "solarized": dict(label="Solarized Dark", tags="muted",
        bg="#002b36", bg2="#073642", panel="#04303b", fg="#93a1a1", dim="#586e75",
        primary="#859900", secondary="#268bd2", tool="#cb4b16", success="#859900", error="#dc322f",
        barBg="#073642", barFg="#93a1a1", barAccent="#2aa198", border="#586e75", borderDim="#0a3a45", sel="#0a3f4a"),
    "nord": dict(label="Nord", tags="arctic",
        bg="#2e3440", bg2="#3b4252", panel="#353c4a", fg="#d8dee9", dim="#4c566a",
        primary="#88c0d0", secondary="#81a1c1", tool="#d08770", success="#a3be8c", error="#bf616a",
        barBg="#3b4252", barFg="#eceff4", barAccent="#8fbcbb", border="#4c566a", borderDim="#434c5e", sel="#434c5e"),
    "matrix": dict(label="Matrix", tags="phosphor green",
        bg="#000700", bg2="#021a02", panel="#021502", fg="#00cc33", dim="#0a5a1a",
        primary="#00ff41", secondary="#39ff14", tool="#aaff00", success="#00ff41", error="#ff3333",
        barBg="#001a00", barFg="#00ff41", barAccent="#39ff14", border="#008f24", borderDim="#013a0e", sel="#012f0c"),
    "synthwave": dict(label="Synthwave", tags="neon sunset",
        bg="#1a1033", bg2="#241447", panel="#1f1140", fg="#f7e8ff", dim="#7b5fa8",
        primary="#ff5fd2", secondary="#2de2e6", tool="#ffb84d", success="#72f1b8", error="#fe4450",
        barBg="#2a0e54", barFg="#2de2e6", barAccent="#ff5fd2", border="#ff5fd2", borderDim="#3a2160", sel="#3a1a66"),
    "tokyonight": dict(label="Tokyo Night", tags="cool blue",
        bg="#1a1b26", bg2="#24283b", panel="#1f2233", fg="#c0caf5", dim="#565f89",
        primary="#7aa2f7", secondary="#bb9af7", tool="#e0af68", success="#9ece6a", error="#f7768e",
        barBg="#24283b", barFg="#c0caf5", barAccent="#7dcfff", border="#414868", borderDim="#2c3047", sel="#2e3450"),
    "catppuccin": dict(label="Catppuccin", tags="soft mocha",
        bg="#1e1e2e", bg2="#313244", panel="#262636", fg="#cdd6f4", dim="#6c7086",
        primary="#cba6f7", secondary="#89dceb", tool="#fab387", success="#a6e3a1", error="#f38ba8",
        barBg="#313244", barFg="#cdd6f4", barAccent="#f5c2e7", border="#45475a", borderDim="#2a2a3c", sel="#313244"),
    "monokai": dict(label="Monokai", tags="classic editor",
        bg="#272822", bg2="#3e3d32", panel="#2f2f28", fg="#f8f8f2", dim="#75715e",
        primary="#a6e22e", secondary="#66d9ef", tool="#fd971f", success="#a6e22e", error="#f92672",
        barBg="#3e3d32", barFg="#f8f8f2", barAccent="#fd971f", border="#75715e", borderDim="#3a3a30", sel="#49483e"),
    "onedark": dict(label="One Dark", tags="atom",
        bg="#282c34", bg2="#21252b", panel="#2c313a", fg="#abb2bf", dim="#5c6370",
        primary="#98c379", secondary="#61afef", tool="#e5c07b", success="#98c379", error="#e06c75",
        barBg="#21252b", barFg="#abb2bf", barAccent="#c678dd", border="#3e4451", borderDim="#2c313a", sel="#3a3f4b"),
    "ayu": dict(label="Ayu Mirage", tags="warm dark",
        bg="#1f2430", bg2="#232834", panel="#212733", fg="#cccac2", dim="#707a8c",
        primary="#ffcc66", secondary="#73d0ff", tool="#ffa759", success="#87d96c", error="#ff6666",
        barBg="#232834", barFg="#cccac2", barAccent="#5ccfe6", border="#33415e", borderDim="#2a3140", sel="#2d3650"),
    "ocean": dict(label="Deep Ocean", tags="abyssal",
        bg="#07131f", bg2="#0c2233", panel="#0a1c2b", fg="#cfe8f5", dim="#4a6b80",
        primary="#2bd4d9", secondary="#4aa3ff", tool="#ffb454", success="#5fd38a", error="#ff6b6b",
        barBg="#0c2233", barFg="#cfe8f5", barAccent="#2bd4d9", border="#16465c", borderDim="#10303f", sel="#103040"),
    "hotdog": dict(label="Hotdog Stand", tags="chaotic",
        bg="#d60000", bg2="#b30000", panel="#c20000", fg="#ffff00", dim="#ffb380",
        primary="#ffff00", secondary="#ffffff", tool="#ffff00", success="#ffff66", error="#ffffff",
        barBg="#ffff00", barFg="#d60000", barAccent="#000000", border="#ffff00", borderDim="#8a0000", sel="#8a0000"),
    "paper": dict(label="Paper White", tags="light mono",
        bg="#f4f1e9", bg2="#eae6da", panel="#efece2", fg="#1c1b18", dim="#9b958a",
        primary="#1c1b18", secondary="#3a6b8a", tool="#9a5b18", success="#2f6b2f", error="#a3302a",
        barBg="#1c1b18", barFg="#f4f1e9", barAccent="#9a5b18", border="#1c1b18", borderDim="#cfc8b8", sel="#e2dcca"),
    "latte": dict(label="Latte", tags="light · soft",
        bg="#eff1f5", bg2="#e6e9ef", panel="#eaedf3", fg="#4c4f69", dim="#8c8fa1",
        primary="#8839ef", secondary="#1e66f5", tool="#fe640b", success="#40a02b", error="#d20f39",
        barBg="#ccd0da", barFg="#4c4f69", barAccent="#ea76cb", border="#bcc0cc", borderDim="#dce0e8", sel="#dce0e8"),
    "solarlight": dict(label="Solarized Light", tags="light · sepia",
        bg="#fdf6e3", bg2="#eee8d5", panel="#f5efdc", fg="#586e75", dim="#93a1a1",
        primary="#859900", secondary="#268bd2", tool="#cb4b16", success="#2aa198", error="#dc322f",
        barBg="#eee8d5", barFg="#586e75", barAccent="#268bd2", border="#bfb98f", borderDim="#e3dcc4", sel="#eee8d5"),
    "rosedawn": dict(label="Rosé Dawn", tags="light · blush",
        bg="#faf4ed", bg2="#fffaf3", panel="#f4ede4", fg="#575279", dim="#9893a5",
        primary="#907aa9", secondary="#56949f", tool="#ea9d34", success="#286983", error="#b4637a",
        barBg="#f2e9e1", barFg="#575279", barAccent="#d7827e", border="#cecacd", borderDim="#ece1d7", sel="#f2e9e1"),
    "mint": dict(label="Mint Cream", tags="light · fresh",
        bg="#f3faf6", bg2="#e6f2ec", panel="#ecf6f1", fg="#234038", dim="#7fa394",
        primary="#0f9d6b", secondary="#2b8aa6", tool="#d98324", success="#0f9d6b", error="#d24b4b",
        barBg="#d7ebe2", barFg="#234038", barAccent="#0f9d6b", border="#b9d6c9", borderDim="#d7ebe2", sel="#def0e8"),
    "sorbet": dict(label="Sorbet", tags="light · candy",
        bg="#fff6fb", bg2="#ffe9f4", panel="#fff0f8", fg="#5b3a55", dim="#c08fb0",
        primary="#ff5fa2", secondary="#6c8cff", tool="#ff9b2f", success="#2fbf8f", error="#ff5470",
        barBg="#ffe0ef", barFg="#5b3a55", barAccent="#8b6cff", border="#f3c6dd", borderDim="#ffd9ec", sel="#ffe0ef"),
    "skylight": dict(label="Daylight", tags="light · sky",
        bg="#f0f6ff", bg2="#e2edff", panel="#ecf3ff", fg="#1c3a5e", dim="#7d97b8",
        primary="#2f6fed", secondary="#00a3c4", tool="#e8821a", success="#2b9d6b", error="#e0445b",
        barBg="#dbe8ff", barFg="#1c3a5e", barAccent="#2f6fed", border="#c2d4ee", borderDim="#dbe8ff", sel="#e2edff"),
}

# Custom-variable fallbacks. Declared by the app so the stylesheet parses even
# before a custom theme is active (on the built-in default theme these tokens
# don't exist). Values are the "lime" defaults.
CUSTOM_VAR_DEFAULTS: dict[str, str] = {
    "dim": "#56657a",
    "tool": "#ff9d2f",
    "bar-bg": "#1717d4",
    "bar-fg": "#b6f135",
    "bar-accent": "#46d4ff",
    "border-dim": "#1d2a3a",
    "sel": "#16351f",
}

# Display metadata for the theme menu (label + tags shown beside each swatch).
THEME_LABELS: dict[str, str] = {k: v["label"] for k, v in REF_THEMES.items()}
THEME_TAGS: dict[str, str] = {k: v["tags"] for k, v in REF_THEMES.items()}


def _to_theme(theme_id: str, ref: dict[str, str]) -> Theme:
    """Build one Textual Theme from a flat reference token set."""
    return Theme(
        name=theme_id,
        primary=ref["primary"],
        secondary=ref["secondary"],
        warning=ref["tool"],
        error=ref["error"],
        success=ref["success"],
        accent=ref["barAccent"],
        foreground=ref["fg"],
        background=ref["bg"],
        surface=ref["bg2"],
        panel=ref["panel"],
        dark=theme_id not in LIGHT_THEMES,
        variables={
            # custom tokens used directly by styles.tcss / Rich-text chrome
            "dim": ref["dim"],
            "tool": ref["tool"],
            "bar-bg": ref["barBg"],
            "bar-fg": ref["barFg"],
            "bar-accent": ref["barAccent"],
            "border-dim": ref["borderDim"],
            "sel": ref["sel"],
            # make Textual's own widgets adopt the same look
            "border": ref["border"],
            "border-blurred": ref["borderDim"],
            "input-selection-background": ref["sel"],
            "input-cursor-background": ref["primary"],
            "input-cursor-foreground": ref["bg"],
            "block-cursor-background": ref["primary"],
            "block-cursor-foreground": ref["bg"],
            "block-cursor-text-style": "none",
            "button-color-foreground": ref["bg"],
            "footer-key-foreground": ref["barAccent"],
        },
    )


def build_themes() -> list[Theme]:
    """All 23 themes as Textual Theme objects, in THEME_ORDER."""
    return [_to_theme(tid, REF_THEMES[tid]) for tid in THEME_ORDER]


def theme_token(theme_id: str, token: str, default: str = "#ffffff") -> str:
    """Look up a raw reference token (e.g. 'primary', 'dim', 'tool') for a theme
    id, straight from REF_THEMES. Used to tint things that aren't styled by CSS
    (e.g. the animation palette). Returns a concrete hex string."""
    return REF_THEMES.get(theme_id, {}).get(token, default)
