from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .storage import ClientAccount


CLIENT_ID = "4765445b-32c6-49b0-83e6-1d93765276ca"
SCOPE = "https://substrate.office.com/sydney/.default openid profile offline_access"
SPA_AUTHORIZATION_LIFETIME_SECONDS = 24 * 60 * 60


class RefreshError(RuntimeError):
    pass


def account_from_session(session_dir: Path, profile_dir: Path) -> ClientAccount:
    frames_path = _latest(session_dir, "private_websocket_raw_frames_", ".json")
    if frames_path is None:
        raise RefreshError("The session has no captured websocket frames")
    frames = json.loads(frames_path.read_text(encoding="utf-8-sig"))
    token = ""
    for frame in frames if isinstance(frames, list) else []:
        url = str(frame.get("url") or "") if isinstance(frame, dict) else ""
        if "substrate.office.com/m365copilot/chathub" in url.lower():
            token = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("access_token", [""])[0]
            if token:
                break
    if not token:
        raise RefreshError("The session has no Chathub access token")
    claims = decode_claims(token)
    return ClientAccount(
        account_id=account_id(claims),
        username=_username(claims),
        tenant_id=str(claims.get("tid") or ""),
        object_id=str(claims.get("oid") or ""),
        session_dir=str(session_dir.resolve()),
        profile_dir=str(profile_dir.resolve()),
        access_expires_at=float(claims.get("exp") or 0),
    )


def decode_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise RefreshError("Microsoft returned an invalid access token")
    return json.loads(_b64url(parts[1]).decode("utf-8"))


def account_id(claims: dict[str, Any]) -> str:
    tenant = str(claims.get("tid") or "").lower()
    identity = str(claims.get("oid") or _username(claims)).lower()
    if not tenant or not identity:
        raise RefreshError("The Microsoft token has no tenant/account identity")
    return "account-" + hashlib.sha256(f"{tenant}|{identity}".encode("utf-8")).hexdigest()[:20]


def refresh_existing(account: ClientAccount) -> tuple[ClientAccount, dict[str, Any]]:
    token_file = _latest(account.session_path, "private_edge_msal_refresh_token_", ".json")
    if token_file is None:
        return initialize_refresh(account.session_path, account.profile_path, expected_account=account)
    payload = json.loads(token_file.read_text(encoding="utf-8-sig"))
    refresh_token = str(payload.get("refresh_token") or "")
    if not refresh_token:
        return initialize_refresh(account.session_path, account.profile_path, expected_account=account)
    refreshed = oauth_refresh(refresh_token, account.tenant_id)
    _validate_expected_identity(refreshed, account)
    combined = dict(payload)
    combined.update(refreshed)
    if not refreshed.get("refresh_token"):
        combined["refresh_token"] = refresh_token
    updated = _save_and_patch(
        account.session_path,
        account.profile_path,
        combined,
        authorization_expires_at=account.authorization_expires_at,
    )
    updated.last_uploaded_at = account.last_uploaded_at
    return updated, combined


def initialize_refresh(
    session_dir: Path,
    profile_dir: Path,
    expected_account: ClientAccount | None = None,
) -> tuple[ClientAccount, dict[str, Any]]:
    records = decrypt_captured_msal(session_dir)
    accounts = _account_index(records)
    candidates = [
        record
        for record in records
        if "refreshtoken" in record["key"].lower()
        and record["value"].get("secret")
        and (not record["value"].get("clientId") or record["value"].get("clientId") == CLIENT_ID)
    ]
    candidates.sort(key=lambda item: float(item["value"].get("lastUpdatedAt") or item.get("lastUpdatedAt") or 0), reverse=True)
    if not candidates:
        raise RefreshError("No Microsoft refresh token was found after sign-in")
    selected = candidates[0]
    tenant = _record_tenant(selected, accounts) or "organizations"
    original_refresh_token = str(selected["value"]["secret"])
    refreshed = oauth_refresh(original_refresh_token, tenant)
    if not refreshed.get("refresh_token"):
        refreshed["refresh_token"] = original_refresh_token
    if expected_account is not None:
        _validate_expected_identity(refreshed, expected_account)
    authorization_at = _record_updated_at(selected) or time.time()
    account = _save_and_patch(
        session_dir,
        profile_dir,
        refreshed,
        authorization_expires_at=authorization_at + SPA_AUTHORIZATION_LIFETIME_SECONDS,
    )
    return account, refreshed


