from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "automation-mail-worker"
CLIENT = ROOT / "apps" / "token-pool-client" / "src"
ADMIN = ROOT / "apps" / "token-pool-admin" / "src"
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(CLIENT))
sys.path.insert(0, str(ADMIN))

from copilot_service.registry import CopilotRegistry  # noqa: E402
from copilot_service.admin_ping import CopilotPingManager  # noqa: E402
from copilot_service.job_status import JobStatusStore  # noqa: E402
from copilot_service.session_bundle import BundleValidationError, install_bundle  # noqa: E402
from copilot_service.token_api import (  # noqa: E402
    TokenApiServer,
    _without_registration_race_duplicates,
)
from token_pool_client.upload import ClientConfig, client_preflight  # noqa: E402
from token_pool_client.upload import server_status as client_server_status  # noqa: E402
from token_pool_client.control import (  # noqa: E402
    complete_admin_command,
    poll_admin_commands,
    send_client_heartbeat,
)
from token_pool_admin import api as admin_api  # noqa: E402
from token_pool_admin.api import AdminApiError  # noqa: E402
from token_pool_admin.api import create_commands as admin_create_commands  # noqa: E402
from token_pool_admin.api import forget_clients as admin_forget_clients  # noqa: E402
from token_pool_admin.api import snapshot as admin_snapshot  # noqa: E402
from token_pool_admin.storage import AdminConfig  # noqa: E402


def fake_jwt(claims: dict[str, object]) -> str:
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


def bundle_bytes(
    *,
    unsafe_name: str | None = None,
    object_id: str = "22222222-2222-2222-2222-222222222222",
) -> bytes:
    token = fake_jwt(
        {
            "tid": "11111111-1111-1111-1111-111111111111",
            "oid": object_id,
            "preferred_username": "tester@example.com",
            "exp": int(time.time()) + 3600,
        }
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "client_version": "test",
                    "authorization_expires_at": time.time() + 23 * 60 * 60,
                }
            ),
        )
        archive.writestr(
            "private_websocket_raw_frames_test.json",
            json.dumps([{"url": f"https://substrate.office.com/m365Copilot/Chathub/x?access_token={token}"}]),
        )
        archive.writestr(
            "private_replay_templates_test.json",
            json.dumps([{"url": "https://substrate.office.com/m365Copilot/UploadFile", "headers": {}}]),
        )
        archive.writestr(
            "private_playwright_cookies_test.json",
            json.dumps([{"name": "cookie", "value": "value", "domain": ".office.com"}]),
        )
        archive.writestr(
            "private_edge_msal_refresh_token_test.json",
            json.dumps({"refresh_token": "test-refresh-token", "access_token": token}),
        )
        if unsafe_name:
            archive.writestr(unsafe_name, "bad")
    return output.getvalue()


