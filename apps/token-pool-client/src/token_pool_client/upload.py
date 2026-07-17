from __future__ import annotations

import json
import hashlib
import hmac
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .storage import application_dir, executable_dir
from .transport_crypto import ENVELOPE_CONTENT_TYPE, encrypt_bundle


@dataclass(frozen=True)
class ClientConfig:
    endpoint: str
    upload_key: str
    ca_certificate: Path
    github_repository: str


class TransientNetworkError(RuntimeError):
    """A temporary internet/AWS failure that should be retried without blaming the token."""


class ServerRejectedError(RuntimeError):
    def __init__(self, status_code: int, detail: dict[str, Any]) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Server rejected the request: {detail}")


_ROUTE_LOCK = threading.Lock()
_PROXY_ROUTE_CACHE: dict[str, bool] = {}


_TRANSIENT_MARKERS = (
    "err_internet_disconnected",
    "err_name_not_resolved",
    "err_network_changed",
    "err_connection_timed_out",
    "err_connection_reset",
    "err_connection_closed",
    "timed out",
    "timeout",
    "temporary failure",
    "network is unreachable",
    "internet connection is not ready",
    "microsoft page did not finish loading",
    "winerror 10060",
    "getaddrinfo failed",
    "remote name could not be resolved",
    "failed to respond",
    "unable to connect",
    "cannot reach the token server",
    "token service is temporarily unavailable",
)


def is_transient_network_error(error: BaseException) -> bool:
    if isinstance(error, TransientNetworkError):
        return True
    message = str(error).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def load_config() -> ClientConfig:
    explicit = os.getenv("TOKEN_POOL_CLIENT_CONFIG", "").strip()
    candidates = [
        Path(explicit) if explicit else None,
        executable_dir() / "client-config.json",
        application_dir() / "client-config.json",
    ]
    path = next((candidate for candidate in candidates if candidate and candidate.exists()), None)
    if path is None:
        raise RuntimeError("client-config.json is missing; reinstall the Token Pool Client")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    certificate = Path(value["ca_certificate"])
    if not certificate.is_absolute():
        certificate = path.parent / certificate
    if not certificate.exists():
        raise RuntimeError(f"Pinned server certificate is missing: {certificate}")
    return ClientConfig(
        endpoint=str(value["endpoint"]).rstrip("/"),
        upload_key=str(value["upload_key"]),
        ca_certificate=certificate.resolve(),
        github_repository=str(value.get("github_repository") or "cheetos06/Automation_Project_FMBSM"),
    )


def upload_bundle(config: ClientConfig, bundle: bytes) -> dict[str, Any]:
    payload = encrypt_bundle(bundle, config.ca_certificate)
    return _request_with_retry(
        lambda: _sign(
            urllib.request.Request(
                config.endpoint + "/v1/accounts/session",
                data=payload,
                headers={
                    "Content-Type": ENVELOPE_CONTENT_TYPE,
                    "User-Agent": "FMBSM-Token-Pool-Client/1",
                },
                method="POST",
            ),
            config.upload_key,
            payload,
        ),
        config,
        timeout_seconds=10,
        attempts=2,
    )


