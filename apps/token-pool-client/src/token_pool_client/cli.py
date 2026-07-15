from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import __version__
from .app import run as run_app
from .bundle import create_bundle
from .legacy import import_build2
from .refresh import refresh_existing
from .storage import AccountStore
from .upload import load_config, server_status, upload_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="FMBSM Copilot Token Pool Client")
    parser.add_argument("--version", action="version", version=f"FMBSM Token Pool Client {__version__}")
    parser.add_argument("--legacy-build2", type=Path, help="Import and upload an existing OPTIMDA Build 2 account pool")
    parser.add_argument("--skip-legacy-refresh", action="store_true")
    parser.add_argument("--refresh-all", action="store_true", help="Refresh/upload configured accounts without the GUI")
    parser.add_argument("--status", action="store_true", help="Print AWS pool status and exit")
    args = parser.parse_args()
    if not any((args.legacy_build2, args.refresh_all, args.status)):
        run_app()
        return 0

    config = load_config()
    store = AccountStore()
    if args.legacy_build2:
        import_build2(
            args.legacy_build2,
            store,
            run_refresh=not args.skip_legacy_refresh,
            log=lambda value: print(value, flush=True),
        )
    if args.legacy_build2 or args.refresh_all:
        failures = 0
        for account in store.load():
            try:
                refreshed, _ = refresh_existing(account)
                response = upload_bundle(config, create_bundle(refreshed))
                refreshed.last_uploaded_at = time.time()
                refreshed.last_error = None
                store.upsert(refreshed)
                print(
                    f"uploaded username={refreshed.username} account={refreshed.account_id} "
                    f"available={response.get('pool', {}).get('available_account_count')}",
                    flush=True,
                )
            except Exception as exc:
                failures += 1
                print(f"failed username={account.username} error={exc}", flush=True)
        if failures:
            return 1
    if args.status:
        print(server_status(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