class RegistryTests(unittest.TestCase):
    def test_admin_hides_only_provable_first_run_client_id_alias(self) -> None:
        shared = {
            "first_seen_at": 1000.0,
            "app_version": "1.0.16",
            "account_ids": ["account-test"],
            "remote_address": "127.0.0.1",
        }
        visible, suppressed = _without_registration_race_duplicates(
            [
                {**shared, "client_id": "winner", "last_seen_at": 1060.0},
                {**shared, "client_id": "alias", "last_seen_at": 1000.0},
                {
                    **shared,
                    "client_id": "real-second-computer",
                    "first_seen_at": 1001.0,
                    "last_seen_at": 1001.0,
                },
            ]
        )
        self.assertEqual(suppressed, 1)
        self.assertEqual(
            {client["client_id"] for client in visible},
            {"winner", "real-second-computer"},
        )

    def test_admin_api_retries_directly_when_configured_proxy_is_dead(self) -> None:
        config = AdminConfig("http://example.invalid", "a" * 32, Path("unused.pem"))
        proxy_opener = MagicMock()
        proxy_opener.open.side_effect = urllib.error.URLError("proxy unavailable")
        response = MagicMock()
        response.status = 200
        response.headers.get.return_value = "application/vnd.fmbsm.admin+aesgcm"
        response.read.return_value = b"encrypted"
        direct_context = MagicMock()
        direct_context.__enter__.return_value = response
        direct_opener = MagicMock()
        direct_opener.open.return_value = direct_context
        admin_api._PROXY_ROUTE_CACHE.clear()

        with patch(
            "token_pool_admin.api._opener",
            side_effect=[proxy_opener, direct_opener],
        ) as opener, patch(
            "token_pool_admin.api._decode_response",
            return_value={"ok": True},
        ):
            result = admin_snapshot(config)

        self.assertTrue(result["ok"])
        self.assertTrue(opener.call_args_list[0].kwargs["use_proxy"])
        self.assertFalse(opener.call_args_list[1].kwargs["use_proxy"])
        self.assertFalse(admin_api._PROXY_ROUTE_CACHE[config.endpoint])

    def test_turn_usage_distinguishes_hour_day_and_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = CopilotRegistry(Path(temporary))
            installed = install_bundle(bundle_bytes(), registry, remote_address="127.0.0.1")
            base = time.time()
            for dispatched_at in (base - 2 * 60 * 60, base - 30 * 60):
                with patch("copilot_service.registry.time.time", return_value=dispatched_at):
                    allowed = registry.reserve_turn(
                        installed.account.account_id,
                        turn_limit=100,
                        window_seconds=3600,
                        cooldown_seconds=3600,
                    )
                self.assertTrue(allowed[0])
            usage = registry.turn_usage(now=base)[installed.account.account_id]
            self.assertEqual(usage["turns_last_hour"], 1)
            self.assertEqual(usage["turns_last_24_hours"], 2)
            self.assertEqual(registry.get_account(installed.account.account_id).total_turns, 2)

    def test_copilot_health_tests_cannot_be_accidentally_queued_twice(self) -> None:
        registry = MagicMock()
        registry.list_accounts.return_value = [MagicMock(account_id="account-test")]
        status_store = MagicMock()
        status_store.update.return_value = {"job_id": "test", "stage": "queued"}
        manager = CopilotPingManager(registry, status_store)
        with patch("copilot_service.admin_ping.threading.Thread.start"):
            manager.start(["account-test"])
            with self.assertRaisesRegex(RuntimeError, "already running"):
                manager.start(["account-test"])

    def test_expired_admin_command_is_never_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = CopilotRegistry(Path(temporary) / "registry")
            client_id = "c" * 32
            registry.update_client_presence(
                client_id=client_id,
                observed_at=1000,
                event="heartbeat",
                scheduled_slot=None,
                app_version="test",
                account_ids=[],
                remote_address="127.0.0.1",
                status={"busy": False},
            )
            with patch("copilot_service.registry.time.time", return_value=1000):
                command = registry.create_client_command(
                    client_id=client_id,
                    command="force_update",
                    payload={},
                    expires_in_seconds=60,
                )
            with patch("copilot_service.registry.time.time", return_value=1061):
                self.assertIsNone(registry.poll_client_command(client_id))
            self.assertEqual(
                registry.get_client_command(command["command_id"])["status"],
                "expired",
            )

    def test_admin_command_round_trip_is_separately_authenticated_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = CopilotRegistry(root / "registry")
            expired = install_bundle(bundle_bytes(), registry, remote_address="127.0.0.1")
            registry.update_token_state(
                expired.account.account_id,
                access_expires_at=time.time() - 60,
                refresh_expires_at=time.time() + 3600,
                error="token_refresh_failed: interactive sign-in required",
            )
            # The general scheduler remains optimistic until it tries the refresh,
            # while the admin dashboard must reflect the recorded failure immediately.
            self.assertEqual(registry.status()["available_account_count"], 1)
            status_store = JobStatusStore(root / "job-status")
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            server = TokenApiServer(
                ("127.0.0.1", 0),
                registry=registry,
                upload_key="u" * 32,
                admin_key="a" * 32,
                status_store=status_store,
                requests_per_minute=20,
                transport_private_key=None,
                session_validator=object(),
                maximum_accounts=1,
                artifact_root=artifact_root,
                artifact_requests_per_hour=10,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_port}"
            client_config = ClientConfig(endpoint, "u" * 32, root / "unused.pem", "repo")
            admin_config = AdminConfig(endpoint, "a" * 32, root / "unused.pem")
            environment = {
                "TOKEN_POOL_CLIENT_DATA": str(root / "client"),
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
            try:
                with patch.dict("os.environ", environment):
                    with self.assertRaisesRegex(AdminApiError, "rejected"):
                        admin_snapshot(AdminConfig(endpoint, "x" * 32, root / "unused.pem"))
                    client_preflight(
                        client_config,
                        event="startup",
                        account_ids=[],
                        status={"busy": False},
                    )
                    initial = admin_snapshot(admin_config)
                    self.assertEqual(len(initial["clients"]), 1)
                    self.assertEqual(initial["clients"][0]["account_ids"], [])
                    self.assertEqual(initial["clients"][0]["account_usernames"], [])
                    self.assertEqual(initial["pool"]["available_account_count"], 0)
                    online_rejection = admin_forget_clients(
                        admin_config,
                        [initial["clients"][0]["client_id"]],
                    )
                    self.assertEqual(online_rejection["forgotten"], [])
                    self.assertEqual(online_rejection["rejected"][0]["reason"], "client_online")
                    scoped = client_server_status(
                        client_config,
                        account_ids=[expired.account.account_id],
                    )
                    self.assertEqual(scoped["connection"], "online")
                    self.assertEqual(scoped["summary"]["configured_account_count"], 1)
                    self.assertEqual(scoped["summary"]["uploaded_account_count"], 1)
                    self.assertEqual(scoped["summary"]["ready_account_count"], 0)
                    self.assertEqual(len(scoped["accounts"]), 1)
                    self.assertEqual(scoped["accounts"][0]["account_id"], expired.account.account_id)
                    self.assertNotIn("username", scoped["accounts"][0])
                    legacy = client_server_status(client_config)
                    self.assertEqual(legacy["pool"]["accounts"], [])
                    self.assertNotIn("jobs", legacy)
                    client_id = initial["clients"][0]["client_id"]
                    created = admin_create_commands(
                        admin_config,
                        client_ids=[client_id],
                        command="force_renew",
                        payload={"interaction": "silent_only"},
                    )
                    self.assertEqual(len(created["created"]), 1)
                    polled = poll_admin_commands(
                        client_config,
                        account_ids=[expired.account.account_id],
                        status={"busy": False},
                    )
                    self.assertEqual(
                        polled["client_status"]["accounts"][0]["account_id"],
                        expired.account.account_id,
                    )
                    command = polled["command"]
                    self.assertEqual(command["command"], "force_renew")
                    self.assertEqual(command["payload"]["interaction"], "silent_only")
                    complete_admin_command(
                        client_config,
                        command_id=command["command_id"],
                        succeeded=True,
                        result={"successes": 1},
                    )
                    final = admin_snapshot(admin_config)
                    self.assertEqual(final["commands"][0]["status"], "completed")
                    self.assertEqual(final["commands"][0]["result"]["successes"], 1)
                    self.assertEqual(
                        final["clients"][0]["server_accounts"][0]["state"],
                        "renewal_required",
                    )
                    self.assertEqual(
                        final["clients"][0]["server_summary"]["ready_account_count"],
                        0,
                    )

                    stale_unmapped_id = "d" * 32
                    stale_mapped_id = "e" * 32
                    for stale_id, account_ids in (
                        (stale_unmapped_id, []),
                        (stale_mapped_id, [expired.account.account_id]),
                    ):
                        registry.update_client_presence(
                            client_id=stale_id,
                            observed_at=time.time() - 300,
                            event="heartbeat",
                            scheduled_slot=None,
                            app_version="old-test",
                            account_ids=account_ids,
                            remote_address="127.0.0.1",
                            status={"busy": False},
                        )
                    forgotten = admin_forget_clients(admin_config, [stale_unmapped_id])
                    self.assertEqual(forgotten["forgotten"], [stale_unmapped_id])
                    self.assertNotIn(
                        stale_unmapped_id,
                        {client["client_id"] for client in registry.list_clients()},
                    )
                    mapped_rejection = admin_forget_clients(admin_config, [stale_mapped_id])
                    self.assertEqual(mapped_rejection["forgotten"], [])
                    self.assertEqual(
                        mapped_rejection["rejected"][0]["reason"],
                        "client_has_accounts",
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_signed_client_preflight_records_scheduled_presence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = CopilotRegistry(root / "registry")
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            server = TokenApiServer(
                ("127.0.0.1", 0),
                registry=registry,
                upload_key="x" * 32,
                status_store=object(),
                requests_per_minute=10,
                transport_private_key=None,
                session_validator=object(),
                maximum_accounts=1,
                artifact_root=artifact_root,
                artifact_requests_per_hour=10,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            config = ClientConfig(
                f"http://127.0.0.1:{server.server_port}",
                "x" * 32,
                root / "unused-certificate.pem",
                "repo",
            )
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "TOKEN_POOL_CLIENT_DATA": str(root / "client"),
                        "NO_PROXY": "127.0.0.1,localhost",
                        "no_proxy": "127.0.0.1,localhost",
                    },
                ):
                    response = client_preflight(
                        config,
                        event="scheduled_refresh",
                        account_ids=["account-test"],
                        scheduled_slot="2026-07-16T09:45+01:00",
                    )
                self.assertTrue(response["ok"])
                events = registry.recent_client_events()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event"], "scheduled_refresh")
                self.assertEqual(events[0]["account_ids"], ["account-test"])
                self.assertEqual(events[0]["scheduled_slot"], "2026-07-16T09:45+01:00")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_busy_heartbeat_updates_presence_without_leasing_admin_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = CopilotRegistry(root / "registry")
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            server = TokenApiServer(
                ("127.0.0.1", 0),
                registry=registry,
                upload_key="x" * 32,
                admin_key="a" * 32,
                status_store=JobStatusStore(root / "job-status"),
                requests_per_minute=20,
                transport_private_key=None,
                session_validator=object(),
                maximum_accounts=1,
                artifact_root=artifact_root,
                artifact_requests_per_hour=10,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            config = ClientConfig(
                f"http://127.0.0.1:{server.server_port}",
                "x" * 32,
                root / "unused-certificate.pem",
                "repo",
            )
            environment = {
                "TOKEN_POOL_CLIENT_DATA": str(root / "client"),
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
            try:
                with patch.dict("os.environ", environment):
                    client_preflight(config, event="startup", account_ids=[])
                    client_id = registry.list_clients()[0]["client_id"]
                    command = registry.create_client_command(
                        client_id=client_id,
                        command="force_update",
                        payload={},
                        expires_in_seconds=600,
                    )
                    send_client_heartbeat(
                        config,
                        account_ids=[],
                        status={"busy": True, "activity": "Adding Microsoft account..."},
                    )
                client = registry.list_clients()[0]
                self.assertTrue(client["status"]["busy"])
                self.assertEqual(client["status"]["activity"], "Adding Microsoft account...")
                self.assertEqual(
                    registry.get_client_command(command["command_id"])["status"],
                    "queued",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_bundle_install_and_turn_cooldown_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = CopilotRegistry(Path(temporary))
            installed = install_bundle(bundle_bytes(), registry, remote_address="127.0.0.1")
            self.assertEqual(installed.account.username, "tester@example.com")
            public = installed.account.as_public_dict()
            self.assertEqual(public["username"], "te***@example.com")
            self.assertGreater(installed.account.refresh_expires_at or 0, time.time() + 22 * 60 * 60)
            self.assertNotIn("last_error", public)
            self.assertNotIn("session_path", public)
            access_expired = replace(
                installed.account,
                access_expires_at=time.time() - 60,
                refresh_expires_at=None,
            ).as_public_dict()
            self.assertFalse(access_expired["access_valid"])
            self.assertTrue(access_expired["runtime_available"])
            allowed1 = registry.reserve_turn(
                installed.account.account_id,
                turn_limit=2,
                window_seconds=3600,
                cooldown_seconds=60,
                job_id="job",
                operation="test",
            )
            allowed2 = registry.reserve_turn(
                installed.account.account_id,
                turn_limit=2,
                window_seconds=3600,
                cooldown_seconds=60,
            )
            denied = registry.reserve_turn(
                installed.account.account_id,
                turn_limit=2,
                window_seconds=3600,
                cooldown_seconds=60,
            )
            self.assertTrue(allowed1[0])
            self.assertTrue(allowed2[0])
            self.assertFalse(denied[0])
            self.assertEqual(registry.status()["recent_turns"], 2)
            self.assertEqual(registry.status()["turns_last_hour"], 2)
            self.assertEqual(registry.status()["turns_last_24_hours"], 2)
            usage = registry.turn_usage()[installed.account.account_id]
            self.assertEqual(usage["turns_last_hour"], 2)
            self.assertEqual(usage["turns_last_24_hours"], 2)
            reopened = CopilotRegistry(Path(temporary))
            self.assertEqual(reopened.status()["recent_turns"], 2)

    def test_token_client_artifacts_are_streamed_without_directory_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_root = root / "artifacts"
            release = artifact_root / "token-client-v1.2.3"
            release.mkdir(parents=True)
            payload = b"verified-client-part"
            (release / "TokenPoolClient-win-x64.zip.part000").write_bytes(payload)
            (root / "secret.txt").write_text("never served", encoding="utf-8")

            server = TokenApiServer(
                ("127.0.0.1", 0),
                registry=object(),
                upload_key="x" * 32,
                status_store=object(),
                requests_per_minute=10,
                transport_private_key=None,
                session_validator=object(),
                maximum_accounts=1,
                artifact_root=artifact_root,
                artifact_requests_per_hour=10,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                url = base + "/downloads/token-client/token-client-v1.2.3/TokenPoolClient-win-x64.zip.part000"
                with opener.open(url, timeout=5) as response:
                    self.assertEqual(response.read(), payload)
                    self.assertEqual(response.headers["Cache-Control"], "public, max-age=31536000, immutable")
                request = urllib.request.Request(url, method="HEAD")
                with opener.open(request, timeout=5) as response:
                    self.assertEqual(int(response.headers["Content-Length"]), len(payload))
                    self.assertEqual(response.read(), b"")
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(
                        base + "/downloads/token-client/token-client-v1.2.3/../secret.txt",
                        timeout=5,
                    )
                self.assertEqual(rejected.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_bundle_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = CopilotRegistry(Path(temporary))
            with self.assertRaises(BundleValidationError):
                install_bundle(bundle_bytes(unsafe_name="../escape.txt"), registry)

    def test_bundle_account_quota_rejects_a_new_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = CopilotRegistry(Path(temporary))
            install_bundle(bundle_bytes(), registry, maximum_accounts=1)
            with self.assertRaisesRegex(BundleValidationError, "account limit"):
                install_bundle(
                    bundle_bytes(object_id="33333333-3333-3333-3333-333333333333"),
                    registry,
                    maximum_accounts=1,
                )


if __name__ == "__main__":
    unittest.main()
