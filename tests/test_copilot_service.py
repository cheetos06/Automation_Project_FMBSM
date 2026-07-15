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
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "automation-mail-worker"
sys.path.insert(0, str(SERVICE))

from copilot_service.registry import CopilotRegistry  # noqa: E402
from copilot_service.session_bundle import BundleValidationError, install_bundle  # noqa: E402
from copilot_service.token_api import TokenApiServer  # noqa: E402


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
        archive.writestr("manifest.json", json.dumps({"client_version": "test"}))
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
    def test_bundle_install_and_turn_cooldown_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = CopilotRegistry(Path(temporary))
            installed = install_bundle(bundle_bytes(), registry, remote_address="127.0.0.1")
            self.assertEqual(installed.account.username, "tester@example.com")
            public = installed.account.as_public_dict()
            self.assertEqual(public["username"], "te***@example.com")
            self.assertNotIn("last_error", public)
            self.assertNotIn("session_path", public)
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
