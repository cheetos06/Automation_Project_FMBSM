from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "automation-mail-worker"
sys.path.insert(0, str(SERVICE))

from copilot_service.registry import CopilotRegistry  # noqa: E402
from copilot_service.session_bundle import BundleValidationError, install_bundle  # noqa: E402


def fake_jwt(claims: dict[str, object]) -> str:
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


def bundle_bytes(*, unsafe_name: str | None = None) -> bytes:
    token = fake_jwt(
        {
            "tid": "11111111-1111-1111-1111-111111111111",
            "oid": "22222222-2222-2222-2222-222222222222",
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

    def test_bundle_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = CopilotRegistry(Path(temporary))
            with self.assertRaises(BundleValidationError):
                install_bundle(bundle_bytes(unsafe_name="../escape.txt"), registry)


if __name__ == "__main__":
    unittest.main()
