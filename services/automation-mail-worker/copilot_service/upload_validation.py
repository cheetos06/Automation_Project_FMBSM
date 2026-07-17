from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .microsoft_oauth import (
    CLIENT_ID,
    OAuthRefreshRejected,
    OAuthRefreshUnavailable,
    exchange_refresh_token,
)
from .session_bundle import (
    BundleValidationError,
    account_id_from_claims,
    decode_jwt_claims,
    find_access_token,
)


EXPECTED_AUDIENCE = "https://substrate.office.com/sydney"
_TENANT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_AADSTS_CODE = re.compile(r"\b(AADSTS\d+)\b", re.IGNORECASE)
_INTERACTIVE_MFA_CODES = frozenset({"AADSTS50072", "AADSTS50074", "AADSTS50076", "AADSTS50078", "AADSTS50079"})
LOGGER = logging.getLogger("copilot-session-validator")


class SessionProofUnavailable(RuntimeError):
    """The server could not ask Microsoft to prove the uploaded session."""


class MicrosoftSessionRejected(BundleValidationError):
    """Microsoft rejected a presented refresh token with a safe error classification."""

    def __init__(self, microsoft_error_code: str | None) -> None:
        self.microsoft_error_code = microsoft_error_code
        self.requires_interactive_mfa = microsoft_error_code in _INTERACTIVE_MFA_CODES
        super().__init__("Microsoft rejected the uploaded refresh token; run the desktop token app again")


