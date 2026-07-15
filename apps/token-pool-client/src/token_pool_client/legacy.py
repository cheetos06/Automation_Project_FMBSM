from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .refresh import account_from_session, refresh_existing
from .storage import AccountStore, ClientAccount


def import_build2(
    build2_dir: Path,
    store: AccountStore,
    *,
    run_refresh: bool = True,
    log: Callable[[str], None] = print,
) -> list[ClientAccount]:
    build2_dir = build2_dir.expanduser().resolve()
    config_path = build2_dir / "config" / "accounts.json"
    refresh_script = build2_dir / "refresh_accounts.py"
    if not config_path.exists():
        raise RuntimeError(f"Build 2 accounts config was not found: {config_path}")
    if run_refresh:
        if not refresh_script.exists():
            raise RuntimeError(f"Build 2 refresh script was not found: {refresh_script}")
        log("Refreshing the existing Build 2 accounts...")
        process = subprocess.Popen(
            [sys.executable, "-u", str(refresh_script)],
            cwd=build2_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log(line.rstrip())
        if process.wait() != 0:
            raise RuntimeError("Build 2 account refresh failed")

    configuration = json.loads(config_path.read_text(encoding="utf-8-sig"))
    imported: list[ClientAccount] = []
    for item in configuration.get("accounts", []):
        if not item.get("enabled", True):
            continue
        source = Path(item["session_dir"])
        if not source.is_absolute():
            source = build2_dir / source
        source = source.resolve()
        source_profile = Path(item.get("edge_profile_path") or source).expanduser().resolve()
        account = account_from_session(source, source_profile)
        if account.access_expires_at <= time.time() + 300:
            account, _ = refresh_existing(account)
        destination, profile = store.account_paths(account.account_id)
        for path in source.iterdir():
            if not path.is_file():
                continue
            if path.name.startswith("private_") or path.name.endswith("_summary.json"):
                shutil.copy2(path, destination / path.name)
        copied = account_from_session(destination, profile)
        copied.last_uploaded_at = account.last_uploaded_at
        store.upsert(copied)
        imported.append(copied)
        log(f"Imported {copied.username} as {copied.account_id}")
    if not imported:
        raise RuntimeError("No enabled Build 2 accounts were imported")
    return imported
