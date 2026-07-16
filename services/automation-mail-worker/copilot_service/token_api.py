from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import threading
import time
import uuid
from collections import defaultdict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .job_status import JobStatusStore
from .registry import CopilotRegistry, default_data_dir
from .session_bundle import MAX_BUNDLE_BYTES, BundleValidationError, install_bundle
from .transport_crypto import (
    ENVELOPE_CONTENT_TYPE,
    MAX_ENVELOPE_BYTES,
    EnvelopeError,
    decrypt_envelope,
    load_private_key,
)
from .upload_validation import MicrosoftSessionValidator, SessionProofUnavailable


LOGGER = logging.getLogger("copilot-token-api")
SERVER_VERSION = "1.0.0"
TOKEN_CLIENT_DOWNLOAD_PREFIX = "/downloads/token-client/"
TOKEN_CLIENT_TAG = re.compile(r"token-client-v[0-9]+\.[0-9]+\.[0-9]+(?:[-A-Za-z0-9.]*)?\Z")
TOKEN_CLIENT_ASSET = re.compile(
    r"TokenPoolClient-(?:"
    r"app-win-x64\.zip(?:\.sha256)?|"
    r"win-x64\.zip(?:\.sha256|\.part[0-9]{3})?|"
    r"release\.json"
    r")\Z"
)
MAX_CLIENT_EVENT_BYTES = 4096
CLIENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
CLIENT_EVENT_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
CLIENT_ACCOUNT_ID_PATTERN = re.compile(r"account-[A-Za-z0-9._-]{1,80}\Z")


class SlidingWindowRateLimiter:
    def __init__(self, maximum: int, window_seconds: float = 60) -> None:
        self.maximum = maximum
        self.window_seconds = window_seconds
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            entries = self._entries[key]
            while entries and entries[0] < now - self.window_seconds:
                entries.popleft()
            if len(entries) >= self.maximum:
                return False
            entries.append(now)
            return True


class TokenApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        registry: CopilotRegistry,
        upload_key: str,
        status_store: JobStatusStore,
        requests_per_minute: int,
        transport_private_key: Any,
        session_validator: MicrosoftSessionValidator,
        maximum_accounts: int,
        artifact_root: Path,
        artifact_requests_per_hour: int,
    ) -> None:
        super().__init__(address, TokenApiHandler)
        self.registry = registry
        self.upload_key = upload_key
        self.status_store = status_store
        self.rate_limiter = SlidingWindowRateLimiter(requests_per_minute)
        self.transport_private_key = transport_private_key
        self.session_validator = session_validator
        self.maximum_accounts = maximum_accounts
        self.artifact_root = artifact_root
        self.artifact_rate_limiter = SlidingWindowRateLimiter(
            artifact_requests_per_hour,
            window_seconds=3600,
        )
        self.install_lock = threading.Lock()
        self._used_nonces: dict[str, float] = {}
        self._nonce_lock = threading.Lock()
        self.started_at = time.time()

    def consume_nonce(self, nonce: str, timestamp: float) -> bool:
        now = time.time()
        with self._nonce_lock:
            self._used_nonces = {
                value: created for value, created in self._used_nonces.items()
                if created >= now - 600
            }
            if nonce in self._used_nonces:
                return False
            self._used_nonces[nonce] = timestamp
            return True


