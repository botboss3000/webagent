"""Read from / write to the *real* OS clipboard.

Textual's built-in Ctrl+V pastes from its own **in-app** clipboard (populated
only by an in-app copy), and its Ctrl+C reaches the system clipboard only via an
OSC-52 escape — which the plain Windows console the .exe runs in silently
ignores. The upshot: out of the box you cannot paste a path you copied from
Explorer/Notepad into a launcher field, which is exactly the install-folder box.

This module talks to the actual OS clipboard so the launcher can wire Ctrl+V to
a genuine paste (and mirror in-app copies back out). Windows uses the Win32
clipboard API directly via ``ctypes`` — no extra dependency, works in a bare
console and inside the PyInstaller one-file exe. macOS/Linux shell out to the
usual tools so development on those platforms keeps working too.

Everything here is best-effort: any failure (clipboard held by another app,
missing CLI tool, empty clipboard) returns an empty string / ``False`` rather
than raising, so a paste that can't find text simply does nothing.
"""

from __future__ import annotations

import subprocess
import sys

__all__ = ["read_text", "write_text"]

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


# ── Windows (the primary target — the .exe opens in a bare console) ───────
def _win_read() -> str:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Explicit prototypes are essential on 64-bit Windows: without them ctypes
    # assumes 32-bit ints and truncates the 64-bit HANDLE/pointer values.
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _win_write(text: str) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    data = text.encode("utf-16-le") + b"\x00\x00"  # NUL-terminated wide string
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
            # Ownership wasn't transferred to the clipboard — free our buffer.
            kernel32.GlobalFree(handle)
            return False
        return True  # on success the OS owns the memory; we must not free it
    finally:
        user32.CloseClipboard()


# ── macOS / Linux (development parity) ────────────────────────────────────
def _run(cmd: list[str], stdin: str | None = None) -> "subprocess.CompletedProcess[bytes] | None":
    try:
        return subprocess.run(
            cmd,
            input=stdin.encode("utf-8") if stdin is not None else None,
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _posix_read() -> str:
    if sys.platform == "darwin":
        cmds = [["pbpaste"]]
    else:
        cmds = [
            ["wl-paste", "--no-newline"],
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "-b"],
        ]
    for cmd in cmds:
        res = _run(cmd)
        if res is not None and res.returncode == 0:
            return res.stdout.decode("utf-8", "replace")
    return ""


def _posix_write(text: str) -> bool:
    if sys.platform == "darwin":
        cmds = [["pbcopy"]]
    else:
        cmds = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "-b", "-i"],
        ]
    for cmd in cmds:
        res = _run(cmd, stdin=text)
        if res is not None and res.returncode == 0:
            return True
    return False


# ── public API ─────────────────────────────────────────────────────────────
def read_text() -> str:
    """Return the OS clipboard's text (empty string if unavailable/non-text)."""
    try:
        if sys.platform == "win32":
            return _win_read()
        return _posix_read()
    except Exception:
        return ""


def write_text(text: str) -> bool:
    """Write ``text`` to the OS clipboard. Returns True on success."""
    try:
        if sys.platform == "win32":
            return _win_write(text)
        return _posix_write(text)
    except Exception:
        return False
