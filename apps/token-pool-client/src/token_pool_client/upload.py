from __future__ import annotations

import json
import hashlib
import hmac
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import application_dir, executable_dir
from .transport_crypto import ENVELOPE_CONTENT_TYPE, encrypt_bundle


@dataclass(frozen=True)
class ClientConfig:
    endpoint: str
    upload_key: str
    ca_certificate: Path
    github_repository: str


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
    request = urllib.request.Request(
        config.endpoint + "/v1/accounts/session",
        data=payload,
        headers={
            "Content-Type": ENVELOPE_CONTENT_TYPE,
            "User-Agent": "FMBSM-Token-Pool-Client/1",
        },
        method="POST",
    )
    return _open_json(_sign(request, config.upload_key, payload), config)


def server_status(config: ClientConfig) -> dict[str, Any]:
    request = urllib.request.Request(
        config.endpoint + "/v1/status",
        headers={
            "User-Agent": "FMBSM-Token-Pool-Client/1",
        },
    )
    return _open_json(_sign(request, config.upload_key, b""), config)


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


def _open_json(request: urllib.request.Request, config: ClientConfig) -> dict[str, Any]:
    handlers: list[urllib.request.BaseHandler] = []
    if urllib.parse.urlsplit(config.endpoint).scheme.lower() == "https":
        context = ssl.create_default_context(cafile=str(config.ca_certificate))
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"error": str(exc)}
        raise RuntimeError(f"Server rejected the request: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach the token server: {exc.reason}") from exc
