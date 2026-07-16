from __future__ import annotations

import hashlib
import hmac
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from .storage import AdminConfig


class AdminApiError(RuntimeError):
    pass


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


def start_copilot_test(config: AdminConfig, account_ids: list[str]) -> dict[str, Any]:
    return _post(config, "/v1/admin/copilot-tests", {"account_ids": account_ids}, timeout=20)


def _post(
    config: AdminConfig,
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float = 12,
) -> dict[str, Any]:
    body = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
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
    request = urllib.request.Request(
        config.endpoint + path,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "FMBSM-Token-Pool-Admin/1",
            "X-FMBSM-Admin-Timestamp": timestamp,
            "X-FMBSM-Admin-Nonce": nonce,
            "X-FMBSM-Admin-Signature": signature,
        },
        method="POST",
    )
    handlers: list[urllib.request.BaseHandler] = []
    if urllib.parse.urlsplit(config.endpoint).scheme.lower() == "https":
        context = ssl.create_default_context(cafile=str(config.ca_certificate))
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"error": str(exc)}
        raise AdminApiError(f"Server rejected administrator request: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdminApiError(f"Cannot reach the FMBSM server: {exc}") from exc
    if not isinstance(value, dict):
        raise AdminApiError("Server returned an invalid administrator response")
    return value
