from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import stat
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .registry import AccountRecord, CopilotRegistry


MAX_BUNDLE_BYTES = 25 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_FILES = 40
_ALLOWED_FILE = re.compile(
    r"^(?:"
    r"manifest\.json|"
    r"private_websocket_raw_frames_[A-Za-z0-9_.-]+\.json|"
    r"private_replay_templates_[A-Za-z0-9_.-]+\.json|"
    r"private_playwright_cookies_[A-Za-z0-9_.-]+\.json|"
    r"private_msal_local_storage_[A-Za-z0-9_.-]+\.json|"
    r"private_edge_msal_refresh_token_[A-Za-z0-9_.-]+\.json|"
    r"private_msal_cache_encryption\.txt|"
    r"[A-Za-z0-9_.-]+_summary\.json"
    r")$"
)


class BundleValidationError(ValueError):
    pass


@dataclass(frozen=True)
class InstalledBundle:
    account: AccountRecord
    claims: dict[str, Any]
    bundle_sha256: str
    file_count: int


def decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise BundleValidationError("The captured access token is not a JWT")
    payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception as exc:
        raise BundleValidationError("The access-token claims could not be decoded") from exc
    if not isinstance(value, dict):
        raise BundleValidationError("The access-token claims are malformed")
    return value


def find_access_token(frames: Any) -> str:
    if not isinstance(frames, list):
        raise BundleValidationError("The websocket frame file must contain a JSON list")
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        url = str(frame.get("url") or "")
        if "substrate.office.com/m365copilot/chathub" not in url.lower():
            continue
        token = parse_qs(urlsplit(url).query).get("access_token", [""])[0]
        if token:
            return token
    raise BundleValidationError("No Chathub access token was found in the websocket capture")


def account_id_from_claims(claims: dict[str, Any]) -> str:
    tenant = str(claims.get("tid") or "").strip().lower()
    object_id = str(claims.get("oid") or "").strip().lower()
    username = _username(claims).lower()
    if not tenant or not (object_id or username):
        raise BundleValidationError("The access token is missing tenant/account identity claims")
    digest = hashlib.sha256(f"{tenant}|{object_id or username}".encode("utf-8")).hexdigest()
    return f"account-{digest[:20]}"


def install_bundle(
    payload: bytes,
    registry: CopilotRegistry,
    *,
    remote_address: str | None = None,
    session_validator: Callable[[dict[str, bytes], str], dict[str, Any]] | None = None,
    maximum_accounts: int | None = None,
) -> InstalledBundle:
    if not payload:
        raise BundleValidationError("The uploaded bundle is empty")
    if len(payload) > MAX_BUNDLE_BYTES:
        raise BundleValidationError(f"The bundle exceeds {MAX_BUNDLE_BYTES} bytes")

    digest = hashlib.sha256(payload).hexdigest()
    files = _read_bundle(payload)
    manifest = _json_file(files, "manifest.json", expected_type=dict)
    frames_name = _newest_name(files, "private_websocket_raw_frames_", ".json")
    templates_name = _newest_name(files, "private_replay_templates_", ".json")
    cookies_name = _newest_name(files, "private_playwright_cookies_", ".json")
    frames = _json_file(files, frames_name, expected_type=list)
    _json_file(files, templates_name, expected_type=list)
    cookies = _json_file(files, cookies_name, expected_type=list)
    if not cookies:
        raise BundleValidationError("The upload-cookie snapshot is empty")

    token = find_access_token(frames)
    if session_validator is None:
        claims = decode_jwt_claims(token)
    else:
        claims = session_validator(files, token)
    tenant_id = str(claims.get("tid") or "").strip().lower()
    object_id = str(claims.get("oid") or "").strip().lower()
    username = _username(claims)
    expires_at = float(claims.get("exp") or 0)
    now = time.time()
    if not tenant_id or not username or expires_at <= now + 60:
        raise BundleValidationError("The uploaded access token is expired or missing required identity claims")

    account_id = account_id_from_claims(claims)
    if (
        maximum_accounts is not None
        and maximum_accounts > 0
        and registry.get_account(account_id) is None
        and registry.status()["account_count"] >= maximum_accounts
    ):
        raise BundleValidationError("The shared account pool has reached its configured account limit")
    version_name = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}-{digest[:12]}"
    versions_dir = registry.accounts_dir / account_id / "versions"
    session_path = versions_dir / version_name
    if session_path.exists():
        record = registry.get_account(account_id)
        if record is None:
            raise RuntimeError("Bundle directory exists without a registry record")
        return InstalledBundle(record, claims, digest, len(files))

    versions_dir.mkdir(parents=True, exist_ok=True)
    staging = versions_dir / f".{version_name}.{os.getpid()}.tmp"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        for name, data in files.items():
            target = staging / name
            target.write_bytes(data)
            try:
                target.chmod(0o600)
            except OSError:
                pass
        staging.replace(session_path)
    except Exception:
        for child in staging.glob("*") if staging.exists() else ():
            try:
                child.unlink()
            except OSError:
                pass
        try:
            staging.rmdir()
        except OSError:
            pass
        raise

    refresh_expires_at = _refresh_expiry(files, now) or _manifest_authorization_expiry(manifest, now)
    source_version = str(manifest.get("client_version") or manifest.get("version") or "unknown")[:100]
    record = registry.upsert_account(
        account_id=account_id,
        username=username,
        tenant_id=tenant_id,
        object_id=object_id,
        session_path=session_path,
        uploaded_at=now,
        access_expires_at=expires_at,
        refresh_expires_at=refresh_expires_at,
        source_version=source_version,
        bundle_sha256=digest,
        remote_address=remote_address,
    )
    _prune_versions(versions_dir, keep_path=session_path, keep=3)
    return InstalledBundle(record, claims, digest, len(files))


