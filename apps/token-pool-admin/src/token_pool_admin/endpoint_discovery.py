from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_REPOSITORY = "cheetos06/Automation_Project_FMBSM"
DEFAULT_REF = "main"
MANIFEST_PATH = "deployment/token-pool-endpoint.json"
DEPLOYMENT_NAME = "fmbsm-production"
CACHE_TTL_SECONDS = 300


def resolve_endpoint(
    configured_endpoint: str,
    *,
    repository: str = DEFAULT_REPOSITORY,
    cache_dir: Path,
    timeout_seconds: float = 5,
    now: float | None = None,
) -> tuple[str, str]:
    """Resolve the API endpoint from GitHub, with a local fallback and cache."""

    fallback = _valid_endpoint(configured_endpoint)
    current_time = float(now if now is not None else time.time())
    cache_path = cache_dir / "endpoint-manifest-cache.json"
    cached = _read_manifest(cache_path)
    if cached is not None and current_time - float(cached.get("fetched_at") or 0) < CACHE_TTL_SECONDS:
        return str(cached["endpoint"]), "github_cache"

    if os.getenv("TOKEN_POOL_DISABLE_ENDPOINT_DISCOVERY", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return fallback, "configured"

    url = _manifest_url(repository, current_time)
    try:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "FMBSM-Token-Pool-Admin/endpoint-discovery",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        manifest = _validated_manifest(payload)
        manifest["fetched_at"] = current_time
        _write_manifest(cache_path, manifest)
        return str(manifest["endpoint"]), "github"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        if cached is not None:
            return str(cached["endpoint"]), "github_cache_stale"
        return fallback, "configured"


def _manifest_url(repository: str, now: float) -> str:
    normalized = repository.strip().strip("/")
    parts = normalized.split("/")
    if len(parts) != 2 or any(not part or not all(char.isalnum() or char in "-_." for char in part) for part in parts):
        normalized = DEFAULT_REPOSITORY
    cache_buster = int(now // CACHE_TTL_SECONDS)
    return (
        f"https://raw.githubusercontent.com/{normalized}/{DEFAULT_REF}/"
        f"{MANIFEST_PATH}?v={cache_buster}"
    )


def _valid_endpoint(value: object) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Token-pool endpoint must be an HTTP(S) origin without credentials or a path")
    return endpoint


def _validated_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Endpoint manifest must be a JSON object")
    if int(value.get("schema_version") or 0) != 1:
        raise ValueError("Unsupported endpoint manifest schema")
    if str(value.get("deployment") or "") != DEPLOYMENT_NAME:
        raise ValueError("Endpoint manifest names the wrong deployment")
    return {
        "schema_version": 1,
        "deployment": DEPLOYMENT_NAME,
        "endpoint": _valid_endpoint(value.get("endpoint")),
        "updated_at": str(value.get("updated_at") or ""),
    }


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        manifest = _validated_manifest(value)
        manifest["fetched_at"] = float(value.get("fetched_at") or 0)
        return manifest
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_manifest(path: Path, value: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        return
