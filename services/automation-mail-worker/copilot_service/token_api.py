from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import uuid
from collections import defaultdict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv

from .admin_ping import CopilotPingManager
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
from .upload_validation import MicrosoftSessionRejected, MicrosoftSessionValidator, SessionProofUnavailable


LOGGER = logging.getLogger("copilot-token-api")
SERVER_VERSION = "1.5.1"
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
MAX_CONTROL_BODY_BYTES = 32768
ADMIN_ENVELOPE_CONTENT_TYPE = "application/vnd.fmbsm.admin+aesgcm"
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
        admin_key: str = "",
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
        self.admin_key = admin_key
        self.ping_manager = CopilotPingManager(registry, status_store)
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
        self._admin_response_nonce = ""
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
                    "server_time": time.time(),
                    # Legacy clients can still prove connectivity, but a shared
                    # upload credential must never reveal the global account pool.
                    "pool": {
                        "scope": "client_identification_required",
                        "account_count": 0,
                        "available_account_count": 0,
                        "recent_turns": 0,
                        "accounts": [],
                    },
                },
            )
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        self._admin_response_nonce = ""
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/v1/client-events":
            self._receive_client_event()
            return
        if path == "/v1/client/status":
            self._client_status()
            return
        if path == "/v1/client/commands/poll":
            self._poll_client_commands()
            return
        if path == "/v1/client/commands/complete":
            self._complete_client_command()
            return
        if path == "/v1/admin/snapshot":
            self._admin_snapshot()
            return
        if path == "/v1/admin/commands":
            self._admin_create_commands()
            return
        if path == "/v1/admin/commands/cancel":
            self._admin_cancel_command()
            return
        if path == "/v1/admin/clients/forget":
            self._admin_forget_clients()
            return
        if path == "/v1/admin/copilot-tests":
            self._admin_start_copilot_test()
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
            detail = str(exc)
            response: dict[str, Any] = {
                "ok": False,
                "request_id": request_id,
                "error": "invalid_bundle",
                "detail": detail,
            }
            if isinstance(exc, MicrosoftSessionRejected):
                response["microsoft_error_code"] = exc.microsoft_error_code
                if exc.requires_interactive_mfa:
                    response["action"] = "interactive_mfa_required"
                    response["detail"] = (
                        "Microsoft requires fresh multi-factor authentication for Copilot. "
                        "Complete the new sign-in in the desktop token app."
                    )
            self._json(
                HTTPStatus.BAD_REQUEST,
                response,
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
                "client_status": _client_status_payload(
                    self.server.registry,
                    [installed.account.account_id],
                ),
                "pool": _legacy_scoped_pool(
                    self.server.registry,
                    [installed.account.account_id],
                ),
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
        raw_status = payload.get("status")
        client_status = raw_status if isinstance(raw_status, dict) else {}
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
            status=client_status,
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
                "client_status": _client_status_payload(
                    self.server.registry,
                    account_ids,
                ),
                "pool": _legacy_scoped_pool(
                    self.server.registry,
                    account_ids,
                ),
            },
        )

    def _client_status(self) -> None:
        parsed = self._read_json_body(MAX_CONTROL_BODY_BYTES)
        if parsed is None:
            return
        body, payload = parsed
        if not self._authenticated(body):
            return
        presence = self._validated_client_presence(payload, event="status_check")
        if presence is None:
            return
        self.server.registry.update_client_presence(
            **presence,
            remote_address=self.client_address[0],
        )
        self._json(
            HTTPStatus.OK,
            _client_status_payload(
                self.server.registry,
                presence["account_ids"],
            ),
        )

    def _poll_client_commands(self) -> None:
        parsed = self._read_json_body(MAX_CONTROL_BODY_BYTES)
        if parsed is None:
            return
        body, payload = parsed
        if not self._authenticated(body):
            return
        presence = self._validated_client_presence(payload, event="heartbeat")
        if presence is None:
            return
        self.server.registry.update_client_presence(
            **presence,
            remote_address=self.client_address[0],
        )
        command = self.server.registry.poll_client_command(
            presence["client_id"],
            lease_seconds=10 * 60,
        )
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "server_time": time.time(),
                "poll_after_seconds": 60,
                "client_status": _client_status_payload(
                    self.server.registry,
                    presence["account_ids"],
                ),
                "command": command,
            },
        )

    def _complete_client_command(self) -> None:
        parsed = self._read_json_body(MAX_CONTROL_BODY_BYTES)
        if parsed is None:
            return
        body, payload = parsed
        if not self._authenticated(body):
            return
        client_id = str(payload.get("client_id") or "").lower()
        command_id = str(payload.get("command_id") or "").lower()
        result = payload.get("result")
        succeeded = payload.get("succeeded")
        if not (
            CLIENT_ID_PATTERN.fullmatch(client_id)
            and re.fullmatch(r"[0-9a-f]{32}", command_id)
            and isinstance(succeeded, bool)
            and isinstance(result, dict)
        ):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_completion"})
            return
        completed = self.server.registry.complete_client_command(
            client_id=client_id,
            command_id=command_id,
            succeeded=succeeded,
            result=result,
        )
        if not completed:
            self._json(HTTPStatus.CONFLICT, {"ok": False, "error": "command_not_active"})
            return
        self._json(HTTPStatus.OK, {"ok": True, "command_id": command_id})

    def _admin_snapshot(self) -> None:
        payload = self._read_admin_body()
        if payload is None:
            return
        registry = self.server.registry
        now = time.time()
        account_records = registry.list_accounts(enabled_only=False)
        account_by_id = {record.account_id: record for record in account_records}
        turn_usage = registry.turn_usage(now=now)
        accounts: list[dict[str, Any]] = []
        for record in account_records:
            value = record.as_public_dict(now=now)
            value.update(
                {
                    "account_id": record.account_id,
                    "username": record.username,
                    "last_error": record.last_error,
                    "uploaded_at": record.uploaded_at,
                    **turn_usage.get(
                        record.account_id,
                        {"turns_last_hour": 0, "turns_last_24_hours": 0},
                    ),
                }
            )
            if (
                record.access_expires_at <= now + 60
                and record.last_error
                and record.last_error.startswith("token_refresh_failed:")
            ):
                value["runtime_available"] = False
            accounts.append(value)

        commands = registry.list_client_commands(limit=150)
        active_by_client: dict[str, int] = {}
        for command in commands:
            if command["status"] in {"queued", "dispatched"}:
                client_id = str(command["client_id"])
                active_by_client[client_id] = active_by_client.get(client_id, 0) + 1
        client_records, suppressed_duplicate_count = _without_registration_race_duplicates(
            registry.list_clients()
        )
        clients: list[dict[str, Any]] = []
        for client in client_records:
            account_names = [
                account_by_id[account_id].username
                for account_id in client["account_ids"]
                if account_id in account_by_id
            ]
            item = dict(client)
            item["account_usernames"] = account_names
            item["online"] = now - float(client["last_seen_at"]) <= 150
            item["seconds_since_seen"] = round(max(0.0, now - float(client["last_seen_at"])), 1)
            item["active_command_count"] = active_by_client.get(str(client["client_id"]), 0)
            live_status = _client_status_payload(
                registry,
                client["account_ids"],
                now=now,
                records_by_id=account_by_id,
            )
            # Client activity comes from its roughly one-minute heartbeat. AWS
            # is authoritative for upload/readiness and changes immediately
            # after an upload, so expose that state separately for the admin UI.
            item["server_accounts"] = live_status["accounts"]
            item["server_summary"] = live_status["summary"]
            clients.append(item)
        pool_status = registry.status()
        # The registry's general availability count is intentionally optimistic when a
        # refresh token has not yet been exercised.  The admin view has stronger evidence:
        # a recorded refresh failure means the account is not currently usable.
        pool_status["available_account_count"] = sum(
            bool(account.get("runtime_available")) for account in accounts
        )
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "now": now,
                "server": _server_metrics(self.server.started_at),
                "pool": {
                    **pool_status,
                    "accounts": accounts,
                },
                "clients": clients,
                "suppressed_duplicate_client_count": suppressed_duplicate_count,
                "commands": commands,
                "client_events": registry.recent_client_events(limit=100),
                "copilot_tests": self.server.ping_manager.recent(limit=20),
                "jobs": [
                    _public_job(job)
                    for job in self.server.status_store.recent(limit=20)
                ],
            },
        )

    def _admin_create_commands(self) -> None:
        payload = self._read_admin_body()
        if payload is None:
            return
        raw_client_ids = payload.get("client_ids")
        client_ids = (
            list(dict.fromkeys(str(value).lower() for value in raw_client_ids))
            if isinstance(raw_client_ids, list)
            else []
        )
        command = str(payload.get("command") or "")
        command_payload = payload.get("payload")
        try:
            expires_in = float(payload.get("expires_in_seconds") or 15 * 60)
        except (TypeError, ValueError):
            expires_in = 0.0
        if not (
            1 <= len(client_ids) <= 20
            and all(CLIENT_ID_PATTERN.fullmatch(value) for value in client_ids)
            and command in {"force_renew", "force_update"}
            and isinstance(command_payload, dict)
            and 60 <= expires_in <= 24 * 60 * 60
        ):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_command"})
            return
        if command == "force_renew" and command_payload.get("interaction") not in {
            "silent_only",
            "allow_visible",
        }:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_interaction_mode"})
            return
        now = time.time()
        clients = {item["client_id"]: item for item in self.server.registry.list_clients()}
        created: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        for client_id in client_ids:
            client = clients.get(client_id)
            if client is None:
                rejected.append({"client_id": client_id, "reason": "unknown_client"})
                continue
            if now - float(client["last_seen_at"]) > 150:
                rejected.append({"client_id": client_id, "reason": "client_offline"})
                continue
            created.append(
                self.server.registry.create_client_command(
                    client_id=client_id,
                    command=command,
                    payload=command_payload,
                    expires_in_seconds=expires_in,
                    requested_by="token-pool-admin",
                )
            )
        self._json(
            HTTPStatus.OK,
            {"ok": True, "created": created, "rejected": rejected},
        )

    def _admin_cancel_command(self) -> None:
        payload = self._read_admin_body()
        if payload is None:
            return
        command_id = str(payload.get("command_id") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}", command_id):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_command_id"})
            return
        cancelled = self.server.registry.cancel_client_command(command_id)
        self._json(HTTPStatus.OK, {"ok": True, "cancelled": cancelled})

    def _admin_forget_clients(self) -> None:
        payload = self._read_admin_body()
        if payload is None:
            return
        raw_client_ids = payload.get("client_ids")
        client_ids = (
            list(dict.fromkeys(str(value).lower() for value in raw_client_ids))
            if isinstance(raw_client_ids, list)
            else []
        )
        if not (
            1 <= len(client_ids) <= 20
            and all(CLIENT_ID_PATTERN.fullmatch(value) for value in client_ids)
        ):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_client_selection"})
            return
        offline_before = time.time() - 150
        forgotten: list[str] = []
        rejected: list[dict[str, str]] = []
        for client_id in client_ids:
            removed, reason = self.server.registry.forget_unmapped_client(
                client_id,
                offline_before=offline_before,
            )
            if removed:
                forgotten.append(client_id)
            else:
                rejected.append({"client_id": client_id, "reason": reason})
        self._json(
            HTTPStatus.OK,
            {"ok": True, "forgotten": forgotten, "rejected": rejected},
        )

    def _admin_start_copilot_test(self) -> None:
        payload = self._read_admin_body()
        if payload is None:
            return
        raw_account_ids = payload.get("account_ids")
        account_ids = (
            list(dict.fromkeys(str(value) for value in raw_account_ids))
            if isinstance(raw_account_ids, list)
            else []
        )
        if not (
            1 <= len(account_ids) <= 20
            and all(CLIENT_ACCOUNT_ID_PATTERN.fullmatch(value) for value in account_ids)
        ):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_account_selection"})
            return
        try:
            job = self.server.ping_manager.start(account_ids)
        except ValueError as exc:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_account_selection", "detail": str(exc)},
            )
            return
        except RuntimeError as exc:
            self._json(
                HTTPStatus.CONFLICT,
                {"ok": False, "error": "copilot_test_already_running", "detail": str(exc)},
            )
            return
        self._json(HTTPStatus.ACCEPTED, {"ok": True, "job": job})

    def _read_admin_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(HTTPStatus.LENGTH_REQUIRED, {"ok": False, "error": "invalid_content_length"})
            return None
        if length < 12 + 16 or length > MAX_CONTROL_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "request_too_large"})
            return None
        self.connection.settimeout(15)
        body = self.rfile.read(length)
        if len(body) != length:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "incomplete_body"})
            return None
        if not self._admin_authenticated(body):
            return None
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != ADMIN_ENVELOPE_CONTENT_TYPE:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "admin_encryption_required"})
            return None
        nonce, ciphertext = body[:12], body[12:]
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            plaintext = AESGCM(_admin_encryption_key(self.server.admin_key)).decrypt(
                nonce,
                ciphertext,
                f"request\n{self.command.upper()}\n{path}".encode("utf-8"),
            )
            payload = json.loads(plaintext.decode("utf-8"))
        except Exception:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_admin_envelope"})
            return None
        if not isinstance(payload, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json_object"})
            return None
        return payload

    def _read_json_body(self, maximum_bytes: int) -> tuple[bytes, dict[str, Any]] | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(HTTPStatus.LENGTH_REQUIRED, {"ok": False, "error": "invalid_content_length"})
            return None
        if length < 2 or length > maximum_bytes:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "request_too_large"})
            return None
        self.connection.settimeout(15)
        body = self.rfile.read(length)
        if len(body) != length:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "incomplete_body"})
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return None
        if not isinstance(payload, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json_object"})
            return None
        return body, payload

    def _validated_client_presence(
        self,
        payload: dict[str, Any],
        *,
        event: str,
    ) -> dict[str, Any] | None:
        client_id = str(payload.get("client_id") or "").lower()
        app_version = str(payload.get("app_version") or "")[:64] or None
        scheduled_slot = str(payload.get("scheduled_slot") or "")[:64] or None
        try:
            observed_at = float(payload.get("observed_at"))
        except (TypeError, ValueError):
            observed_at = 0.0
        raw_ids = payload.get("account_ids")
        account_ids = (
            list(dict.fromkeys(str(value) for value in raw_ids))
            if isinstance(raw_ids, list)
            else []
        )
        raw_status = payload.get("status")
        status = raw_status if isinstance(raw_status, dict) else {}
        if not (
            CLIENT_ID_PATTERN.fullmatch(client_id)
            and observed_at > 0
            and abs(time.time() - observed_at) <= 10 * 60
            and len(account_ids) <= 20
            and all(CLIENT_ACCOUNT_ID_PATTERN.fullmatch(value) for value in account_ids)
        ):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_client_presence"})
            return None
        return {
            "client_id": client_id,
            "observed_at": observed_at,
            "event": event,
            "scheduled_slot": scheduled_slot,
            "app_version": app_version,
            "account_ids": account_ids,
            "status": status,
        }

    def do_HEAD(self) -> None:  # noqa: N802
        self._admin_response_nonce = ""
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

    def _admin_authenticated(self, body: bytes) -> bool:
        if len(self.server.admin_key) < 32:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "admin_api_disabled"})
            return False
        timestamp_text = self.headers.get("X-FMBSM-Admin-Timestamp", "")
        nonce = self.headers.get("X-FMBSM-Admin-Nonce", "")
        signature = self.headers.get("X-FMBSM-Admin-Signature", "")
        try:
            timestamp = float(timestamp_text)
        except ValueError:
            timestamp = 0.0
        path = urlsplit(self.path).path.rstrip("/") or "/"
        valid_shape = bool(
            len(nonce) == 32
            and all(character in "0123456789abcdef" for character in nonce.lower())
            and abs(time.time() - timestamp) <= 300
            and len(signature) == 64
        )
        if valid_shape:
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
                self.server.admin_key.encode("utf-8"),
                canonical,
                hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(signature.lower(), expected) and self.server.consume_nonce(nonce, timestamp):
                self._admin_response_nonce = nonce
                return True
        self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
        return False

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        plaintext = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        response_nonce = getattr(self, "_admin_response_nonce", "")
        if response_nonce:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            nonce = os.urandom(12)
            body = nonce + AESGCM(_admin_encryption_key(self.server.admin_key)).encrypt(
                nonce,
                plaintext,
                f"response\n{int(status)}\n{path}\n{response_nonce}".encode("utf-8"),
            )
            content_type = ADMIN_ENVELOPE_CONTENT_TYPE
        else:
            body = plaintext
            content_type = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-FMBSM-Token-API-Version", SERVER_VERSION)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("remote=%s %s", self.client_address[0], format % args)


