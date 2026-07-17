from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import __version__
from . import bootstrap
from .app import run as run_app
from .automation import is_automatic_work_account
from .bundle import create_bundle
from .legacy import import_build2
from .refresh import initialize_refresh
from .storage import AccountStore
from .upload import load_config, server_status, upload_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="FMBSM Copilot Token Pool Client")
    parser.add_argument("--version", action="version", version=f"FMBSM Token Pool Client {__version__}")
    parser.add_argument("--legacy-build2", type=Path, help="Import and upload an existing OPTIMDA Build 2 account pool")
    parser.add_argument("--skip-legacy-refresh", action="store_true")
    parser.add_argument("--refresh-all", action="store_true", help="Fresh-authorize/upload configured accounts without the GUI")
    parser.add_argument("--status", action="store_true", help="Print AWS pool status and exit")
    parser.add_argument("--background", action="store_true", help="Start in the Windows notification area")
    args = parser.parse_args()
    if not any((args.legacy_build2, args.refresh_all, args.status)):
        run_app(background=args.background)
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
                progress = lambda value: print(f"{account.username}: {value}", flush=True)
                try:
                    bootstrap.renew_microsoft_session(
                        site="https://m365.cloud.microsoft/chat",
                        profile_dir=account.profile_path,
                        session_dir=account.session_path,
                        expected_username=account.username,
                        expected_tenant=account.tenant_id,
                        channel="msedge",
                        timeout_seconds=30,
                        progress=progress,
                        mode="silent",
                        prompt_user=False,
                    )
                except bootstrap.InteractiveAuthenticationRequired:
                    bootstrap.renew_microsoft_session(
                        site="https://m365.cloud.microsoft/chat",
                        profile_dir=account.profile_path,
                        session_dir=account.session_path,
                        expected_username=account.username,
                        expected_tenant=account.tenant_id,
                        channel="msedge",
                        timeout_seconds=300,
                        progress=progress,
                        mode="expected_account" if is_automatic_work_account(account.username) else "select_account",
                        prompt_user=False,
                    )
                refreshed, _ = initialize_refresh(
                    account.session_path,
                    account.profile_path,
                    expected_account=account,
                )
                upload_bundle(config, create_bundle(refreshed))
                refreshed.last_uploaded_at = time.time()
                refreshed.last_error = None
                store.upsert(refreshed)
                print(
                    f"uploaded username={refreshed.username} account={refreshed.account_id} "
                    "aws_status=accepted",
                    flush=True,
                )
            except Exception as exc:
                failures += 1
                print(f"failed username={account.username} error={exc}", flush=True)
        if failures:
            return 1
    if args.status:
        accounts = store.load()
        print(server_status(config, account_ids=[account.account_id for account in accounts]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
