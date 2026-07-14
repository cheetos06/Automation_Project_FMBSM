from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from fmbsm_email_bot.zip_utils import safe_extract_files  # noqa: E402
from token_pool_client.bundle import create_bundle  # noqa: E402
from token_pool_client.storage import ClientAccount  # noqa: E402
from token_pool_client.transport_crypto import encrypt_bundle  # noqa: E402
from copilot_service.transport_crypto import EnvelopeError, decrypt_envelope, load_private_key  # noqa: E402


class InputSafetyTests(unittest.TestCase):
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
