from __future__ import annotations

import io
import os
import time
import unittest
from contextlib import redirect_stdout

from app.remote_access.slave import (
    TunnelSlave,
    parse_public_url,
    read_status,
    status_path,
    write_status,
)


class TunnelSlaveTests(unittest.TestCase):
    def test_parses_provider_urls_from_child_output(self) -> None:
        self.assertEqual(
            parse_public_url("cloudflare", "INF Visit https://quiet-river-12.trycloudflare.com now"),
            "https://quiet-river-12.trycloudflare.com",
        )
        self.assertEqual(
            parse_public_url("ngrok", 'url=https://example-123.ngrok-free.app'),
            "https://example-123.ngrok-free.app",
        )

    def test_fake_child_output_updates_the_slave_url(self) -> None:
        class FakeChild:
            stdout = iter(["INF connecting\n", "INF https://calm-field.trycloudflare.com\n"])

            @staticmethod
            def wait() -> int:
                return 1

            @staticmethod
            def poll() -> int:
                return 1

        slave = TunnelSlave(port=54320, token="test", provider="cloudflare", quick=True)
        slave.proc = FakeChild()  # type: ignore[assignment]
        snapshots = []
        slave.persist = lambda **_kwargs: snapshots.append(slave.snapshot())  # type: ignore[method-assign]
        with redirect_stdout(io.StringIO()):
            slave._drain_child()
        self.assertEqual(slave.url, "https://calm-field.trycloudflare.com")
        self.assertTrue(any(item["state"] == "running" for item in snapshots))

    def test_named_cloudflare_connection_uses_configured_url(self) -> None:
        class FakeChild:
            stdout = io.BytesIO(
                b"INF Registered tunnel connection connIndex=0 connection=abc\n"
            )

            @staticmethod
            def wait() -> int:
                return 1

            @staticmethod
            def poll() -> int:
                return 1

        slave = TunnelSlave(
            port=54320,
            token="test",
            provider="cloudflare",
            quick=False,
            name="named-tunnel",
            public_url="https://agent.example.com/",
        )
        slave.url = slave.configured_url
        slave.proc = FakeChild()  # type: ignore[assignment]
        snapshots = []
        slave.persist = lambda **_kwargs: snapshots.append(slave.snapshot())  # type: ignore[method-assign]
        with redirect_stdout(io.StringIO()):
            slave._drain_child()
        self.assertEqual(slave.url, "https://agent.example.com")
        self.assertTrue(any(item["state"] == "running" for item in snapshots))

    def test_status_file_round_trip_and_freshness(self) -> None:
        port = 50000 + (os.getpid() % 10000)
        path = status_path(port)
        path.unlink(missing_ok=True)
        try:
            payload = {"state": "running", "url": "https://example.test", "pid": 42, "ts": time.time()}
            write_status(port, payload)
            self.assertEqual(read_status(port), payload)
            payload["ts"] = time.time() - 61
            write_status(port, payload)
            self.assertIsNone(read_status(port))
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
