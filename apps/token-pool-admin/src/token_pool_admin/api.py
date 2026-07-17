from __future__ import annotations

import hashlib
import hmac
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .storage import AdminConfig


class AdminApiError(RuntimeError):
    pass


ADMIN_ENVELOPE_CONTENT_TYPE = "application/vnd.fmbsm.admin+aesgcm"
_ROUTE_LOCK = threading.Lock()
_PROXY_ROUTE_CACHE: dict[str, bool] = {}


def snapshot(config: AdminConfig) -> dict[str, Any]:
    return _post(config, "/v1/admin/snapshot", {})


def create_commands(
    config: AdminConfig,
    *,
    client_ids: list[str],
    command: str,
    payload: dict[str, Any],
    expires_in_seconds: int = 15 * 60,
) -> dict[str, Any]:
    return _post(
        config,
        "/v1/admin/commands",
        {
            "client_ids": client_ids,
            "command": command,
            "payload": payload,
            "expires_in_seconds": expires_in_seconds,
        },
    )


def cancel_command(config: AdminConfig, command_id: str) -> dict[str, Any]:
    return _post(config, "/v1/admin/commands/cancel", {"command_id": command_id})


def forget_clients(config: AdminConfig, client_ids: list[str]) -> dict[str, Any]:
    return _post(config, "/v1/admin/clients/forget", {"client_ids": client_ids})


def start_copilot_test(config: AdminConfig, account_ids: list[str]) -> dict[str, Any]:
    return _post(config, "/v1/admin/copilot-tests", {"account_ids": account_ids}, timeout=20)


def _post(
    config: AdminConfig,
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float = 12,
) -> dict[str, Any]:
    plaintext = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    envelope_nonce = os.urandom(12)
    body = envelope_nonce + AESGCM(_encryption_key(config.admin_key)).encrypt(
        envelope_nonce,
        plaintext,
        f"request\nPOST\n{path}".encode("utf-8"),
    )
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    canonical = "\n".join(
        (timestamp, nonce, "POST", path, hashlib.sha256(body).hexdigest())
    ).encode("utf-8")
    signature = hmac.new(
        config.admin_key.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Content-Type": ADMIN_ENVELOPE_CONTENT_TYPE,
        "User-Agent": "FMBSM-Token-Pool-Admin/1",
        "X-FMBSM-Admin-Timestamp": timestamp,
        "X-FMBSM-Admin-Nonce": nonce,
        "X-FMBSM-Admin-Signature": signature,
    }
    value: dict[str, Any] | None = None
    last_network_error: BaseException | None = None
    for use_proxy in _proxy_route_order(config.endpoint):
        request = urllib.request.Request(
            config.endpoint + path,
            data=body,
            headers=headers,
            method="POST",
        )
        opener = _opener(config, use_proxy=use_proxy)
        try:
            with opener.open(request, timeout=timeout) as response:
                value = _decode_response(
                    response.read(),
                    status=response.status,
                    content_type=response.headers.get("Content-Type", ""),
                    path=path,
                    request_nonce=nonce,
                    admin_key=config.admin_key,
                )
            _remember_proxy_route(config.endpoint, use_proxy)
            break
        except urllib.error.HTTPError as exc:
            try:
                detail = _decode_response(
                    exc.read(),
                    status=exc.code,
                    content_type=exc.headers.get("Content-Type", ""),
                    path=path,
                    request_nonce=nonce,
                    admin_key=config.admin_key,
                )
            except Exception:
                # A configured proxy can answer with its own HTML 407/502/503
                # page. That is not an authenticated FMBSM server rejection;
                # treat it like a failed proxy route and try direct transport.
                if use_proxy:
                    last_network_error = exc
                    continue
                detail = {"error": str(exc)}
            raise AdminApiError(f"Server rejected administrator request: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_network_error = exc
            continue
    if value is None:
        raise AdminApiError(f"Cannot reach the FMBSM server: {last_network_error}") from last_network_error
    if not isinstance(value, dict):
        raise AdminApiError("Server returned an invalid administrator response")
    return value


def _proxy_route_order(endpoint: str) -> tuple[bool, bool]:
    """Try the last working route first, then the other route.

    Python honours HTTP_PROXY/HTTPS_PROXY environment variables independently
    from the Windows proxy toggle.  A colleague can therefore disable a proxy
    in Windows while a packaged process still inherits a now-dead proxy address.
    The admin envelope is encrypted and authenticated at the application layer,
    so retrying the same signed request directly does not weaken transport safety.
    """

    with _ROUTE_LOCK:
        preferred = _PROXY_ROUTE_CACHE.get(endpoint, True)
    return preferred, not preferred


def _remember_proxy_route(endpoint: str, use_proxy: bool) -> None:
    with _ROUTE_LOCK:
        _PROXY_ROUTE_CACHE[endpoint] = use_proxy


def _opener(config: AdminConfig, *, use_proxy: bool) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if not use_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    if urllib.parse.urlsplit(config.endpoint).scheme.lower() == "https":
        context = ssl.create_default_context(cafile=str(config.ca_certificate))
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def _decode_response(
    body: bytes,
    *,
    status: int,
    content_type: str,
    path: str,
    request_nonce: str,
    admin_key: str,
) -> dict[str, Any]:
    if content_type.split(";", 1)[0].strip().lower() != ADMIN_ENVELOPE_CONTENT_TYPE:
        raise AdminApiError("Server returned an unencrypted administrator response")
    if len(body) < 12 + 16:
        raise AdminApiError("Server returned a truncated administrator response")
    nonce, ciphertext = body[:12], body[12:]
    try:
        plaintext = AESGCM(_encryption_key(admin_key)).decrypt(
            nonce,
            ciphertext,
            f"response\n{int(status)}\n{path}\n{request_nonce}".encode("utf-8"),
        )
        value = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        raise AdminApiError("Administrator response authentication failed") from exc
    if not isinstance(value, dict):
        raise AdminApiError("Server returned an invalid administrator response")
    return value


def _encryption_key(admin_key: str) -> bytes:
    return hashlib.sha256(
        b"fmbsm-admin-envelope-v1\0" + admin_key.encode("utf-8")
    ).digest()
