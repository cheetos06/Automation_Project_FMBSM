from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _load_resolver(relative_path: str, name: str) -> ModuleType:
    path = REPOSITORY_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["client", "admin"])
def resolver(request: pytest.FixtureRequest) -> ModuleType:
    application = str(request.param)
    return _load_resolver(
        f"apps/token-pool-{application}/src/token_pool_{application}/endpoint_discovery.py",
        f"endpoint_discovery_{application}",
    )


@pytest.fixture(autouse=True)
def enable_endpoint_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKEN_POOL_DISABLE_ENDPOINT_DISCOVERY", raising=False)


class _Response:
    def __init__(self, value: object) -> None:
        self.payload = json.dumps(value).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def _manifest(endpoint: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "deployment": "fmbsm-production",
        "endpoint": endpoint,
        "updated_at": "2026-07-22T00:00:00Z",
    }


def test_github_manifest_overrides_stale_config_and_is_cached(
    resolver: ModuleType,
    tmp_path: Path,
) -> None:
    response = _Response(_manifest("https://203.0.113.7"))
    with patch.object(resolver.urllib.request, "urlopen", return_value=response) as fetch:
        endpoint, source = resolver.resolve_endpoint(
            "http://198.51.100.2",
            cache_dir=tmp_path,
            now=1_000,
        )

    assert endpoint == "https://203.0.113.7"
    assert source == "github"
    assert fetch.call_count == 1

    cached = json.loads((tmp_path / "endpoint-manifest-cache.json").read_text())
    assert cached["endpoint"] == "https://203.0.113.7"
    assert cached["fetched_at"] == 1_000


def test_stale_last_known_good_endpoint_survives_github_outage(
    resolver: ModuleType,
    tmp_path: Path,
) -> None:
    cache = _manifest("http://203.0.113.8") | {"fetched_at": 100}
    (tmp_path / "endpoint-manifest-cache.json").write_text(json.dumps(cache))

    with patch.object(resolver.urllib.request, "urlopen", side_effect=OSError("offline")):
        endpoint, source = resolver.resolve_endpoint(
            "http://198.51.100.2",
            cache_dir=tmp_path,
            now=1_000,
        )

    assert endpoint == "http://203.0.113.8"
    assert source == "github_cache_stale"


def test_invalid_remote_manifest_cannot_override_local_endpoint(
    resolver: ModuleType,
    tmp_path: Path,
) -> None:
    response = _Response(_manifest("https://user:password@example.test/private"))
    with patch.object(resolver.urllib.request, "urlopen", return_value=response):
        endpoint, source = resolver.resolve_endpoint(
            "http://198.51.100.2",
            cache_dir=tmp_path,
            now=1_000,
        )

    assert endpoint == "http://198.51.100.2"
    assert source == "configured"
    assert not (tmp_path / "endpoint-manifest-cache.json").exists()


def test_fresh_cache_avoids_network_dependency(
    resolver: ModuleType,
    tmp_path: Path,
) -> None:
    cache = _manifest("http://203.0.113.9") | {"fetched_at": 950}
    (tmp_path / "endpoint-manifest-cache.json").write_text(json.dumps(cache))

    with patch.object(resolver.urllib.request, "urlopen") as fetch:
        endpoint, source = resolver.resolve_endpoint(
            "http://198.51.100.2",
            cache_dir=tmp_path,
            now=1_000,
        )

    assert endpoint == "http://203.0.113.9"
    assert source == "github_cache"
    fetch.assert_not_called()


def test_invalid_configured_endpoint_is_rejected_even_when_remote_exists(
    resolver: ModuleType,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="HTTP\\(S\\) origin"):
        resolver.resolve_endpoint(
            "file:///tmp/credentials",
            cache_dir=tmp_path,
            now=1_000,
        )