class MicrosoftSessionValidator:
    """Prove an uploaded session by exchanging its refresh token with Microsoft."""

    def __init__(
        self,
        allowed_tenant_ids: set[str] | frozenset[str],
        *,
        oauth_exchanger: Callable[[str, str, float], dict[str, Any]] = exchange_refresh_token,
        timeout_seconds: float = 45,
    ) -> None:
        self.allowed_tenant_ids = frozenset(value.strip().lower() for value in allowed_tenant_ids if value.strip())
        if not self.allowed_tenant_ids:
            raise ValueError("At least one allowed Microsoft tenant is required")
        self.oauth_exchanger = oauth_exchanger
        self.timeout_seconds = timeout_seconds

    def validate(self, files: dict[str, bytes], presented_token: str) -> dict[str, Any]:
        presented_claims = decode_jwt_claims(presented_token)
        self._validate_claims(presented_claims, label="uploaded")

        refresh_name = _newest_refresh_name(files)
        try:
            previous_oauth = json.loads(files[refresh_name].decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BundleValidationError("The uploaded refresh-token payload is not valid JSON") from exc
        if not isinstance(previous_oauth, dict):
            raise BundleValidationError("The uploaded refresh-token payload has the wrong JSON shape")
        refresh_token = str(previous_oauth.get("refresh_token") or "")
        if not refresh_token:
            raise BundleValidationError("The uploaded OAuth payload has no refresh token")

        tenant_id = str(presented_claims["tid"]).lower()
        try:
            refreshed_oauth = self.oauth_exchanger(tenant_id, refresh_token, self.timeout_seconds)
        except OAuthRefreshRejected as exc:
            # OAuthRefreshRejected contains Microsoft's HTTP/AADSTS explanation,
            # but never the submitted refresh token.  Keep the client response
            # generic while retaining enough information in the private service
            # journal to diagnose Conditional Access and device-bound sessions.
            LOGGER.warning(
                "Microsoft rejected uploaded session proof tenant=%s reason=%s",
                tenant_id,
                exc,
            )
            match = _AADSTS_CODE.search(str(exc))
            code = match.group(1).upper() if match else None
            raise MicrosoftSessionRejected(code) from exc
        except OAuthRefreshUnavailable as exc:
            raise SessionProofUnavailable(str(exc)) from exc
        except Exception as exc:
            raise SessionProofUnavailable(f"Microsoft session proof failed unexpectedly: {exc}") from exc

        refreshed_token = str(refreshed_oauth.get("access_token") or "")
        if not refreshed_token:
            raise SessionProofUnavailable("Microsoft OAuth succeeded without an access token")
        refreshed_claims = decode_jwt_claims(refreshed_token)
        self._validate_claims(refreshed_claims, label="refreshed")
        if account_id_from_claims(refreshed_claims) != account_id_from_claims(presented_claims):
            raise BundleValidationError("Microsoft refreshed a different account than the uploaded session")

        _replace_access_token(files, presented_token, refreshed_token)
        combined = dict(previous_oauth)
        combined.update(refreshed_oauth)
        if not refreshed_oauth.get("refresh_token"):
            combined["refresh_token"] = refresh_token
        combined["refreshed_at"] = time.time()
        files[refresh_name] = (json.dumps(combined, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        return refreshed_claims

    def _validate_claims(self, claims: dict[str, Any], *, label: str) -> None:
        tenant_id = str(claims.get("tid") or "").strip().lower()
        if not _TENANT_ID.fullmatch(tenant_id) or tenant_id not in self.allowed_tenant_ids:
            raise BundleValidationError(f"The {label} Microsoft tenant is not allowed to contribute accounts")
        if claims.get("aud") != EXPECTED_AUDIENCE:
            raise BundleValidationError(f"The {label} access token is not for the Copilot Sydney service")
        client_id = claims.get("appid") or claims.get("azp")
        if client_id != CLIENT_ID:
            raise BundleValidationError(f"The {label} access token was issued to an unexpected client")
        valid_issuers = {
            f"https://sts.windows.net/{tenant_id}/",
            f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        }
        if claims.get("iss") not in valid_issuers:
            raise BundleValidationError(f"The {label} token issuer does not match its tenant")
        try:
            expires_at = float(claims["exp"])
            not_before = float(claims.get("nbf") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise BundleValidationError(f"The {label} Microsoft access token has invalid timing claims") from exc
        now = time.time()
        if expires_at <= now + 60:
            raise BundleValidationError(f"The {label} Microsoft access token is expired")
        if not_before > now + 300:
            raise BundleValidationError(f"The {label} Microsoft access token is not active yet")
        account_id_from_claims(claims)


def _newest_refresh_name(files: dict[str, bytes]) -> str:
    names = sorted(
        name
        for name in files
        if name.startswith("private_edge_msal_refresh_token_") and name.endswith(".json")
    )
    if not names:
        raise BundleValidationError("The bundle has no Microsoft refresh-token payload")
    return names[-1]


def _replace_access_token(files: dict[str, bytes], previous: str, refreshed: str) -> None:
    frames_name = _newest_json_name(files, "private_websocket_raw_frames_")
    templates_name = _newest_json_name(files, "private_replay_templates_")
    try:
        frames = json.loads(files[frames_name].decode("utf-8-sig"))
        templates = json.loads(files[templates_name].decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleValidationError("The uploaded runtime token files are malformed") from exc
    if not isinstance(frames, list) or not isinstance(templates, list):
        raise BundleValidationError("The uploaded runtime token files have the wrong JSON shape")

    frame_updates = 0
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        url = str(frame.get("url") or "")
        if "substrate.office.com/m365copilot/chathub" not in url.lower():
            continue
        split = urlsplit(url)
        query = dict(parse_qsl(split.query, keep_blank_values=True))
        if query.get("access_token") != previous:
            raise BundleValidationError("The websocket session contains inconsistent access tokens")
        query["access_token"] = refreshed
        frame["url"] = urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))
        frame_updates += 1
    if not frame_updates:
        raise BundleValidationError("The uploaded access token was not found in its runtime material")

    for template in templates:
        if not isinstance(template, dict) or not isinstance(template.get("headers"), dict):
            continue
        for key in list(template["headers"]):
            if key.lower() == "authorization":
                template["headers"][key] = f"Bearer {refreshed}"

    files[frames_name] = (json.dumps(frames, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    files[templates_name] = (json.dumps(templates, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if find_access_token(frames) != refreshed:
        raise BundleValidationError("The refreshed access token was not installed in the websocket capture")


def _newest_json_name(files: dict[str, bytes], prefix: str) -> str:
    names = sorted(name for name in files if name.startswith(prefix) and name.endswith(".json"))
    if not names:
        raise BundleValidationError(f"The bundle is missing a {prefix}*.json file")
    return names[-1]