def _client_status_payload(
    registry: CopilotRegistry,
    account_ids: list[str],
    *,
    now: float | None = None,
    records_by_id: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    requested_ids = list(dict.fromkeys(account_ids))
    records = records_by_id
    if records is None:
        records = {
            record.account_id: record
            for record in registry.list_accounts(enabled_only=False)
            if record.account_id in requested_ids
        }
    accounts: list[dict[str, Any]] = []
    for account_id in requested_ids:
        record = records.get(account_id)
        if record is None:
            accounts.append(
                {
                    "account_id": account_id,
                    "uploaded": False,
                    "ready": False,
                    "state": "not_uploaded",
                    "uploaded_at": None,
                    "authorization_expires_at": None,
                }
            )
            continue
        public = record.as_public_dict(now=current)
        ready = bool(public.get("runtime_available"))
        if (
            record.access_expires_at <= current + 60
            and record.last_error
            and record.last_error.startswith("token_refresh_failed:")
        ):
            ready = False
        if not record.enabled:
            state = "disabled"
        elif public.get("cooling_down"):
            state = "cooldown"
        elif ready:
            state = "ready"
        else:
            state = "renewal_required"
        accounts.append(
            {
                "account_id": account_id,
                "uploaded": True,
                "ready": ready,
                "state": state,
                "uploaded_at": record.uploaded_at,
                "authorization_expires_at": record.refresh_expires_at,
                "access_expires_at": record.access_expires_at,
                "cooldown_until": record.cooldown_until if public.get("cooling_down") else None,
            }
        )
    uploaded = sum(bool(account["uploaded"]) for account in accounts)
    ready = sum(bool(account["ready"]) for account in accounts)
    return {
        "ok": True,
        "version": SERVER_VERSION,
        "server_time": current,
        "connection": "online",
        "summary": {
            "configured_account_count": len(requested_ids),
            "uploaded_account_count": uploaded,
            "ready_account_count": ready,
            "renewal_required_count": sum(
                bool(account["uploaded"]) and not bool(account["ready"])
                for account in accounts
            ),
            "missing_account_count": len(accounts) - uploaded,
        },
        "accounts": accounts,
    }


def _without_registration_race_duplicates(
    clients: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Hide only provable one-shot aliases created by the old first-run race.

    A real second installation must not disappear merely because it uses the same
    account or public IP. The old race has a much stronger fingerprint: two IDs
    for the same non-empty account set appear within a few milliseconds, while
    only one ID is ever seen again. Version and remote address are intentionally
    excluded because the surviving client updates both fields over time.
    """

    one_shot_tolerance = 0.001
    registration_race_window = 0.05

    def account_set(client: dict[str, Any]) -> tuple[str, ...]:
        return tuple(sorted(str(value) for value in client.get("account_ids") or []))

    continued = [
        client
        for client in clients
        if float(client.get("last_seen_at") or 0.0)
        > float(client.get("first_seen_at") or 0.0) + one_shot_tolerance
    ]

    def is_provable_alias(client: dict[str, Any]) -> bool:
        first_seen = float(client.get("first_seen_at") or 0.0)
        last_seen = float(client.get("last_seen_at") or 0.0)
        accounts = account_set(client)
        if not accounts or abs(last_seen - first_seen) > one_shot_tolerance:
            return False
        return any(
            account_set(survivor) == accounts
            and abs(float(survivor.get("first_seen_at") or 0.0) - first_seen)
            <= registration_race_window
            for survivor in continued
        )

    visible = [client for client in clients if not is_provable_alias(client)]
    return visible, len(clients) - len(visible)


def _legacy_scoped_pool(
    registry: CopilotRegistry,
    account_ids: list[str],
) -> dict[str, Any]:
    status = _client_status_payload(registry, account_ids)
    summary = status["summary"]
    return {
        "scope": "current_client",
        "now": status["server_time"],
        "account_count": summary["uploaded_account_count"],
        "available_account_count": summary["ready_account_count"],
        "recent_turns": 0,
        "accounts": status["accounts"],
    }


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: job.get(key)
        for key in ("job_id", "kind", "stage", "message", "started_at", "updated_at", "finished_at")
        if key in job
    }


def _server_metrics(process_started_at: float) -> dict[str, Any]:
    now = time.time()
    try:
        system_uptime = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        system_uptime = None
    try:
        load_average = [round(value, 2) for value in os.getloadavg()]
    except (AttributeError, OSError):
        load_average = []
    memory: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, raw = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                memory[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        memory = {}
    try:
        disk = shutil.disk_usage("/")
        disk_value = {"total": disk.total, "used": disk.used, "free": disk.free}
    except OSError:
        disk_value = {}

    services: dict[str, str] = {}
    for name in (
        "fmbsm-token-api.service",
        "fmbsm-token-api-http.service",
        "fmbsm-email-bot.service",
    ):
        try:
            completed = subprocess.run(
                ["systemctl", "is-active", name],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            services[name] = (completed.stdout.strip() or "inactive")[:40]
        except (OSError, subprocess.SubprocessError):
            services[name] = "unknown"
    return {
        "host": socket.gethostname(),
        "server_time": now,
        "api_uptime_seconds": round(max(0.0, now - process_started_at), 1),
        "system_uptime_seconds": round(system_uptime, 1) if system_uptime is not None else None,
        "load_average": load_average,
        "memory": {
            "total": memory.get("MemTotal"),
            "available": memory.get("MemAvailable"),
        },
        "disk": disk_value,
        "services": services,
    }


def _admin_encryption_key(admin_key: str) -> bytes:
    return hashlib.sha256(
        b"fmbsm-admin-envelope-v1\0" + admin_key.encode("utf-8")
    ).digest()


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
    admin_key = os.getenv("TOKEN_ADMIN_KEY", "").strip()
    if admin_key and len(admin_key) < 32:
        raise RuntimeError("TOKEN_ADMIN_KEY must be empty or contain at least 32 characters")
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
        admin_key=admin_key,
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
