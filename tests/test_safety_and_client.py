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

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


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
    configured_refresh_times,
    is_automatic_work_account,
    latest_due_slot,
    slot_key,
)
from token_pool_client.refresh import (  # noqa: E402
    RefreshError,
    decrypt_captured_msal,
    requires_interactive_reauthentication,
)
from token_pool_client.storage import ClientAccount  # noqa: E402
from token_pool_client.transport_crypto import encrypt_bundle  # noqa: E402
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
            self.assertIn("manifest.json", names)
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
