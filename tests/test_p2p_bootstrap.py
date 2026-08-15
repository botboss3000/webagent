import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.p2p import bootstrap
from app.p2p.manifest import classify_file
from app.p2p.vault_policy import should_sync_row


class P2PBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def test_recurring_manifest_never_replaces_app_db(self):
        self.assertEqual(classify_file("data/db/app.db"), "never")
        self.assertEqual(classify_file("data/db/app_secrets.db"), "row")
        self.assertEqual(classify_file("data/config/app-settings.json"), "full")
        self.assertEqual(classify_file("data/config/db_connection.json"), "never")

    def test_portable_app_table_allowlist_excludes_runtime_identity(self):
        excluded = {
            "instances",
            "device_presence",
            "device_jobs",
            "background_leader",
            "storage_layout",
            "storage_migrations",
            "tenant_key_meta",
        }
        self.assertTrue(excluded.isdisjoint(bootstrap.PORTABLE_APP_TABLES))
        self.assertIn("user_accounts", bootstrap.PORTABLE_APP_TABLES)

    async def test_build_payload_honours_each_selection(self):
        vault = AsyncMock(return_value=([{"service": "llm"}], ["one warning"]))
        with patch.object(bootstrap, "_export_vault_rows", vault), \
                patch.object(bootstrap, "_export_app_rows", return_value={"user_accounts": [{}]}), \
                patch.object(bootstrap, "_portable_configs", return_value={"app-settings.json": {}}):
            payload = await bootstrap.build_payload({
                "app_db": False,
                "app_configs": True,
                "app_secrets": True,
            })

        self.assertEqual(payload["app_rows"], {})
        self.assertEqual(payload["configs"], {"app-settings.json": {}})
        self.assertEqual(payload["vault_rows"], [{"service": "llm"}])
        self.assertEqual(payload["warnings"], ["one warning"])

    async def test_apply_reencrypts_plain_secret_through_db_facade(self):
        fake_db = unittest.mock.Mock()
        fake_db.auth_element_set = AsyncMock()
        payload = {
            "version": 1,
            "app_rows": {},
            "configs": {},
            "vault_rows": [{
                "user_id": "admin",
                "service": "llm",
                "label": "default",
                "config": {"provider": "openai"},
                "secret": "plaintext-inside-encrypted-transport",
            }],
        }
        with patch.object(bootstrap, "_apply_app_rows", return_value=0), \
                patch.object(bootstrap, "_apply_configs", return_value=0), \
                patch("app.db.get_db", return_value=fake_db):
            result = await bootstrap.apply_payload(payload)

        fake_db.auth_element_set.assert_awaited_once_with(
            user_id="admin",
            service="llm",
            label="default",
            config={"provider": "openai"},
            secret_ref="plaintext-inside-encrypted-transport",
        )
        self.assertEqual(result["secret_rows"], 1)

    def test_peer_store_persists_bootstrap_scope_and_transport_key(self):
        from app.p2p import store

        with tempfile.TemporaryDirectory() as tmp, patch.object(store, "_PEERS_DIR", Path(tmp)):
            peer = store.add_peer(
                "https://new.example",
                "new",
                "ab" * 32,
                remote_instance_id="target-id",
                x25519_public_key="transport-key",
                sync_options={"app_secrets": True, "app_db": False},
                bootstrap_only=True,
                push_replica=True,
            )
            loaded = store.get_peer(peer["id"])

        self.assertEqual(loaded["x25519_public_key"], "transport-key")
        self.assertEqual(loaded["sync_options"], {"app_secrets": True, "app_db": False})
        self.assertTrue(loaded["bootstrap_only"])
        self.assertTrue(loaded["push_replica"])

    def test_github_deployment_token_stays_local(self):
        self.assertFalse(should_sync_row("admin", "deploy_github_token", "default"))
        self.assertTrue(should_sync_row("admin", "llm", "default"))

    async def test_pair_stops_with_release_guidance_when_target_has_no_p2p_routes(self):
        response = Mock(status_code=404)
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client

        with patch("httpx.AsyncClient", return_value=context):
            with self.assertRaisesRegex(RuntimeError, "Release the scoped P2P bootstrap routes"):
                await bootstrap.pair_and_push(
                    target_url="https://new.example",
                    source_url="https://source.example",
                    options={"app_db": True},
                )

        client.post.assert_not_awaited()

    async def test_pair_rejects_older_p2p_service_before_handshake(self):
        response = Mock(status_code=200)
        response.json.return_value = {"instance_id": "older-target"}
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client

        with patch("httpx.AsyncClient", return_value=context):
            with self.assertRaisesRegex(RuntimeError, "older P2P service"):
                await bootstrap.pair_and_push(
                    target_url="https://new.example",
                    source_url="https://source.example",
                    options={"app_db": True},
                )

        client.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
