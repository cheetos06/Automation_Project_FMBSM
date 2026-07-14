from __future__ import annotations

from typing import Any

import httpx


CLIENT_ID = "4765445b-32c6-49b0-83e6-1d93765276ca"
SCOPE = "https://substrate.office.com/sydney/.default openid profile offline_access"


class OAuthRefreshError(RuntimeError):
    pass


class OAuthRefreshRejected(OAuthRefreshError):
    """Microsoft rejected the submitted refresh-token grant."""


class OAuthRefreshUnavailable(OAuthRefreshError):
    """The Microsoft token endpoint could not provide an authoritative answer."""


def exchange_refresh_token(
    tenant_id: str,
    refresh_token: str,
    timeout_seconds: float = 45,
) -> dict[str, Any]:
    try:
        response = httpx.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded;charset=utf-8",
                "origin": "https://m365.cloud.microsoft",
                "referer": "https://m365.cloud.microsoft/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
            data={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": SCOPE,
                "client_info": "1",
            },
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise OAuthRefreshUnavailable(f"Microsoft OAuth is temporarily unreachable: {exc}") from exc

    try:
        payload = response.json()
    except Exception as exc:
        error_type = OAuthRefreshUnavailable if response.status_code >= 500 else OAuthRefreshRejected
        raise error_type(f"Microsoft OAuth returned non-JSON HTTP {response.status_code}") from exc
    if not isinstance(payload, dict):
        raise OAuthRefreshUnavailable("Microsoft OAuth returned a malformed response")
    if not response.is_success:
        description = str(payload.get("error_description") or payload.get("error") or "unknown error")
        message = f"Microsoft OAuth HTTP {response.status_code}: {description[:500]}"
        if response.status_code >= 500 or response.status_code in {408, 429}:
            raise OAuthRefreshUnavailable(message)
        raise OAuthRefreshRejected(message)
    if not payload.get("access_token"):
        raise OAuthRefreshUnavailable("Microsoft OAuth succeeded without an access token")
    return payload
