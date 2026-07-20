from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
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
from token_pool_client import upload as client_upload  # noqa: E402
from token_pool_client import refresh as client_refresh  # noqa: E402
from token_pool_client.automation import (  # noqa: E402
    AutomationState,
    configured_refresh_times,
    is_automatic_work_account,
    latest_due_slot,
    restart_after_exit,
    slot_key,
)
from token_pool_client.app import (  # noqa: E402
    RenewalBatchResult,
    TokenPoolApp,
    _account_status,
    _authorization_expires_at,
    _my_aws_status_text,
    _retry_is_due,
    _server_account_state,
)
from token_pool_client.bootstrap import (  # noqa: E402
    BrowserConnectivityError,
    InteractiveAuthenticationRequired,
    click_sources_menu_if_present,
    explicit_upload_control,
    explicit_authentication_required,
    navigate,
    upload_image_or_pause,
)
from token_pool_client.refresh import (  # noqa: E402
    RefreshError,
    decrypt_captured_msal,
    requires_interactive_reauthentication,
)
from token_pool_client.storage import AccountStore, ClientAccount  # noqa: E402
from token_pool_client.transport_crypto import encrypt_bundle  # noqa: E402
from token_pool_client.upload import (  # noqa: E402
    ClientConfig,
    ServerRejectedError,
    TransientNetworkError,
    _request_with_retry,
    _sign,
    client_preflight,
    is_transient_network_error,
)
from copilot_service.transport_crypto import EnvelopeError, decrypt_envelope, load_private_key  # noqa: E402
from copilot_service.microsoft_oauth import CLIENT_ID, OAuthRefreshRejected  # noqa: E402
from copilot_service.session_bundle import BundleValidationError  # noqa: E402
from copilot_service.upload_validation import (  # noqa: E402
    EXPECTED_AUDIENCE,
    MicrosoftSessionRejected,
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
    def test_first_run_threads_share_one_persistent_client_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            barrier = threading.Barrier(16)

            def synchronized_application_dir() -> Path:
                barrier.wait(timeout=5)
                return root

            with patch.object(
                client_upload,
                "application_dir",
                side_effect=synchronized_application_dir,
            ):
                with ThreadPoolExecutor(max_workers=16) as executor:
                    client_ids = list(
                        executor.map(lambda _: client_upload._client_installation_id(), range(16))
                    )

            self.assertEqual(len(set(client_ids)), 1)
            self.assertEqual(
                (root / "client-id.txt").read_text(encoding="ascii").strip(),
                client_ids[0],
            )

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

    def test_primary_manual_renewal_skips_non_work_accounts(self) -> None:
        work = ClientAccount(
            account_id="account-work",
            username="tester@mazars.fr",
            tenant_id="tenant",
            object_id="work",
            session_dir="work-session",
            profile_dir="work-profile",
            access_expires_at=9999999999,
        )
        isga = ClientAccount(
            account_id="account-isga",
            username="tester@edu.isga.ma",
            tenant_id="tenant",
            object_id="isga",
            session_dir="isga-session",
            profile_dir="isga-profile",
            access_expires_at=9999999999,
        )
        app = TokenPoolApp.__new__(TokenPoolApp)
        app.store = MagicMock()
        app.store.load.return_value = [isga, work]
        app._renew_and_upload = MagicMock(return_value=RenewalBatchResult(successes=1))
        app._background = MagicMock(
            side_effect=lambda label, action, **kwargs: action()
        )

        app.refresh_all()

        selected = app._renew_and_upload.call_args.args[0]
        self.assertEqual([account.account_id for account in selected], [work.account_id])
        self.assertTrue(app._renew_and_upload.call_args.kwargs["automatic"])

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

    def test_successful_upload_after_due_slot_prevents_redundant_renewal(self) -> None:
        now = datetime.now().astimezone()
        due_time = now.strftime("%H:%M")
        due = latest_due_slot(now, (due_time,))
        self.assertIsNotNone(due)
        account = ClientAccount(
            account_id="account-test",
            username="tester@mazars.fr",
            tenant_id="tenant",
            object_id="object",
            session_dir="session",
            profile_dir="profile",
            access_expires_at=now.timestamp() + 3600,
            last_uploaded_at=now.timestamp(),
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
            app.log = MagicMock()
            app._background = MagicMock()

            app._scheduler_tick()

            app._background.assert_not_called()
            self.assertEqual(app.automation_state.last_work_refresh_slot, slot_key(due))
            self.assertEqual(
                app.automation_state.last_work_refresh_result,
                "satisfied_by_recent_upload",
            )
            self.assertEqual(app.automation_state.pending_work_refresh_slot, "")

            app.automation_state.last_work_refresh_result = "failed:1"
            app.automation_state.save()
            app._scheduler_tick()
            self.assertEqual(
                app.automation_state.last_work_refresh_result,
                "satisfied_by_recent_upload",
            )

    def test_ready_aws_session_clears_stale_rejected_retry_warning(self) -> None:
        account = ClientAccount(
            account_id="account-test",
            username="tester@mazars.fr",
            tenant_id="tenant",
            object_id="object",
            session_dir="session",
            profile_dir="profile",
            access_expires_at=9999999999,
            last_error=(
                "Server rejected the request: {'microsoft_error_code': 'AADSTS50078', "
                "'action': 'interactive_mfa_'}"
            ),
        )
        app = TokenPoolApp.__new__(TokenPoolApp)
        app.store = MagicMock()
        app.store.load.return_value = [account]
        app.log = MagicMock()
        app._refresh_table = MagicMock()

        app._reconcile_ready_server_accounts(
            {"accounts": [{"account_id": "account-test", "ready": True}]}
        )

        self.assertIsNone(account.last_error)
        app.store.upsert.assert_called_once_with(account)
        app._refresh_table.assert_called_once_with()

    def test_scheduler_stops_after_two_delayed_network_retries(self) -> None:
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
            attempts = 0

            def run_background(label, work, *, on_complete, show_error):
                nonlocal attempts
                attempts += 1
                on_complete(RenewalBatchResult(transient_failures=1), None)
                return True

            app._background = run_background
            for _ in range(3):
                app._scheduler_tick()
                app.automation_state.next_work_retry_at = (now - timedelta(seconds=1)).isoformat()

            self.assertEqual(attempts, 3)
            self.assertTrue(app.automation_state.last_work_refresh_slot)
            self.assertEqual(app.automation_state.pending_work_refresh_slot, "")
            self.assertEqual(app.automation_state.work_retry_count, 2)
            self.assertEqual(app.automation_state.last_work_refresh_result, "network_retry_exhausted:2")
            app._scheduler_tick()
            self.assertEqual(attempts, 3)

    def test_scheduler_does_not_resurrect_legacy_failed_slot(self) -> None:
        now = datetime.now().astimezone()
        due_time = now.strftime("%H:%M")
        due = latest_due_slot(now, (due_time,))
        self.assertIsNotNone(due)
        account = ClientAccount(
            account_id="account-test",
            username="tester@mazars.fr",
            tenant_id="tenant",
            object_id="object",
            session_dir="session",
            profile_dir="profile",
            access_expires_at=now.timestamp() - 60,
            last_error="[WinError 10060] failed to respond",
        )
        with tempfile.TemporaryDirectory() as temporary:
            app = TokenPoolApp.__new__(TokenPoolApp)
            app.shutting_down = False
            app.refresh_times = (due_time,)
            app.store = MagicMock()
            app.store.load.return_value = [account]
            app.automation_state = AutomationState(
                path=Path(temporary) / "automation-state.json",
                last_work_refresh_slot=slot_key(due),
                last_work_refresh_result="failed:1",
            )
            app.busy = False
            app.next_update_check = float("inf")
            app.root = MagicMock()
            app._background = MagicMock()
            app._scheduler_tick()
            app._background.assert_not_called()
            self.assertEqual(app.automation_state.pending_work_refresh_slot, "")

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

    def test_colleague_status_renders_only_locally_configured_accounts(self) -> None:
        local = ClientAccount(
            account_id="account-local",
            username="colleague@mazars.fr",
            tenant_id="tenant",
            object_id="object",
            session_dir="session",
            profile_dir="profile",
            access_expires_at=0,
        )
        text = _my_aws_status_text(
            [local],
            {
                "version": "1.2.0",
                "summary": {
                    "configured_account_count": 1,
                    "ready_account_count": 1,
                },
                "accounts": [
                    {
                        "account_id": "account-local",
                        "uploaded": True,
                        "ready": True,
                        "state": "ready",
                        "uploaded_at": 1784260000,
                        "authorization_expires_at": 1784346400,
                    },
                    {
                        "account_id": "account-other",
                        "username": "other.person@mazars.fr",
                        "uploaded": True,
                        "ready": True,
                        "state": "ready",
                    },
                ],
            },
        )
        self.assertIn("colleague@mazars.fr: Uploaded and ready", text)
        self.assertNotIn("other.person", text)
        self.assertNotIn("account-other", text)

    def test_transient_connectivity_is_friendly_and_retried_with_new_request(self) -> None:
        self.assertTrue(is_transient_network_error(OSError("[WinError 10060] failed to respond")))
        config = ClientConfig("http://example.invalid", "key", Path("unused"), "repo")
        requests: list[urllib.request.Request] = []

        def request_factory() -> urllib.request.Request:
            request = _sign(
                urllib.request.Request("http://example.invalid/v1/status"),
                config.upload_key,
                b"",
            )
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
        self.assertEqual(len(requests), 1)
        self.assertEqual(open_json.call_count, 2)
        alternate = open_json.call_args_list[1].args[0]
        self.assertEqual(alternate.full_url, "https://example.invalid/v1/status")
        self.assertNotEqual(
            requests[0].get_header("X-fmbsm-nonce"),
            alternate.get_header("X-fmbsm-nonce"),
        )

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
            "token_pool_client.app.client_preflight",
            side_effect=TransientNetworkError("offline"),
        ):
            result = app._renew_and_upload([account], automatic=True)
        self.assertTrue(result.waiting_for_network)
        self.assertEqual(result.transient_failures, 1)
        app._fresh_renewal.assert_not_called()

    def test_starting_background_work_immediately_requests_busy_presence(self) -> None:
        app = TokenPoolApp.__new__(TokenPoolApp)
        app.busy = False
        app.current_activity = "Ready"
        app.next_presence_heartbeat = 999.0
        app.next_control_poll = 999.0
        app.refresh_button = MagicMock()
        app.add_button = MagicMock()
        app.status_button = MagicMock()
        app.state_label = MagicMock()
        app._send_busy_heartbeat = MagicMock()

        app._set_busy(True, "Adding Microsoft account...")

        self.assertTrue(app.busy)
        self.assertEqual(app.current_activity, "Adding Microsoft account...")
        self.assertEqual(app.next_presence_heartbeat, 0.0)
        app._send_busy_heartbeat.assert_called_once_with()

        app._set_busy(False)
        self.assertFalse(app.busy)
        self.assertEqual(app.current_activity, "Ready")
        self.assertEqual(app.next_control_poll, 0.0)

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
        with patch("token_pool_client.app.client_preflight", return_value={"pool": {}}), patch(
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

    def test_client_preflight_falls_back_when_server_endpoint_is_not_deployed(self) -> None:
        config = ClientConfig("http://example.invalid", "key", Path("unused"), "repo")
        with patch(
            "token_pool_client.upload._request_with_retry",
            side_effect=ServerRejectedError(404, {"error": "not_found"}),
        ), patch(
            "token_pool_client.upload.server_status",
            return_value={"ok": True, "pool": {}},
        ) as fallback:
            result = client_preflight(
                config,
                event="scheduled_refresh",
                account_ids=["account-test"],
                scheduled_slot="2026-07-16T09:45+01:00",
            )
        self.assertTrue(result["ok"])
        fallback.assert_called_once_with(config)

    def test_client_api_rebuilds_request_before_direct_proxy_fallback(self) -> None:
        config = ClientConfig("http://example.invalid", "k" * 32, Path("unused"), "repo")
        proxy_opener = MagicMock()
        proxy_opener.open.side_effect = urllib.error.URLError("proxy unavailable")
        response = MagicMock()
        response.read.return_value = b'{"ok":true}'
        direct_context = MagicMock()
        direct_context.__enter__.return_value = response
        direct_opener = MagicMock()
        direct_opener.open.return_value = direct_context
        client_upload._PROXY_ROUTE_CACHE.clear()
        request = urllib.request.Request(
            "http://example.invalid/v1/client/status",
            data=b"{}",
            method="POST",
        )

        with patch(
            "token_pool_client.upload._opener",
            side_effect=[proxy_opener, direct_opener],
        ):
            result = client_upload._open_json(request, config, timeout_seconds=1)

        self.assertTrue(result["ok"])
        first_request = proxy_opener.open.call_args.args[0]
        second_request = direct_opener.open.call_args.args[0]
        self.assertIsNot(first_request, second_request)
        self.assertFalse(client_upload._PROXY_ROUTE_CACHE["http://example.invalid"])

    def test_microsoft_oauth_retries_proxy_after_direct_route_failure(self) -> None:
        direct_opener = MagicMock()
        direct_opener.open.side_effect = urllib.error.URLError("direct route unavailable")
        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(
            {"access_token": "access", "refresh_token": "refresh"}
        ).encode("utf-8")
        proxy_context = MagicMock()
        proxy_context.__enter__.return_value = response
        proxy_opener = MagicMock()
        proxy_opener.open.return_value = proxy_context
        client_refresh._OAUTH_ROUTE_CACHE.clear()

        with patch(
            "token_pool_client.refresh.urllib.request.build_opener",
            side_effect=[direct_opener, proxy_opener],
        ) as build_opener:
            payload = client_refresh.oauth_refresh("refresh", "tenant")

        self.assertEqual(payload["access_token"], "access")
        self.assertEqual(build_opener.call_count, 2)
        self.assertTrue(client_refresh._OAUTH_ROUTE_CACHE)
        self.assertTrue(next(iter(client_refresh._OAUTH_ROUTE_CACHE.values())))

    def test_retry_recovers_browser_capture_without_reopening_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session"
            profile = Path(temporary) / "profile"
            session.mkdir()
            profile.mkdir()
            converted = session / "private_edge_msal_refresh_token_old.json"
            converted.write_text("{}", encoding="utf-8")
            summary = session / "interactive_reauth_new_summary.json"
            summary.write_text("{}", encoding="utf-8")
            now = time.time()
            os.utime(converted, (now - 60, now - 60))
            os.utime(summary, (now, now))
            account = ClientAccount(
                account_id="account-test",
                username="tester@mazars.fr",
                tenant_id="tenant",
                object_id="object",
                session_dir=str(session),
                profile_dir=str(profile),
                access_expires_at=now - 60,
                authorization_expires_at=now - 60,
            )
            renewed = ClientAccount(
                account_id=account.account_id,
                username=account.username,
                tenant_id=account.tenant_id,
                object_id=account.object_id,
                session_dir=account.session_dir,
                profile_dir=account.profile_dir,
                access_expires_at=now + 3600,
                authorization_expires_at=now + 23 * 3600,
            )
            app = TokenPoolApp.__new__(TokenPoolApp)
            app.config = ClientConfig("http://example.invalid", "key", Path("unused"), "repo")
            app.store = MagicMock()
            app.log = MagicMock()
            app._fresh_renewal = MagicMock()
            with patch(
                "token_pool_client.app.client_preflight",
                return_value={"ok": True},
            ), patch(
                "token_pool_client.app.initialize_refresh",
                return_value=(renewed, {}),
            ) as initialize, patch(
                "token_pool_client.app.create_bundle",
                return_value=b"bundle",
            ), patch(
                "token_pool_client.app.upload_bundle",
                return_value={"ok": True},
            ):
                result = app._renew_and_upload([account], automatic=True)

            self.assertEqual(result.successes, 1)
            initialize.assert_called_once()
            app._fresh_renewal.assert_not_called()
            self.assertTrue(
                any("already captured" in str(call.args[0]) for call in app.log.call_args_list)
            )

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

    def test_sources_menu_click_and_ab_test_upload_label_are_supported(self) -> None:
        page = MagicMock()
        sources = MagicMock()
        with patch("token_pool_client.bootstrap.first_visible", return_value=sources):
            self.assertTrue(click_sources_menu_if_present(page))
        sources.click.assert_called_once()

        item = MagicMock()
        item.is_visible.return_value = True
        item.inner_text.return_value = "Upload from this computer"
        item.get_attribute.return_value = None
        items = MagicMock()
        items.count.return_value = 1
        items.nth.return_value = item
        page.locator.return_value = items
        with patch("token_pool_client.bootstrap.first_visible", return_value=None):
            self.assertIs(explicit_upload_control(page), item)

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

    def test_account_selector_is_not_mistaken_for_required_visible_login(self) -> None:
        self.assertFalse(
            explicit_authentication_required(
                "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
                "Pick an account anas.nmili@mazars.fr",
            )
        )
        self.assertTrue(
            explicit_authentication_required(
                "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
                "Approve sign in request in Microsoft Authenticator",
            )
        )

    def test_remote_silent_only_renewal_never_opens_visible_edge(self) -> None:
        account = ClientAccount(
            account_id="account-test",
            username="tester@mazars.fr",
            tenant_id="tenant",
            object_id="object",
            session_dir="session",
            profile_dir="profile",
            access_expires_at=0,
        )
        app = TokenPoolApp.__new__(TokenPoolApp)
        app.log = MagicMock()
        with patch(
            "token_pool_client.app.bootstrap.renew_microsoft_session",
            side_effect=InteractiveAuthenticationRequired("MFA required"),
        ) as renew:
            with self.assertRaisesRegex(Exception, "MFA required"):
                app._fresh_renewal(account, automatic=True, allow_visible=False)
        renew.assert_called_once()
        self.assertTrue(any("Edge was not opened" in str(call) for call in app.log.call_args_list))

    def test_remote_force_update_installs_reports_and_restarts(self) -> None:
        app = TokenPoolApp.__new__(TokenPoolApp)
        app.store = SimpleNamespace(root=Path("client-root"))
        app.config = ClientConfig("http://example.invalid", "key", Path("unused"), "repo")
        app.root = MagicMock()
        app.log = MagicMock()
        app.shutdown = MagicMock()
        app._finish_admin_command = MagicMock()

        def run_background(_label, work, *, on_complete, show_error):
            on_complete(work(), None)
            return True

        app._background = run_background
        command_id = "a" * 32
        with patch(
            "token_pool_client.app.check_for_update",
            return_value=(True, "token-client-v1.0.11", "token-client-v1.0.12"),
        ) as update, patch(
            "token_pool_client.app.queue_admin_command_result"
        ) as queue, patch(
            "token_pool_client.app.flush_admin_command_results",
            return_value=1,
        ) as flush, patch(
            "token_pool_client.app.restart_after_exit"
        ) as restart:
            app._execute_admin_command(
                {"command_id": command_id, "command": "force_update", "payload": {}}
            )

        update.assert_called_once_with(app.store.root)
        queue.assert_called_once_with(
            command_id=command_id,
            succeeded=True,
            result={
                "changed": True,
                "before": "token-client-v1.0.11",
                "after": "token-client-v1.0.12",
            },
        )
        flush.assert_called_once_with(app.config)
        restart.assert_called_once_with(app.store.root)
        app.root.after.assert_called_once_with(0, app.shutdown)

    def test_restart_handoff_uses_powershell_start_process_trampoline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "Launch-TokenPoolClient.ps1"
            launcher.write_text("param()\n", encoding="utf-8")
            completed = SimpleNamespace(returncode=0)
            with patch(
                "token_pool_client.automation.subprocess.CREATE_NO_WINDOW",
                0,
                create=True,
            ), patch(
                "token_pool_client.automation.subprocess.run",
                return_value=completed,
            ) as run:
                restart_after_exit(root, process_id=12345)

            command = run.call_args.args[0]
            options = run.call_args.kwargs
            self.assertIn("-EncodedCommand", command)
            handoff = json.loads(options["env"]["FMBSM_RESTART_ARGUMENTS"])
            self.assertIn("-WaitForProcessId", handoff)
            self.assertEqual(handoff[-1], "12345")
            self.assertIn(f'"{launcher.resolve()}"', handoff)
            self.assertEqual(options["timeout"], 15)

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

    def test_microsoft_mfa_rejection_is_classified_for_interactive_recovery(self) -> None:
        tenant = "11111111-1111-1111-1111-111111111111"
        presented = _test_token(tenant, "22222222-2222-2222-2222-222222222222", marker="old")
        validator = MicrosoftSessionValidator(
            {tenant},
            oauth_exchanger=lambda actual_tenant, refresh, timeout: (_ for _ in ()).throw(
                OAuthRefreshRejected("AADSTS50078: Presented multi-factor authentication has expired")
            ),
        )
        with self.assertRaises(MicrosoftSessionRejected) as caught:
            validator.validate(_session_files(presented), presented)
        self.assertEqual(caught.exception.microsoft_error_code, "AADSTS50078")
        self.assertTrue(caught.exception.requires_interactive_mfa)

    def test_add_account_retries_once_with_forced_mfa_when_aws_requires_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AccountStore(Path(temporary))
            app = TokenPoolApp.__new__(TokenPoolApp)
            app.store = store
            app.config = ClientConfig("http://example.invalid", "k" * 32, Path("unused"), "repo")
            app.log = MagicMock()
            app._browser_interaction = MagicMock()

            def background(label, work, **kwargs):
                work()
                return True

            app._background = background

            def initialized(session, profile, expected_account=None):
                return (
                    ClientAccount(
                        account_id="account-test",
                        username="tester@mazars.fr",
                        tenant_id="tenant",
                        object_id="object",
                        session_dir=str(session),
                        profile_dir=str(profile),
                        access_expires_at=9999999999,
                        authorization_expires_at=9999999999,
                    ),
                    {},
                )

            mfa_error = ServerRejectedError(
                400,
                {
                    "error": "invalid_bundle",
                    "microsoft_error_code": "AADSTS50078",
                    "action": "interactive_mfa_required",
                },
            )
            with patch("token_pool_client.app.bootstrap.bootstrap_api_session"), patch(
                "token_pool_client.app.bootstrap.renew_microsoft_session"
            ) as renew, patch(
                "token_pool_client.app.initialize_refresh", side_effect=initialized
            ), patch(
                "token_pool_client.app.create_bundle", return_value=b"bundle"
            ), patch(
                "token_pool_client.app.upload_bundle", side_effect=[mfa_error, {"ok": True}]
            ) as upload:
                app.add_account()

            self.assertEqual(upload.call_count, 2)
            self.assertTrue(renew.call_args.kwargs["force_mfa"])
            self.assertEqual(store.load()[0].username, "tester@mazars.fr")

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
