from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .endpoint_discovery import DEFAULT_REPOSITORY, resolve_endpoint


def application_dir() -> Path:
    root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "FMBSM" / "TokenPoolAdmin"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AdminConfig:
    endpoint: str
    admin_key: str
    ca_certificate: Path
    endpoint_source: str = "configured"


def load_config() -> AdminConfig:
    explicit = os.getenv("TOKEN_POOL_ADMIN_CONFIG", "").strip()
    candidates = [
        Path(explicit) if explicit else None,
        executable_dir() / "admin-config.json",
        application_dir() / "admin-config.json",
    ]
    path = next((candidate for candidate in candidates if candidate and candidate.exists()), None)
    if path is None:
        raise RuntimeError("admin-config.json is missing; reinstall the private Token Pool Admin app")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    key = str(value.get("admin_key") or "")
    if len(key) < 32:
        raise RuntimeError("The private administrator credential is missing or invalid")
    certificate = Path(str(value.get("ca_certificate") or ""))
    if not certificate.is_absolute():
        certificate = path.parent / certificate
    if not certificate.exists():
        raise RuntimeError(f"Pinned server certificate is missing: {certificate}")
    endpoint, endpoint_source = resolve_endpoint(
        str(value["endpoint"]),
        repository=str(value.get("github_repository") or DEFAULT_REPOSITORY),
        cache_dir=application_dir(),
    )
    return AdminConfig(
        endpoint=endpoint,
        admin_key=key,
        ca_certificate=certificate.resolve(),
        endpoint_source=endpoint_source,
    )
