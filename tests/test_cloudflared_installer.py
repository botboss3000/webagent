from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.remote_access import installer


class _Download(io.BytesIO):
    headers = {"Content-Length": str(2 * 1024 * 1024)}


class CloudflaredInstallerTests(unittest.TestCase):
    def test_official_asset_mapping(self) -> None:
        linux, linux_name = installer.cloudflared_download_spec("Linux", "x86_64")
        arm, _ = installer.cloudflared_download_spec("linux", "aarch64")
        windows, windows_name = installer.cloudflared_download_spec("Windows", "AMD64")
        self.assertEqual(
            linux,
            "https://github.com/cloudflare/cloudflared/releases/latest/download/"
            "cloudflared-linux-amd64",
        )
        self.assertTrue(arm.endswith("/cloudflared-linux-arm64"))
        self.assertTrue(windows.endswith("/cloudflared-windows-amd64.exe"))
        self.assertEqual(linux_name, "cloudflared")
        self.assertEqual(windows_name, "cloudflared.exe")

    def test_unsupported_platform_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not supported"):
            installer.cloudflared_download_spec("Plan9", "mips")

    def test_install_is_atomic_verified_and_persisted(self) -> None:
        payload = b"x" * (2 * 1024 * 1024)
        version = SimpleNamespace(
            returncode=0, stdout="cloudflared version 2026.8.1", stderr=""
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(installer, "INSTALL_DIR", Path(tmp)),
                patch.object(installer.platform, "system", return_value="Linux"),
                patch.object(installer.platform, "machine", return_value="x86_64"),
                patch.object(installer.urllib.request, "urlopen", return_value=_Download(payload)),
                patch.object(installer.subprocess, "run", return_value=version) as run,
                patch.object(installer.store, "update_config") as update,
            ):
                result = installer.install_cloudflared()
                target = Path(result["path"])
                self.assertTrue(target.is_file())
                self.assertEqual(target.read_bytes(), payload)
                if os.name != "nt":
                    self.assertTrue(target.stat().st_mode & 0o100)
                run.assert_called_once()
                update.assert_called_once_with({
                    "active_method": "cloudflare",
                    "cloudflare": {
                        "bin_path": str(target),
                        "quick": True,
                    },
                })
                self.assertEqual(list(Path(tmp).iterdir()), [target])

    def test_failed_version_probe_does_not_activate_download(self) -> None:
        payload = b"x" * (2 * 1024 * 1024)
        bad = SimpleNamespace(returncode=1, stdout="", stderr="not an executable")
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(installer, "INSTALL_DIR", Path(tmp)),
                patch.object(installer.platform, "system", return_value="Linux"),
                patch.object(installer.platform, "machine", return_value="x86_64"),
                patch.object(installer.urllib.request, "urlopen", return_value=_Download(payload)),
                patch.object(installer.subprocess, "run", return_value=bad),
                patch.object(installer.store, "update_config") as update,
            ):
                with self.assertRaisesRegex(RuntimeError, "version check"):
                    installer.install_cloudflared()
                self.assertEqual(list(Path(tmp).iterdir()), [])
                update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
