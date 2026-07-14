from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

from . import __version__
from .storage import ClientAccount


def create_bundle(account: ClientAccount) -> bytes:
    session = account.session_path
    selections: list[Path] = []
    for prefix in (
        "private_websocket_raw_frames_",
        "private_replay_templates_",
        "private_playwright_cookies_",
        "private_edge_msal_refresh_token_",
    ):
        match = _latest(session, prefix, ".json")
        if match is None:
            raise RuntimeError(f"Session is missing {prefix}*.json")
        selections.append(match)
    for fixed in (
        "private_msal_local_storage_current.json",
        "private_msal_cache_encryption.txt",
    ):
        path = session / fixed
        if path.exists():
            selections.append(path)
    manifest = {
        "version": 1,
        "client_version": __version__,
        "created_at": time.time(),
        "claimed_account_id": account.account_id,
        "claimed_username": account.username,
        "claimed_tenant_id": account.tenant_id,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for path in selections:
            archive.write(path, path.name)
    return output.getvalue()


def _latest(directory: Path, prefix: str, suffix: str) -> Path | None:
    candidates = [path for path in directory.glob(f"{prefix}*{suffix}") if path.is_file()]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
