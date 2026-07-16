from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

from . import __version__
from .upload import (
    ClientConfig,
    ServerRejectedError,
    _client_installation_id,
    _request_with_retry,
    _sign,
)
from .storage import application_dir


def poll_admin_commands(
    config: ClientConfig,
    *,
    account_ids: list[str],
    status: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "client_id": _client_installation_id(),
        "observed_at": time.time(),
        "app_version": __version__,
        "account_ids": list(dict.fromkeys(account_ids)),
        "status": status,
    }
    try:
        return _signed_json_post(config, "/v1/client/commands/poll", payload, timeout_seconds=8)
    except ServerRejectedError as exc:
        if exc.status_code == 404 and exc.detail.get("error") == "not_found":
            return {"ok": True, "supported": False, "command": None, "poll_after_seconds": 300}
        raise


def complete_admin_command(
    config: ClientConfig,
    *,
    command_id: str,
    succeeded: bool,
    result: dict[str, Any],
) -> dict[str, Any]:
    return _signed_json_post(
        config,
        "/v1/client/commands/complete",
        {
            "client_id": _client_installation_id(),
            "command_id": command_id,
            "succeeded": bool(succeeded),
            "result": result,
        },
        timeout_seconds=8,
    )


def queue_admin_command_result(
    *,
    command_id: str,
    succeeded: bool,
    result: dict[str, Any],
) -> None:
    path = application_dir() / "pending-admin-results.json"
    pending = _load_pending_results(path)
    pending[command_id] = {
        "command_id": command_id,
        "succeeded": bool(succeeded),
        "result": result,
        "queued_at": time.time(),
    }
    _save_pending_results(path, pending)


def flush_admin_command_results(config: ClientConfig) -> int:
    path = application_dir() / "pending-admin-results.json"
    pending = _load_pending_results(path)
    sent = 0
    for command_id, item in list(pending.items()):
        try:
            complete_admin_command(
                config,
                command_id=command_id,
                succeeded=bool(item.get("succeeded")),
                result=item.get("result") if isinstance(item.get("result"), dict) else {},
            )
        except ServerRejectedError as exc:
            if exc.status_code != 409 or exc.detail.get("error") != "command_not_active":
                continue
        except Exception:
            continue
        pending.pop(command_id, None)
        sent += 1
    _save_pending_results(path, pending)
    return sent


def _load_pending_results(path) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, dict) and len(str(key)) == 32
    }


def _save_pending_results(path, pending: dict[str, dict[str, Any]]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(pending, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _signed_json_post(
    config: ClientConfig,
    path: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    body = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _request_with_retry(
        lambda: _sign(
            urllib.request.Request(
                config.endpoint + path,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "FMBSM-Token-Pool-Client/1",
                },
                method="POST",
            ),
            config.upload_key,
            body,
        ),
        config,
        timeout_seconds=timeout_seconds,
        attempts=2,
    )