class TokenApiHandler(BaseHTTPRequestHandler):
    server: TokenApiServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if self._serve_token_client_artifact(path, head_only=False):
            return
        if path == "/health":
            status = self.server.registry.status()
            jobs = self.server.status_store.recent(limit=5)
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "version": SERVER_VERSION,
                    "uptime_seconds": round(time.time() - self.server.started_at, 1),
                    "account_count": status["account_count"],
                    "available_account_count": status["available_account_count"],
                    "recent_turns": status["recent_turns"],
                    "jobs": [_public_job(job) for job in jobs],
                },
            )
            return
        if path == "/v1/status":
            if not self._authenticated(b""):
                return
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "version": SERVER_VERSION,
                    "pool": self.server.registry.status(),
                    "jobs": [
                        _public_job(job)
                        for job in self.server.status_store.recent(limit=20)
                    ],
                },
            )
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/v1/client-events":
            self._receive_client_event()
            return
        if path != "/v1/accounts/session":
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        if not self.server.rate_limiter.allow(self.client_address[0]):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "rate_limited"})
            return
        request_id = uuid.uuid4().hex[:16]
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError:
            self._json(HTTPStatus.LENGTH_REQUIRED, {"ok": False, "error": "invalid_content_length"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        maximum_length = MAX_ENVELOPE_BYTES if content_type == ENVELOPE_CONTENT_TYPE else MAX_BUNDLE_BYTES
        if length <= 0 or length > maximum_length:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "bundle_too_large"})
            return
        self.connection.settimeout(60)
        payload = self.rfile.read(length)
        if len(payload) != length:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "incomplete_body"})
            return
        if not self._authenticated(payload):
            return
        if content_type == ENVELOPE_CONTENT_TYPE:
            try:
                payload = decrypt_envelope(payload, self.server.transport_private_key)
            except EnvelopeError as exc:
                LOGGER.warning(
                    "Rejected encrypted bundle request_id=%s remote=%s reason=%s",
                    request_id,
                    self.client_address[0],
                    exc,
                )
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "request_id": request_id, "error": "invalid_envelope"},
                )
                return
            if len(payload) > MAX_BUNDLE_BYTES:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "bundle_too_large"})
                return
        elif not isinstance(self.connection, ssl.SSLSocket):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "encryption_required"})
            return
        try:
            with self.server.install_lock:
                installed = install_bundle(
                    payload,
                    self.server.registry,
                    remote_address=self.client_address[0],
                    session_validator=self.server.session_validator.validate,
                    maximum_accounts=self.server.maximum_accounts,
                )
        except SessionProofUnavailable as exc:
            LOGGER.error("Microsoft session proof unavailable request_id=%s reason=%s", request_id, exc)
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "request_id": request_id, "error": "session_proof_unavailable"},
            )
            return
        except BundleValidationError as exc:
            LOGGER.warning("Rejected bundle request_id=%s remote=%s reason=%s", request_id, self.client_address[0], exc)
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "request_id": request_id, "error": "invalid_bundle", "detail": str(exc)},
            )
            return
        except Exception:
            LOGGER.exception("Bundle install failed request_id=%s", request_id)
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "request_id": request_id, "error": "install_failed"},
            )
            return
        LOGGER.info(
            "Installed account session request_id=%s account=%s username=%s expires=%s files=%s",
            request_id,
            installed.account.account_id,
            installed.account.username,
            installed.account.access_expires_at,
            installed.file_count,
        )
        self._json(
            HTTPStatus.CREATED,
            {
                "ok": True,
                "request_id": request_id,
                "account": installed.account.as_public_dict(),
                "bundle_sha256": installed.bundle_sha256,
                "file_count": installed.file_count,
                "pool": self.server.registry.status(),
            },
        )

    def _receive_client_event(self) -> None:
        """Record one signed diagnostic event without receiving credentials or tokens."""

        rate_key = f"client-event:{self.client_address[0]}"
        if not self.server.rate_limiter.allow(rate_key):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "rate_limited"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(HTTPStatus.LENGTH_REQUIRED, {"ok": False, "error": "invalid_content_length"})
            return
        if length <= 0 or length > MAX_CLIENT_EVENT_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "event_too_large"})
            return
        self.connection.settimeout(10)
        body = self.rfile.read(length)
        if len(body) != length:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "incomplete_body"})
            return
        if not self._authenticated(body):
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_event_json"})
            return
        if not isinstance(payload, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_event"})
            return

        client_id = str(payload.get("client_id") or "").lower()
        event = str(payload.get("event") or "").lower()
        app_version = str(payload.get("app_version") or "")[:64] or None
        scheduled_slot = str(payload.get("scheduled_slot") or "")[:64] or None
        try:
            observed_at = float(payload.get("observed_at"))
        except (TypeError, ValueError):
            observed_at = 0.0
        raw_account_ids = payload.get("account_ids")
        account_ids = (
            list(dict.fromkeys(str(value) for value in raw_account_ids))
            if isinstance(raw_account_ids, list)
            else []
        )
        valid = bool(
            CLIENT_ID_PATTERN.fullmatch(client_id)
            and CLIENT_EVENT_PATTERN.fullmatch(event)
            and observed_at > 0
            and abs(time.time() - observed_at) <= 2 * 24 * 60 * 60
            and len(account_ids) <= 20
            and all(CLIENT_ACCOUNT_ID_PATTERN.fullmatch(value) for value in account_ids)
            and (scheduled_slot is None or not any(ord(character) < 32 for character in scheduled_slot))
        )
        if not valid:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_event"})
            return
        event_id = self.server.registry.record_client_event(
            client_id=client_id,
            observed_at=observed_at,
            event=event,
            scheduled_slot=scheduled_slot,
            app_version=app_version,
            account_ids=account_ids,
            remote_address=self.client_address[0],
        )
        LOGGER.info(
            "Recorded client event id=%s client=%s event=%s slot=%s accounts=%s remote=%s",
            event_id,
            client_id[:10],
            event,
            scheduled_slot or "-",
            len(account_ids),
            self.client_address[0],
        )
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "event_id": event_id,
                "pool": self.server.registry.status(),
            },
        )

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if self._serve_token_client_artifact(path, head_only=True):
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", "0")
        self.send_header("X-FMBSM-Token-API-Version", SERVER_VERSION)
        self.end_headers()

    def _serve_token_client_artifact(self, path: str, *, head_only: bool) -> bool:
        if not path.startswith(TOKEN_CLIENT_DOWNLOAD_PREFIX):
            return False
        relative = path[len(TOKEN_CLIENT_DOWNLOAD_PREFIX):]
        parts = relative.split("/")
        if (
            len(parts) != 2
            or not TOKEN_CLIENT_TAG.fullmatch(parts[0])
            or not TOKEN_CLIENT_ASSET.fullmatch(parts[1])
        ):
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return True
        if not self.server.artifact_rate_limiter.allow(self.client_address[0]):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "rate_limited"})
            return True

        root = self.server.artifact_root.resolve()
        candidate = root.joinpath(*parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, OSError, ValueError):
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return True
        if not resolved.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return True

        length = resolved.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-FMBSM-Token-API-Version", SERVER_VERSION)
        self.end_headers()
        if not head_only:
            with resolved.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    self.wfile.write(chunk)
        return True

    def _authenticated(self, body: bytes) -> bool:
        timestamp_text = self.headers.get("X-FMBSM-Timestamp", "")
        nonce = self.headers.get("X-FMBSM-Nonce", "")
        signature = self.headers.get("X-FMBSM-Signature", "")
        try:
            timestamp = float(timestamp_text)
        except ValueError:
            timestamp = 0.0
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if (
            len(nonce) == 32
            and all(character in "0123456789abcdef" for character in nonce.lower())
            and abs(time.time() - timestamp) <= 300
            and len(signature) == 64
        ):
            canonical = "\n".join(
                (
                    timestamp_text,
                    nonce,
                    self.command.upper(),
                    path,
                    hashlib.sha256(body).hexdigest(),
                )
            ).encode("utf-8")
            expected = hmac.new(
                self.server.upload_key.encode("utf-8"),
                canonical,
                hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(signature.lower(), expected) and self.server.consume_nonce(nonce, timestamp):
                return True

        # Preserve compatibility with the first pinned-HTTPS client, but never
        # accept the raw persistent key over plaintext HTTP.
        provided = self.headers.get("X-FMBSM-Upload-Key", "")
        direct_key_allowed = isinstance(self.connection, ssl.SSLSocket) and hmac.compare_digest(
            provided.encode("utf-8"), self.server.upload_key.encode("utf-8")
        )
        if not direct_key_allowed:
            self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return False
        return True

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-FMBSM-Token-API-Version", SERVER_VERSION)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("remote=%s %s", self.client_address[0], format % args)


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: job.get(key)
        for key in ("job_id", "kind", "stage", "message", "started_at", "updated_at", "finished_at")
        if key in job
    }