def server_status(
    config: ClientConfig,
    *,
    account_ids: list[str] | None = None,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if account_ids is not None:
        unique_ids = list(dict.fromkeys(account_ids))
        body = json.dumps(
            {
                "client_id": _client_installation_id(),
                "observed_at": time.time(),
                "app_version": __version__,
                "account_ids": unique_ids,
                "status": status or {},
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            return _request_with_retry(
                lambda: _sign(
                    urllib.request.Request(
                        config.endpoint + "/v1/client/status",
                        data=body,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "FMBSM-Token-Pool-Client/1",
                        },
                        method="POST",
                    ),
                    config.upload_key,
                    body,
                ),
                config,
                timeout_seconds=4,
                attempts=2,
            )
        except ServerRejectedError as exc:
            if exc.status_code != 404 or exc.detail.get("error") != "not_found":
                raise
            legacy = server_status(config)
            legacy.update(
                {
                    "connection": "online",
                    "summary": {
                        "configured_account_count": len(unique_ids),
                        "uploaded_account_count": 0,
                        "ready_account_count": 0,
                        "renewal_required_count": 0,
                        "missing_account_count": len(unique_ids),
                    },
                    "accounts": [],
                    "scoped_status_supported": False,
                }
            )
            return legacy
    return _request_with_retry(
        lambda: _sign(
            urllib.request.Request(
                config.endpoint + "/v1/status",
                headers={"User-Agent": "FMBSM-Token-Pool-Client/1"},
            ),
            config.upload_key,
            b"",
        ),
        config,
        timeout_seconds=4,
        attempts=2,
    )


def client_preflight(
    config: ClientConfig,
    *,
    event: str,
    account_ids: list[str],
    scheduled_slot: str | None = None,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Report one signed presence event and obtain pool status in the same request.

    Older servers do not have the diagnostic endpoint. Falling back to the
    existing status request keeps deployment order safe and never blocks a
    renewal merely because server telemetry has not been deployed yet.
    """

    body = json.dumps(
        {
            "client_id": _client_installation_id(),
            "observed_at": time.time(),
            "event": event,
            "scheduled_slot": scheduled_slot,
            "app_version": __version__,
            "account_ids": list(dict.fromkeys(account_ids)),
            "status": status or {},
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    try:
        return _request_with_retry(
            lambda: _sign(
                urllib.request.Request(
                    config.endpoint + "/v1/client-events",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "FMBSM-Token-Pool-Client/1",
                    },
                    method="POST",
                ),
                config.upload_key,
                body,
            ),
            config,
            timeout_seconds=4,
            attempts=2,
        )
    except ServerRejectedError as exc:
        if exc.status_code != 404 or exc.detail.get("error") != "not_found":
            raise
        return server_status(config)


def _client_installation_id() -> str:
    path = application_dir() / "client-id.txt"
    try:
        value = uuid.UUID(path.read_text(encoding="ascii").strip()).hex
        return value
    except (OSError, ValueError):
        pass
    value = uuid.uuid4().hex
    temporary = path.with_suffix(".tmp")
    temporary.write_text(value + "\n", encoding="ascii")
    temporary.replace(path)
    return value


def _sign(request: urllib.request.Request, key: str, body: bytes) -> urllib.request.Request:
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    path = urllib.parse.urlsplit(request.full_url).path
    canonical = "\n".join(
        (timestamp, nonce, request.get_method().upper(), path, hashlib.sha256(body).hexdigest())
    ).encode("utf-8")
    signature = hmac.new(key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    request.add_header("X-FMBSM-Timestamp", timestamp)
    request.add_header("X-FMBSM-Nonce", nonce)
    request.add_header("X-FMBSM-Signature", signature)
    return request


def _request_with_retry(
    request_factory,
    config: ClientConfig,
    *,
    timeout_seconds: float,
    attempts: int = 3,
) -> dict[str, Any]:
    last_error: TransientNetworkError | None = None
    for attempt in range(1, attempts + 1):
        request = request_factory()
        try:
            return _open_json(request, config, timeout_seconds=timeout_seconds)
        except TransientNetworkError as exc:
            last_error = exc
            alternate = _alternate_transport_request(request, config)
            if alternate is not None:
                try:
                    return _open_json(alternate, config, timeout_seconds=timeout_seconds)
                except TransientNetworkError as alternate_exc:
                    last_error = alternate_exc
            if attempt == attempts:
                break
            time.sleep(attempt)
    raise last_error or TransientNetworkError(
        "The internet or AWS token service is temporarily unavailable. The app will retry automatically."
    )


def _open_json(
    request: urllib.request.Request,
    config: ClientConfig,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    original_url = request.full_url
    original_headers = dict(request.header_items())
    original_body = request.data
    original_method = request.get_method()
    route_key = _route_key(original_url)
    last_error: BaseException | None = None
    for use_proxy in _proxy_route_order(route_key):
        # ProxyHandler mutates a Request when routing it through an HTTP proxy.
        # Rebuild it for every route so a failed proxy attempt cannot leak its
        # proxy host into the subsequent direct attempt.
        attempt_request = urllib.request.Request(
            original_url,
            data=original_body,
            headers=original_headers,
            method=original_method,
        )
        opener = _opener(config, original_url, use_proxy=use_proxy)
        try:
            with opener.open(attempt_request, timeout=timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
            _remember_proxy_route(route_key, use_proxy)
            return value
        except urllib.error.HTTPError as exc:
            try:
                raw_detail = exc.read()
                detail = json.loads(raw_detail.decode("utf-8"))
            except Exception:
                detail = None
            if use_proxy and not isinstance(detail, dict):
                # Corporate proxies commonly return an HTML 407/502 page when
                # disabled or disconnected. It is not a token-server response.
                last_error = exc
                continue
            if exc.code >= 500:
                raise TransientNetworkError(
                    "The AWS token service is temporarily unavailable. The app will retry automatically."
                ) from exc
            if not isinstance(detail, dict):
                detail = {"error": str(exc)}
            raise ServerRejectedError(exc.code, detail) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            continue
    raise TransientNetworkError(
        "The internet or AWS token service is temporarily unavailable. The app will retry automatically."
    ) from last_error


def _route_key(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _proxy_route_order(route_key: str) -> tuple[bool, bool]:
    with _ROUTE_LOCK:
        preferred = _PROXY_ROUTE_CACHE.get(route_key, True)
    return preferred, not preferred


def _remember_proxy_route(route_key: str, use_proxy: bool) -> None:
    with _ROUTE_LOCK:
        _PROXY_ROUTE_CACHE[route_key] = use_proxy


def _opener(
    config: ClientConfig,
    url: str,
    *,
    use_proxy: bool,
) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if not use_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    if urllib.parse.urlsplit(url).scheme.lower() == "https":
        context = ssl.create_default_context(cafile=str(config.ca_certificate))
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def _alternate_transport_request(
    request: urllib.request.Request,
    config: ClientConfig,
) -> urllib.request.Request | None:
    """Retry the same signed operation through the server's other listener.

    Some company networks intermittently block the HTTP listener while others
    block HTTPS CONNECT tunnelling. The AWS service intentionally exposes both.
    Every alternate attempt receives a new nonce and signature, and HTTPS still
    uses the pinned certificate.
    """

    parsed = urllib.parse.urlsplit(request.full_url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    alternate_scheme = "https" if parsed.scheme.lower() == "http" else "http"
    alternate_url = urllib.parse.urlunsplit(
        (alternate_scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )
    headers = {
        key: value
        for key, value in request.header_items()
        if not key.lower().startswith("x-fmbsm-")
    }
    body = request.data or b""
    alternate = urllib.request.Request(
        alternate_url,
        data=request.data,
        headers=headers,
        method=request.get_method(),
    )
    return _sign(alternate, config.upload_key, body)
