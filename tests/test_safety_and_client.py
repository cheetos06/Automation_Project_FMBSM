from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "automation-mail-worker"
CLIENT = ROOT / "apps" / "token-pool-client" / "src"
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(CLIENT))

from fmbsm_email_bot.fs_job import _looks_prior, prepare_ticket  # noqa: E402
from fmbsm_email_bot.mail import _unread_subject_search_terms  # noqa: E402
from fmbsm_email_bot.worker import _is_authorized_job_sender  # noqa: E402
from fmbsm_email_bot.zip_utils import safe_extract_files  # noqa: E402
from token_pool_client.bundle import create_bundle  # noqa: E402
from token_pool_client.automation import (  # noqa: E402
    AutomationState,
    configured_refresh_times,
    is_automatic_work_account,
    latest_due_slot,
    slot_key,
)
from token_pool_client.app import (  # noqa: E402
    RenewalBatchResult,
    TokenPoolApp,
    _account_status,
    _authorization_expires_at,
    _retry_is_due,
    _server_account_state,
)
from token_pool_client.bootstrap import (  # noqa: E402
    BrowserConnectivityError,
    navigate,
    upload_image_or_pause,
)
from token_pool_client.refresh import (  # noqa: E402
    RefreshError,
    decrypt_captured_msal,
    requires_interactive_reauthentication,
)
from token_pool_client.storage import ClientAccount  # noqa: E402
from token_pool_client.transport_crypto import encrypt_bundle  # noqa: E402
from token_pool_client.upload import (  # noqa: E402
    ClientConfig,
    TransientNetworkError,
    _request_with_retry,
    is_transient_network_error,
)
from copilot_service.transport_crypto import EnvelopeError, decrypt_envelope, load_private_key  # noqa: E402
from copilot_service.microsoft_oauth import CLIENT_ID, OAuthRefreshRejected  # noqa: E402
from copilot_service.session_bundle import BundleValidationError  # noqa: E402
from copilot_service.upload_validation import (  # noqa: E402
    EXPECTED_AUDIENCE,
    MicrosoftSessionValidator,
)


def _urlsafe_json(value: dict[str, object]) -> str:
    encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _test_token(tenant: str, object_id: str, *, marker: str) -> str:
    claims: dict[str, object] = {
        "tid": tenant,
        "oid": object_id,
        "preferred_username": "tester@example.com",
        "iss": f"https://sts.windows.net/{tenant}/",
        "aud": EXPECTED_AUDIENCE,
        "appid": CLIENT_ID,
        "nbf": 1,
        "exp": 9999999999,
        "marker": marker,
    }
    return f"{_urlsafe_json({'alg': 'RS256', 'typ': 'JWT'})}.{_urlsafe_json(claims)}.test-signature"


