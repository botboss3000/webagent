"""Read the OS clipboard (for Ctrl+V in input fields).

Textual's ``Input.action_paste`` pastes ``app.clipboard`` — its *internal*
clipboard, which is empty unless the app itself copied something. So Ctrl+V on a
token/key pasted from another program (a browser, a password manager) inserts
nothing. This reads the real system clipboard instead.

Self-contained: Windows uses the Win32 API via ``ctypes`` (no extra deps, works
in the frozen .exe); macOS/Linux shell out to the usual clipboard utilities.
Every path is best-effort — any failure returns ``""`` so paste just no-ops.
"""

from __future__ import annotations

import subprocess
import sys


def _read_windows() -> str:
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]

    if not user32.OpenClipboard(None):
        return ""
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.c_wchar_p(ptr).value or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _write_windows(text: str) -> bool:
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    data = text.encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        # On success the system owns the memory — do not free it.
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


def _write_cmd(cmd: list[str], text: str) -> bool:
    try:
        proc = subprocess.run(cmd, input=text.encode("utf-8"),
                              capture_output=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def write_clipboard(text: str) -> bool:
    """Best-effort write of ``text`` to the OS clipboard. Returns False if no
    clipboard mechanism is available (the caller can fall back to OSC 52)."""
    try:
        if sys.platform == "win32":
            return _write_windows(text)
        if sys.platform == "darwin":
            return _write_cmd(["pbcopy"], text)
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                    ["xsel", "--clipboard", "--input"]):
            if _write_cmd(cmd, text):
                return True
        return False
    except Exception:
        return False


def _read_cmd(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", errors="replace")


def read_clipboard() -> str:
    """Best-effort read of the OS clipboard. Returns ``""`` if unavailable."""
    try:
        if sys.platform == "win32":
            return _read_windows()
        if sys.platform == "darwin":
            return _read_cmd(["pbpaste"])
        # Linux/other: try Wayland then X11 utilities, in order.
        for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
                    ["xsel", "--clipboard", "--output"]):
            text = _read_cmd(cmd)
            if text:
                return text
        return ""
    except Exception:
        return ""