def requires_interactive_reauthentication(error: BaseException) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "aadsts700084",
            "aadsts700082",
            "interaction_required",
            "login_required",
            "invalid_grant",
            "refresh token was issued to a single page app",
            "refresh token has expired",
            "no microsoft refresh token was found",
            "captured msal state is incomplete",
        )
    )


def oauth_refresh(refresh_token: str, tenant: str) -> dict[str, Any]:
    body = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": SCOPE,
            "client_info": "1",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Origin": "https://m365.cloud.microsoft",
            "Referer": "https://m365.cloud.microsoft/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {}
        message = detail.get("error_description") or detail.get("error") or str(exc)
        raise RefreshError(f"Microsoft refresh failed: {str(message)[:500]}") from exc
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise RefreshError("Microsoft refresh returned no access token")
    return payload


def _validate_expected_identity(payload: dict[str, Any], expected: ClientAccount) -> None:
    claims = decode_claims(str(payload.get("access_token") or ""))
    actual_tenant = str(claims.get("tid") or "").lower()
    actual_object = str(claims.get("oid") or "").lower()
    if actual_tenant != expected.tenant_id.lower() or actual_object != expected.object_id.lower():
        raise RefreshError(
            "Microsoft signed in a different account. "
            f"Expected {expected.username}; close the browser and retry with that account."
        )