def _session_files(access_token: str) -> dict[str, bytes]:
    values: dict[str, object] = {
        "private_websocket_raw_frames_1.json": [
            {
                "url": (
                    "wss://substrate.office.com/m365copilot/chathub"
                    f"?access_token={access_token}"
                )
            }
        ],
        "private_replay_templates_1.json": [
            {"headers": {"Authorization": f"Bearer {access_token}"}}
        ],
        "private_playwright_cookies_1.json": [{"name": "session", "value": "test"}],
        "private_edge_msal_refresh_token_1.json": {"refresh_token": "valid-refresh-token"},
    }
    return {
        name: (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")
        for name, value in values.items()
    }


class InputSafetyTests(unittest.TestCase):
    def test_job_sender_allowlist_uses_exact_addresses_and_domains(self) -> None:
        settings = SimpleNamespace(
            authorized_job_senders=("approved.one@gmail.com", "approved.two@gmail.com"),
            authorized_job_sender_domains=("forvismazars.com", "mazars.fr"),
        )
        for sender in (
            "person@forvismazars.com",
            "PERSON@MAZARS.FR",
            "approved.one@gmail.com",
            "approved.two@gmail.com",
        ):
            self.assertTrue(_is_authorized_job_sender(settings, sender), sender)
        for sender in (
            "outsider@gmail.com",
            "person@sub.mazars.fr",
            "person@mazars.fr.attacker.example",
            "person@forvismazars.com.attacker.example",
            "",
        ):
            self.assertFalse(_is_authorized_job_sender(settings, sender), sender)

    def test_imap_query_routes_both_job_prefixes(self) -> None:
        self.assertEqual(
            _unread_subject_search_terms("[optimda-extract-dates]", "[fs-review]"),
            (
                "UNSEEN",
                "OR",
                "HEADER",
                "Subject",
                '"[optimda-extract-dates]"',
                "HEADER",
                "Subject",
                '"[fs-review]"',
            ),
        )

    def test_fs_input_classification_and_canonical_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for name in ("Client FS N.pdf", "Client FS N−1.pdf", "Client BG.xlsx"):
                (source / name).write_bytes(b"test")
            ticket = prepare_ticket(source.iterdir(), root / "job")
            names = sorted(path.name for path in (ticket / "Input").iterdir())
            self.assertEqual(
                names,
                ["bg_standardized.xlsx", "financial_statements_N.pdf", "financial_statements_N_1.pdf"],
            )
        self.assertTrue(_looks_prior("financial statements previous.pdf"))
        self.assertFalse(_looks_prior("financial statements N.pdf"))

    def test_zip_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escape.pdf", b"bad")
            with self.assertRaises(ValueError):
                safe_extract_files(
                    archive,
                    root / "out",
                    allowed_suffixes={".pdf"},
                    max_extracted_bytes=1_000,
                    max_files=5,
                )
            self.assertFalse((root / "escape.pdf").exists())


class ClientBundleTests(unittest.TestCase):
    def test_only_company_work_domains_are_automatically_renewed(self) -> None:
        for username in ("person@forvismazars.com", "PERSON@MAZARS.FR"):
            self.assertTrue(is_automatic_work_account(username), username)
        for username in (
            "anas.nmili@edu.isga.ma",
            "person@sub.mazars.fr",
            "person@mazars.fr.attacker.example",
            "person@gmail.com",
            "invalid",
        ):
            self.assertFalse(is_automatic_work_account(username), username)

    def test_automatic_schedule_uses_local_0445_and_0945_slots(self) -> None:
        times = configured_refresh_times("")
        self.assertEqual(times, ("04:45", "09:45"))
        before_second = datetime(2026, 7, 15, 9, 44).astimezone()
        due = latest_due_slot(before_second, times)
        self.assertIsNotNone(due)
        self.assertEqual((due.hour, due.minute), (4, 45))
        self.assertIn("T04:45", slot_key(due))
        after_second = datetime(2026, 7, 15, 9, 46).astimezone()
        due = latest_due_slot(after_second, times)
        self.assertIsNotNone(due)
        self.assertEqual((due.hour, due.minute), (9, 45))

    def test_schedule_rejects_invalid_times(self) -> None:
        with self.assertRaises(ValueError):
            configured_refresh_times("25:00")

    def test_automation_state_persists_pending_network_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = AutomationState(
                path=root / "automation-state.json",
                last_work_refresh_result="waiting_for_network:2",
                pending_work_refresh_slot="2026-07-16T09:45+01:00",
                next_work_retry_at="2026-07-16T10:00:00+01:00",
                work_retry_count=2,
            )
            state.save()
            loaded = AutomationState.load(root)
            self.assertEqual(loaded.pending_work_refresh_slot, state.pending_work_refresh_slot)
            self.assertEqual(loaded.next_work_retry_at, state.next_work_retry_at)
            self.assertEqual(loaded.work_retry_count, 2)
            self.assertFalse(
                _retry_is_due(
                    loaded.next_work_retry_at,
                    datetime.fromisoformat("2026-07-16T09:55:00+01:00"),
                )
            )

    def test_scheduler_retries_offline_slot_then_marks_only_success_complete(self) -> None:
        now = datetime.now().astimezone()
        due_time = now.strftime("%H:%M")
        account = ClientAccount(
            account_id="account-test",
            username="tester@mazars.fr",
            tenant_id="tenant",
            object_id="object",
            session_dir="session",
            profile_dir="profile",
            access_expires_at=now.timestamp() + 3600,
        )
        with tempfile.TemporaryDirectory() as temporary:
            app = TokenPoolApp.__new__(TokenPoolApp)
            app.shutting_down = False
            app.refresh_times = (due_time,)
            app.store = MagicMock()
            app.store.load.return_value = [account]
            app.automation_state = AutomationState(path=Path(temporary) / "automation-state.json")
            app.busy = False
            app.next_update_check = float("inf")
            app.root = MagicMock()
            app.tray = MagicMock()
            app.log = MagicMock()
            app._renew_and_upload = MagicMock()
            results = iter(
                [
                    RenewalBatchResult(transient_failures=1),
                    RenewalBatchResult(successes=1),
                ]
            )

            def run_background(label, work, *, on_complete, show_error):
                on_complete(next(results), None)
                return True

            app._background = run_background
            app._scheduler_tick()
            pending_slot = app.automation_state.pending_work_refresh_slot
            self.assertTrue(pending_slot)
            self.assertEqual(app.automation_state.last_work_refresh_slot, "")
            self.assertEqual(app.automation_state.last_work_refresh_result, "waiting_for_network:1")

            app.automation_state.next_work_retry_at = (now - timedelta(seconds=1)).isoformat()
            app._scheduler_tick()
            self.assertEqual(app.automation_state.last_work_refresh_slot, pending_slot)
            self.assertEqual(app.automation_state.pending_work_refresh_slot, "")
            self.assertEqual(app.automation_state.last_work_refresh_result, "success:1")

    def test_one_hour_access_expiry_does_not_mean_authorization_expiry(self) -> None:
        now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC).timestamp()
        account = ClientAccount(
            account_id="account-test",
            username="tester@mazars.fr",
            tenant_id="tenant",
            object_id="object",
            session_dir="session",
            profile_dir="profile",
            access_expires_at=now - 60,
            authorization_expires_at=now + 20 * 60 * 60,
        )
        self.assertEqual(_account_status(account, now=now), "Ready")
        self.assertEqual(_authorization_expires_at(account), now + 20 * 60 * 60)
        self.assertEqual(
            _server_account_state(
                {
                    "enabled": True,
                    "access_valid": False,
                    "refresh_expires_at": None,
                    "cooling_down": False,
                },
                now=now,
            ),
            "ready",
        )

    def test_transient_connectivity_is_friendly_and_retried_with_new_request(self) -> None:
        self.assertTrue(is_transient_network_error(OSError("[WinError 10060] failed to respond")))
        config = ClientConfig("http://example.invalid", "key", Path("unused"), "repo")
        requests: list[object] = []

        def request_factory() -> object:
            request = object()
            requests.append(request)
            return request

        with patch(
            "token_pool_client.upload._open_json",
            side_effect=[TransientNetworkError("offline"), {"ok": True}],
        ) as open_json:
            with patch("token_pool_client.upload.time.sleep"):
                self.assertEqual(
                    _request_with_retry(request_factory, config, timeout_seconds=1, attempts=2),
                    {"ok": True},
                )
        self.assertEqual(len(requests), 2)
        self.assertIsNot(requests[0], requests[1])
        self.assertEqual(open_json.call_count, 2)

    def test_offline_preflight_does_not_open_microsoft_and_remains_pending(self) -> None:
        account = ClientAccount(
            account_id="account-test",
            username="tester@mazars.fr",
            tenant_id="tenant",
            object_id="object",
            session_dir="session",
            profile_dir="profile",
            access_expires_at=9999999999,
        )
        app = TokenPoolApp.__new__(TokenPoolApp)
        app.config = ClientConfig("http://example.invalid", "key", Path("unused"), "repo")
        app.store = MagicMock()
        app.log = MagicMock()
        app._fresh_renewal = MagicMock()
        with patch(
            "token_pool_client.app.server_status",
            side_effect=TransientNetworkError("offline"),
        ):
            result = app._renew_and_upload([account], automatic=True)
        self.assertTrue(result.waiting_for_network)
        self.assertEqual(result.transient_failures, 1)
        app._fresh_renewal.assert_not_called()

    def test_network_failure_after_renewal_preserves_pending_upload_without_token_error(self) -> None:
        account = ClientAccount(
            account_id="account-test",
            username="tester@mazars.fr",
            tenant_id="tenant",
            object_id="object",
            session_dir="session",
            profile_dir="profile",
            access_expires_at=9999999999,
            authorization_expires_at=9999999999,
        )
        app = TokenPoolApp.__new__(TokenPoolApp)
        app.config = ClientConfig("http://example.invalid", "key", Path("unused"), "repo")
        app.store = MagicMock()
        app.log = MagicMock()
        app._fresh_renewal = MagicMock(return_value=account)
        with patch("token_pool_client.app.server_status", return_value={"pool": {}}), patch(
            "token_pool_client.app.create_bundle", return_value=b"bundle"
        ), patch(
            "token_pool_client.app.upload_bundle",
            side_effect=TransientNetworkError("offline"),
        ):
            result = app._renew_and_upload([account], automatic=True)
        self.assertTrue(result.waiting_for_network)
        self.assertTrue(account.pending_upload)
        self.assertIsNone(account.last_error)
        self.assertGreaterEqual(app.store.upsert.call_count, 1)

    def test_bootstrap_upload_clicks_only_one_explicit_control(self) -> None:
        page = MagicMock()
        button = MagicMock()
        chooser = MagicMock()
        chooser.value = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = chooser
        page.expect_file_chooser.return_value = context
        with patch("token_pool_client.bootstrap.click_sources_menu_if_present", return_value=True), patch(
            "token_pool_client.bootstrap.wait_for_file_input", return_value=None
        ), patch("token_pool_client.bootstrap.first_visible", return_value=button):
            method = upload_image_or_pause(page, Path("bootstrap.png"), interactive=False)
        self.assertEqual(method, "filechooser")
        button.click.assert_called_once()
        page.get_by_role.assert_not_called()

    def test_navigation_timeout_requires_a_usable_microsoft_dom(self) -> None:
        stalled = MagicMock()
        stalled.goto.side_effect = PlaywrightTimeoutError("timed out")
        stalled.evaluate.return_value = "loading"
        with self.assertRaises(BrowserConnectivityError):
            navigate(stalled, "https://m365.cloud.microsoft/chat")

        usable = MagicMock()
        usable.goto.side_effect = PlaywrightTimeoutError("timed out")
        usable.evaluate.return_value = "interactive"
        navigate(usable, "https://m365.cloud.microsoft/chat")

    def test_expired_spa_refresh_token_requires_real_sign_in(self) -> None:
        self.assertTrue(
            requires_interactive_reauthentication(
                RefreshError("AADSTS700084: refresh token was issued to a single page app and is expired")
            )
        )
        self.assertTrue(requires_interactive_reauthentication(RefreshError("invalid_grant")))
        self.assertFalse(
            requires_interactive_reauthentication(RefreshError("Microsoft refresh failed: connection timed out"))
        )

    def test_current_plaintext_msal_cache_does_not_require_encryption_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            payload = {
                "records": [
                    {
                        "key": "msal.3|home.tenant|login.windows.net|refreshtoken|client",
                        "value": json.dumps(
                            {
                                "credentialType": "RefreshToken",
                                "homeAccountId": "home.tenant",
                                "clientId": "4765445b-32c6-49b0-83e6-1d93765276ca",
                                "secret": "test-refresh-token",
                                "lastUpdatedAt": 123,
                            }
                        ),
                    },
                    {"key": "msal.3.account.keys", "value": "[]"},
                ]
            }
            (session / "private_msal_local_storage_current.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            records = decrypt_captured_msal(session)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["value"]["secret"], "test-refresh-token")

    def test_microsoft_refresh_proves_tenant_account_and_patches_session(self) -> None:
        tenant = "11111111-1111-1111-1111-111111111111"
        presented = _test_token(tenant, "22222222-2222-2222-2222-222222222222", marker="old")
        refreshed = _test_token(tenant, "22222222-2222-2222-2222-222222222222", marker="new")
        files = _session_files(presented)
        validator = MicrosoftSessionValidator(
            {tenant},
            oauth_exchanger=lambda actual_tenant, refresh, timeout: {
                "access_token": refreshed,
                "refresh_token": "rotated-refresh-token",
                "expires_in": 3600,
            },
        )
        claims = validator.validate(files, presented)
        self.assertEqual(claims["oid"], "22222222-2222-2222-2222-222222222222")
        self.assertIn(refreshed.encode("ascii"), files["private_websocket_raw_frames_1.json"])
        self.assertNotIn(presented.encode("ascii"), files["private_replay_templates_1.json"])
        oauth_payload = json.loads(files["private_edge_msal_refresh_token_1.json"])
        self.assertEqual(oauth_payload["refresh_token"], "rotated-refresh-token")

    def test_microsoft_refresh_rejection_and_identity_swap_are_rejected(self) -> None:
        tenant = "11111111-1111-1111-1111-111111111111"
        presented = _test_token(tenant, "22222222-2222-2222-2222-222222222222", marker="old")
        rejected = MicrosoftSessionValidator(
            {tenant},
            oauth_exchanger=lambda actual_tenant, refresh, timeout: (_ for _ in ()).throw(
                OAuthRefreshRejected("invalid_grant")
            ),
        )
        with self.assertRaises(BundleValidationError):
            rejected.validate(_session_files(presented), presented)

        other_account = _test_token(tenant, "33333333-3333-3333-3333-333333333333", marker="new")
        swapped = MicrosoftSessionValidator(
            {tenant},
            oauth_exchanger=lambda actual_tenant, refresh, timeout: {"access_token": other_account},
        )
        with self.assertRaises(BundleValidationError):
            swapped.validate(_session_files(presented), presented)

    def test_client_bundle_contains_only_runtime_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session"
            profile = Path(temporary) / "profile"
            session.mkdir()
            profile.mkdir()
            required = {
                "private_websocket_raw_frames_1.json": [],
                "private_replay_templates_1.json": [],
                "private_playwright_cookies_1.json": [],
                "private_edge_msal_refresh_token_1.json": {"refresh_token": "test"},
            }
            for name, payload in required.items():
                (session / name).write_text(json.dumps(payload), encoding="utf-8")
            (profile / "browser-secret.txt").write_text("never upload", encoding="utf-8")
            account = ClientAccount(
                account_id="account-test",
                username="tester@example.com",
                tenant_id="tenant",
                object_id="object",
                session_dir=str(session),
                profile_dir=str(profile),
                access_expires_at=9999999999,
            )
            bundle_path = Path(temporary) / "bundle.zip"
            bundle_path.write_bytes(create_bundle(account))
            with zipfile.ZipFile(bundle_path) as archive:
                names = set(archive.namelist())
                manifest = json.loads(archive.read("manifest.json"))
            self.assertIn("manifest.json", names)
            self.assertEqual(manifest["version"], 2)
            self.assertEqual(names - {"manifest.json"}, set(required))
            self.assertNotIn("browser-secret.txt", names)

    def test_encrypted_transport_round_trip_and_tamper_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
            certificate = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
                .not_valid_after(datetime.now(UTC) + timedelta(days=1))
                .sign(key, hashes.SHA256())
            )
            certificate_path = root / "server.crt"
            key_path = root / "server.key"
            certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            plaintext = b"private token bundle test"
            envelope = encrypt_bundle(plaintext, certificate_path)
            private_key = load_private_key(key_path)
            self.assertEqual(decrypt_envelope(envelope, private_key), plaintext)
            damaged = bytearray(envelope)
            damaged[-8] = ord("A") if damaged[-8] != ord("A") else ord("B")
            with self.assertRaises(EnvelopeError):
                decrypt_envelope(bytes(damaged), private_key)


if __name__ == "__main__":
    unittest.main()