def _read_bundle(payload: bytes) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise BundleValidationError("The uploaded payload is not a valid ZIP bundle") from exc
    infos = archive.infolist()
    if len(infos) > MAX_FILES:
        raise BundleValidationError(f"The bundle contains more than {MAX_FILES} files")
    total_size = 0
    result: dict[str, bytes] = {}
    for info in infos:
        name = info.filename.replace("\\", "/")
        if info.is_dir():
            continue
        if "/" in name or name in {"", ".", ".."} or not _ALLOWED_FILE.fullmatch(name):
            raise BundleValidationError(f"Bundle contains a disallowed path: {info.filename!r}")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise BundleValidationError(f"Bundle contains a symbolic link: {name!r}")
        total_size += int(info.file_size)
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise BundleValidationError("The uncompressed bundle is too large")
        if name in result:
            raise BundleValidationError(f"Bundle contains duplicate file: {name!r}")
        result[name] = archive.read(info)
    required = {"manifest.json"}
    missing = required - result.keys()
    if missing:
        raise BundleValidationError(f"Bundle is missing: {', '.join(sorted(missing))}")
    return result


def _json_file(files: dict[str, bytes], name: str, *, expected_type: type) -> Any:
    try:
        value = json.loads(files[name].decode("utf-8-sig"))
    except KeyError as exc:
        raise BundleValidationError(f"Bundle is missing {name}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleValidationError(f"Bundle file {name} is not valid JSON") from exc
    if not isinstance(value, expected_type):
        raise BundleValidationError(f"Bundle file {name} has the wrong JSON shape")
    return value


def _newest_name(files: dict[str, bytes], prefix: str, suffix: str) -> str:
    matches = sorted(name for name in files if name.startswith(prefix) and name.endswith(suffix))
    if not matches:
        raise BundleValidationError(f"Bundle is missing a {prefix}*{suffix} file")
    return matches[-1]


def _username(claims: dict[str, Any]) -> str:
    return str(
        claims.get("preferred_username")
        or claims.get("upn")
        or claims.get("email")
        or claims.get("name")
        or ""
    ).strip()


def _refresh_expiry(files: dict[str, bytes], now: float) -> float | None:
    names = sorted(name for name in files if name.startswith("private_edge_msal_refresh_token_") and name.endswith(".json"))
    if not names:
        return None
    try:
        payload = json.loads(files[names[-1]].decode("utf-8-sig"))
    except Exception:
        return None
    seconds = payload.get("refresh_token_expires_in") or payload.get("refresh_expires_in")
    try:
        return now + float(seconds) if seconds else None
    except (TypeError, ValueError):
        return None


def _manifest_authorization_expiry(manifest: dict[str, Any], now: float) -> float | None:
    try:
        expires_at = float(manifest.get("authorization_expires_at"))
    except (TypeError, ValueError):
        return None
    # This is scheduling metadata, not proof of identity. Microsoft session
    # validation above remains authoritative, and the client value is bounded.
    if now < expires_at <= now + 25 * 60 * 60:
        return expires_at
    return None


def _prune_versions(versions_dir: Path, *, keep_path: Path, keep: int) -> None:
    candidates = sorted(
        (path for path in versions_dir.iterdir() if path.is_dir() and not path.name.startswith(".")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    protected = {keep_path.resolve(), *(path.resolve() for path in candidates[:keep])}
    for directory in candidates:
        if directory.resolve() in protected:
            continue
        for child in directory.iterdir():
            if child.is_file():
                try:
                    child.unlink()
                except OSError:
                    pass
        try:
            directory.rmdir()
        except OSError:
            pass
