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
from typing import Optional, Tuple


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


def _read_windows_image() -> Optional[Tuple[bytes, str]]:
    """Read an image off the Windows clipboard with no third-party deps.

    Prefers a registered ``PNG`` clipboard format (what Snipping Tool, browsers
    and most screenshot tools publish) — those bytes are already a PNG. Falls
    back to ``CF_DIB`` (a device-independent bitmap), which we wrap in a BMP file
    header so it's a valid standalone image.
    """
    import ctypes
    from ctypes import wintypes

    CF_DIB = 8
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
    kernel32.GlobalSize.restype = ctypes.c_size_t

    def _grab(fmt: int) -> bytes:
        if not user32.IsClipboardFormatAvailable(fmt):
            return b""
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return b""
        size = int(kernel32.GlobalSize(handle))
        ptr = kernel32.GlobalLock(handle)
        if not ptr or size <= 0:
            return b""
        try:
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(handle)

    cf_png = user32.RegisterClipboardFormatW("PNG")
    if not user32.OpenClipboard(None):
        return None
    try:
        if cf_png:
            png = _grab(cf_png)
            if png:
                return png, "image/png"
        dib = _grab(CF_DIB)
        if dib and len(dib) >= 40:
            # Build a BMP: 14-byte BITMAPFILEHEADER + the DIB (header + pixels).
            header_size = int.from_bytes(dib[0:4], "little")
            bit_count = int.from_bytes(dib[14:16], "little")
            compression = int.from_bytes(dib[16:20], "little")
            clr_used = int.from_bytes(dib[32:36], "little")
            palette = 0
            if bit_count <= 8:
                palette = (clr_used or (1 << bit_count)) * 4
            if compression == 3 and header_size == 40:  # BI_BITFIELDS adds 3 masks
                palette += 12
            pixel_offset = 14 + header_size + palette
            filesize = 14 + len(dib)
            bmp_header = (b"BM" + filesize.to_bytes(4, "little") + b"\x00\x00\x00\x00"
                          + pixel_offset.to_bytes(4, "little"))
            return bmp_header + dib, "image/bmp"
        return None
    finally:
        user32.CloseClipboard()


def _read_macos_image() -> Optional[Tuple[bytes, str]]:
    """Ask AppleScript for the clipboard as PNG and write it to a temp file."""
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    script = (
        f'set p to "{path}"\n'
        'try\n'
        '  set png to (the clipboard as «class PNGf»)\n'
        '  set fh to open for access (POSIX file p) with write permission\n'
        '  set eof fh to 0\n'
        '  write png to fh\n'
        '  close access fh\n'
        'on error\n'
        '  return "none"\n'
        'end try'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=4)
        data = open(path, "rb").read()
    except (OSError, subprocess.SubprocessError):
        data = b""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return (data, "image/png") if data else None


def _read_linux_image() -> Optional[Tuple[bytes, str]]:
    for cmd in (["wl-paste", "--type", "image/png"],
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]):
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout:
            return out.stdout, "image/png"
    return None


def read_clipboard_image() -> Optional[Tuple[bytes, str]]:
    """Best-effort read of an *image* on the OS clipboard. Returns
    ``(bytes, mime)`` or ``None`` if there's no image (or no way to read one).

    Tries Pillow first when it happens to be installed (most robust, cross
    platform), then falls back to dependency-free native paths per OS so the
    frozen .exe / a bare Termux install still works.
    """
    try:
        from PIL import ImageGrab  # type: ignore
        import io

        grabbed = ImageGrab.grabclipboard()
        # A list means file paths were copied, not an image — let the caller
        # handle those as path attachments instead.
        if grabbed is not None and not isinstance(grabbed, list) and hasattr(grabbed, "save"):
            buf = io.BytesIO()
            img = grabbed
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            img.save(buf, "PNG")
            return buf.getvalue(), "image/png"
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            return _read_windows_image()
        if sys.platform == "darwin":
            return _read_macos_image()
        return _read_linux_image()
    except Exception:
        return None


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