def decrypt_captured_msal(session_dir: Path) -> list[dict[str, Any]]:
    cookie_path = session_dir / "private_msal_cache_encryption.txt"
    storage_path = session_dir / "private_msal_local_storage_current.json"
    if not storage_path.exists():
        raise RefreshError("The captured MSAL state is incomplete; sign in again")
    storage = json.loads(storage_path.read_text(encoding="utf-8-sig"))
    plaintext_records: list[dict[str, Any]] = []
    for item in storage.get("records", []):
        key = str(item.get("key") or "")
        if "msal" not in key.lower():
            continue
        try:
            value = json.loads(item.get("value") or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        plaintext_records.append({"key": key, "value": value, "lastUpdatedAt": value.get("lastUpdatedAt")})
    if any(
        "refreshtoken" in record["key"].lower() and record["value"].get("secret")
        for record in plaintext_records
    ):
        return plaintext_records
    if not cookie_path.exists():
        raise RefreshError("The captured MSAL state is incomplete; sign in again")
    raw_cookie = cookie_path.read_text(encoding="utf-8-sig").strip()
    value = raw_cookie.split("=", 1)[1] if raw_cookie.startswith("msal.cache.encryption=") else raw_cookie
    for _ in range(2):
        decoded = urllib.parse.unquote(value)
        if decoded == value:
            break
        value = decoded
    cookie = json.loads(value)
    cookie_id = str(cookie.get("id") or "")
    base_key = _b64url(str(cookie.get("key") or ""))
    if not cookie_id or not base_key:
        raise RefreshError("The MSAL encryption cookie is malformed")
    records: list[dict[str, Any]] = []
    for item in storage.get("records", []):
        key = str(item.get("key") or "")
        if "msal" not in key.lower():
            continue
        try:
            wrapped = json.loads(item.get("value") or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(wrapped, dict):
            continue
        if wrapped.get("id") != cookie_id or not wrapped.get("nonce") or not wrapped.get("data"):
            continue
        info = CLIENT_ID.encode("utf-8") if CLIENT_ID in key else b""
        aes_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_b64url(str(wrapped["nonce"])),
            info=info,
        ).derive(base_key)
        try:
            plaintext = AESGCM(aes_key).decrypt(b"\0" * 12, _b64url(str(wrapped["data"])), None)
            decrypted = json.loads(plaintext.decode("utf-8"))
        except Exception:
            continue
        records.append({"key": key, "value": decrypted, "lastUpdatedAt": wrapped.get("lastUpdatedAt")})
    if not records:
        raise RefreshError("No decryptable Microsoft session records were captured")
    return records


def _save_and_patch(
    session_dir: Path,
    profile_dir: Path,
    payload: dict[str, Any],
    *,
    authorization_expires_at: float | None = None,
) -> ClientAccount:
    access_token = str(payload.get("access_token") or "")
    claims = decode_claims(access_token)
    expires_at = float(claims.get("exp") or 0)
    if expires_at <= time.time() + 60:
        raise RefreshError("Microsoft returned an expired access token")
    _patch_session(session_dir, access_token)
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    token_path = session_dir / f"private_edge_msal_refresh_token_{stamp}.json"
    payload = dict(payload)
    payload["refreshed_at"] = time.time()
    token_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ClientAccount(
        account_id=account_id(claims),
        username=_username(claims),
        tenant_id=str(claims.get("tid") or ""),
        object_id=str(claims.get("oid") or ""),
        session_dir=str(session_dir.resolve()),
        profile_dir=str(profile_dir.resolve()),
        access_expires_at=expires_at,
        authorization_expires_at=authorization_expires_at,
    )


def _record_updated_at(record: dict[str, Any]) -> float | None:
    raw = record.get("value", {}).get("lastUpdatedAt") or record.get("lastUpdatedAt")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 10_000_000_000:
        value /= 1000
    return value if value > 0 else None


def _patch_session(session_dir: Path, access_token: str) -> None:
    frames_path = _latest(session_dir, "private_websocket_raw_frames_", ".json")
    templates_path = _latest(session_dir, "private_replay_templates_", ".json")
    if frames_path is None or templates_path is None:
        raise RefreshError("Copilot bootstrap frames/templates are missing")
    frames = json.loads(frames_path.read_text(encoding="utf-8-sig"))
    templates = json.loads(templates_path.read_text(encoding="utf-8-sig"))
    patched = 0
    for frame in frames:
        url = str(frame.get("url") or "") if isinstance(frame, dict) else ""
        if "substrate.office.com/m365copilot/chathub" not in url.lower():
            continue
        split = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
        query["access_token"] = access_token
        frame["url"] = urllib.parse.urlunsplit(
            (split.scheme, split.netloc, split.path, urllib.parse.urlencode(query), split.fragment)
        )
        patched += 1
    for template in templates:
        headers = template.get("headers") if isinstance(template, dict) else None
        if not isinstance(headers, dict):
            continue
        for key in list(headers):
            if key.lower() == "authorization":
                headers[key] = f"Bearer {access_token}"
    if not patched:
        raise RefreshError("No Chathub URL was found in the captured bootstrap")
    frames_path.write_text(json.dumps(frames, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    templates_path.write_text(json.dumps(templates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _account_index(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        value = record["value"]
        credential = str(value.get("credentialType") or value.get("credential_type") or "").lower()
        home = str(value.get("homeAccountId") or value.get("home_account_id") or "").lower()
        if home and (credential == "account" or "account" in record["key"].lower()):
            result[home] = value
    return result


def _record_tenant(record: dict[str, Any], accounts: dict[str, dict[str, Any]]) -> str:
    value = record["value"]
    home = str(value.get("homeAccountId") or value.get("home_account_id") or "")
    linked = accounts.get(home.lower(), {})
    return str(
        value.get("realm")
        or value.get("tenantId")
        or value.get("tid")
        or linked.get("realm")
        or linked.get("tenantId")
        or (home.rsplit(".", 1)[-1] if "." in home else "")
    )


def _username(claims: dict[str, Any]) -> str:
    return str(
        claims.get("preferred_username")
        or claims.get("upn")
        or claims.get("email")
        or claims.get("name")
        or "unknown"
    )


def _latest(directory: Path, prefix: str, suffix: str) -> Path | None:
    candidates = [
        path for path in directory.glob(f"{prefix}*{suffix}") if path.is_file()
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _b64url(value: str) -> bytes:
    value += "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value.encode("ascii"))