def _load_environment() -> Path:
    project = Path(__file__).resolve().parents[1]
    env_file = Path(os.getenv("ENV_FILE") or project / ".env")
    load_dotenv(env_file, encoding="utf-8-sig")
    return project


def main() -> int:
    parser = argparse.ArgumentParser(description="Pinned-HTTPS Copilot session upload API")
    parser.add_argument("--insecure-http", action="store_true", help="Testing only: do not enable TLS")
    parser.add_argument("--port", type=int, help="Override TOKEN_API_PORT (used by the encrypted HTTP fallback)")
    args = parser.parse_args()
    project = _load_environment()
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    upload_key = os.getenv("COPILOT_UPLOAD_KEY", "").strip()
    if len(upload_key) < 32:
        raise RuntimeError("COPILOT_UPLOAD_KEY must contain at least 32 characters")
    host = os.getenv("TOKEN_API_HOST", "0.0.0.0")
    port = args.port or int(os.getenv("TOKEN_API_PORT", "443"))
    registry = CopilotRegistry.from_env()
    allowed_tenants = {
        value.strip().lower()
        for value in os.getenv("COPILOT_ALLOWED_TENANT_IDS", "").split(",")
        if value.strip()
    }
    if not allowed_tenants:
        raise RuntimeError("COPILOT_ALLOWED_TENANT_IDS must contain at least one tenant ID")
    maximum_accounts = max(1, int(os.getenv("COPILOT_MAX_ACCOUNTS", "20")))
    artifact_root = Path(
        os.getenv("TOKEN_CLIENT_ARTIFACT_DIR")
        or (project / "data" / "client-artifacts")
    )
    status_dir = Path(os.getenv("JOB_STATUS_DIR") or (project / "data" / "job-status"))
    certificate = Path(os.getenv("TOKEN_API_CERT_FILE") or (project / "data" / "tls" / "server.crt"))
    private_key = Path(os.getenv("TOKEN_API_KEY_FILE") or (project / "data" / "tls" / "server.key"))
    server = TokenApiServer(
        (host, port),
        registry=registry,
        upload_key=upload_key,
        status_store=JobStatusStore(status_dir),
        requests_per_minute=max(1, int(os.getenv("TOKEN_API_REQUESTS_PER_MINUTE", "12"))),
        transport_private_key=load_private_key(private_key),
        session_validator=MicrosoftSessionValidator(allowed_tenants),
        maximum_accounts=maximum_accounts,
        artifact_root=artifact_root,
        artifact_requests_per_hour=max(
            20,
            int(os.getenv("TOKEN_CLIENT_DOWNLOADS_PER_HOUR", "120")),
        ),
    )
    insecure = args.insecure_http or os.getenv("TOKEN_API_INSECURE_HTTP", "").lower() in {"1", "true", "yes"}
    if not insecure:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certificate, private_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    LOGGER.info("Starting token API host=%s port=%s tls=%s data=%s", host, port, not insecure, default_data_dir())
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
