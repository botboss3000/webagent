# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for webagent launcher.

Build:  uv run pyinstaller webagent.spec
Output: dist/webagent.exe  (single portable file)
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

# Textual and Rich ship with CSS/template/data files we need at runtime.
textual_datas, textual_binaries, textual_hidden = collect_all("textual")
rich_datas = collect_data_files("rich")

# Our package's CSS file
launcher_datas = [
    ("webagent_launcher/styles.tcss", "webagent_launcher"),
]

datas = textual_datas + rich_datas + launcher_datas
binaries = textual_binaries

hidden_imports = textual_hidden + [
    "webagent_launcher",
    "webagent_launcher.app",
    "webagent_launcher.app_window",
    "webagent_launcher.clipboard",
    "webagent_launcher.ascii_anim",
    "webagent_launcher.stage",
    "webagent_launcher.config",
    "webagent_launcher.bootstrap",
    "webagent_launcher.palette",
    "webagent_launcher.reset",
    "webagent_launcher.screens",
    "webagent_launcher.widgets",
    "webagent_launcher.server",
    "webagent_launcher.chat_screen",
    "webagent_launcher.api_client",
    "webagent_launcher.model_windows",
    "webagent_launcher.model_windows.MODEL_CONTEXT_WINDOWS",
    "webagent_launcher.model_windows.MODEL_CONTEXT_BY_ID",
    # Networking + process control
    "httpx",
    "psutil",
]


a = Analysis(
    ["webagent_launcher/__main__.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # FastAPI/heavy webagent deps aren't needed by the launcher
        "fastapi",
        "uvicorn",
        "openai",
        "supabase",
        "playwright",
        "pandas",
        "matplotlib",
        "scipy",
        "torch",
        "tensorflow",
        # Pillow is build-time only (icon generation). Rich optionally imports
        # PIL for terminal image support, which we don't use.
        "PIL",
        "PIL.Image",
        # Tk isn't used; gets pulled in transitively sometimes.
        "tkinter",
        "_tkinter",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="webagent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX confuses Windows SmartScreen and AV more than it helps
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,             # TUI requires a real console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/webagent.ico",
)
