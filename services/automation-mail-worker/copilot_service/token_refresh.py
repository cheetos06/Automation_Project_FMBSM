from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .microsoft_oauth import exchange_refresh_token
from .registry import AccountRecord, CopilotRegistry
from .session_bundle import BundleValidationError, account_id_from_claims, decode_jwt_claims


class TokenRefreshError(RuntimeError):
    pass


def ensure_account_fresh(
    registry: CopilotRegistry,
    account: AccountRecord,
    *,
    minimum_remaining_seconds: float = 600,
    timeout_seconds: float = 45,
) -> AccountRecord:
    if account.access_expires_at > time.time() + minimum_remaining_seconds:
        return account
    lock_path = registry.accounts_dir / account.account_id / ".refresh.lock"
    with _exclusive_lock(lock_path, timeout_seconds=60):
        current = registry.get_account(account.account_id)
        if current is None:
            raise TokenRefreshError(f"Account disappeared: {account.account_id}")
        if current.access_expires_at > time.time() + minimum_remaining_seconds:
            return current
        try:
            refreshed = _refresh_session_version(current, timeout_seconds=timeout_seconds)
            registry.update_token_state(
                current.account_id,
                session_path=refreshed["session_path"],
                access_expires_at=refreshed["expires_at"],
                refresh_expires_at=refreshed.get("refresh_expires_at"),
            )
            _prune_session_versions(
                refreshed["session_path"].parent,
                keep_path=refreshed["session_path"],
            )
        except Exception as exc:
            registry.mark_error(current.account_id, f"token_refresh_failed: {exc}")
            raise
        updated = registry.get_account(current.account_id)
        if updated is None:
            raise TokenRefreshError(f"Account disappeared after refresh: {account.account_id}")
        return updated


def fresh_runtime_accounts(
    registry: CopilotRegistry,
    *,
    minimum_remaining_seconds: float = 600,
) -> list[AccountRecord]:
    usable: list[AccountRecord] = []
    for account in registry.list_accounts():
        if not account.session_path.exists():
            registry.mark_error(account.account_id, "session_directory_missing")
            continue
        try:
            usable.append(
                ensure_account_fresh(
                    registry,
                    account,
                    minimum_remaining_seconds=minimum_remaining_seconds,
                )
            )
        except Exception:
            continue
    return usable


def _refresh_session_version(account: AccountRecord, *, timeout_seconds: float) -> dict[str, Any]:
    token_file = _latest(account.session_path, "private_edge_msal_refresh_token_", ".json")
    if token_file is None:
        raise TokenRefreshError("No refresh-token payload is available; run the desktop token app again")
    try:
        previous = json.loads(token_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TokenRefreshError(f"Cannot read {token_file.name}: {exc}") from exc
    refresh_token = str(previous.get("refresh_token") or "")
    if not refresh_token:
        raise TokenRefreshError("The saved OAuth payload has no refresh_token")

    payload = exchange_refresh_token(account.tenant_id, refresh_token, timeout_seconds)
    access_token = str(payload["access_token"])
    claims = decode_jwt_claims(access_token)
    try:
        refreshed_account_id = account_id_from_claims(claims)
    except BundleValidationError as exc:
        raise TokenRefreshError(str(exc)) from exc
    if refreshed_account_id != account.account_id:
        raise TokenRefreshError("OAuth refresh returned a different account identity")
    expires_at = float(claims.get("exp") or 0)
    if expires_at <= time.time() + 60:
        raise TokenRefreshError("OAuth returned an already-expired access token")

    combined = dict(previous)
    combined.update(payload)
    if not payload.get("refresh_token"):
        combined["refresh_token"] = refresh_token
    combined["refreshed_at"] = time.time()

    versions_dir = account.session_path.parent
    version_name = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-server-refresh-{os.getpid()}"
    destination = versions_dir / version_name
    staging = versions_dir / f".{version_name}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(account.session_path, staging)
    try:
        _patch_session_token(staging, access_token)
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        new_token_file = staging / f"private_edge_msal_refresh_token_{stamp}.json"
        new_token_file.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            new_token_file.chmod(0o600)
        except OSError:
            pass
        staging.replace(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    refresh_expires_at = None
    raw_refresh_expiry = combined.get("refresh_token_expires_in") or combined.get("refresh_expires_in")
    if raw_refresh_expiry:
        try:
            refresh_expires_at = time.time() + float(raw_refresh_expiry)
        except (TypeError, ValueError):
            pass
    return {
        "session_path": destination,
        "expires_at": expires_at,
        "refresh_expires_at": refresh_expires_at,
        "claims": claims,
    }


def _patch_session_token(session_dir: Path, access_token: str) -> None:
    frames_path = _latest(session_dir, "private_websocket_raw_frames_", ".json")
    templates_path = _latest(session_dir, "private_replay_templates_", ".json")
    if frames_path is None or templates_path is None:
        raise TokenRefreshError("The session is missing websocket frames or replay templates")
    frames = json.loads(frames_path.read_text(encoding="utf-8-sig"))
    templates = json.loads(templates_path.read_text(encoding="utf-8-sig"))
    frame_updates = 0
    template_updates = 0
    for frame in frames if isinstance(frames, list) else []:
        if not isinstance(frame, dict):
            continue
        url = str(frame.get("url") or "")
        if "substrate.office.com/m365copilot/chathub" not in url.lower():
            continue
        split = urlsplit(url)
        query = dict(parse_qsl(split.query, keep_blank_values=True))
        query["access_token"] = access_token
        frame["url"] = urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))
        frame_updates += 1
    for template in templates if isinstance(templates, list) else []:
        if not isinstance(template, dict) or not isinstance(template.get("headers"), dict):
            continue
        for key in list(template["headers"]):
            if key.lower() == "authorization":
                template["headers"][key] = f"Bearer {access_token}"
                template_updates += 1
    if not frame_updates:
        raise TokenRefreshError("No Chathub URL was patched in the captured frames")
    frames_path.write_text(json.dumps(frames, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    templates_path.write_text(json.dumps(templates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _latest(directory: Path, prefix: str, suffix: str) -> Path | None:
    candidates = [
        path for path in directory.iterdir()
        if path.is_file() and path.name.startswith(prefix) and path.name.endswith(suffix)
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _prune_session_versions(versions_dir: Path, *, keep_path: Path, retain: int = 10) -> None:
    """Keep recent immutable versions while never deleting the active session."""
    candidates = sorted(
        (
            path for path in versions_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    keep = {keep_path.resolve()}
    keep.update(path.resolve() for path in candidates[: max(1, retain)])
    for path in candidates:
        if path.resolve() not in keep:
            shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _exclusive_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, f"{os.getpid()} {time.time()}\n".encode("ascii"))
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
                if age > max(120.0, timeout_seconds * 2):
                    path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TokenRefreshError("Timed out waiting for another token refresh")
            time.sleep(0.25)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            path.unlink()
        except OSError:
            pass
