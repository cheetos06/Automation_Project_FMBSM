from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def application_dir() -> Path:
    configured = os.getenv("TOKEN_POOL_CLIENT_DATA", "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "FMBSM" / "TokenPoolClient"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


@dataclass
class ClientAccount:
    account_id: str
    username: str
    tenant_id: str
    object_id: str
    session_dir: str
    profile_dir: str
    access_expires_at: float
    last_uploaded_at: float | None = None
    last_error: str | None = None

    @property
    def session_path(self) -> Path:
        return Path(self.session_dir)

    @property
    def profile_path(self) -> Path:
        return Path(self.profile_dir)


class AccountStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or application_dir()).resolve()
        self.path = self.root / "accounts.json"
        self.accounts_root = self.root / "accounts"
        self.accounts_root.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[ClientAccount]:
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return []
        items = value.get("accounts", []) if isinstance(value, dict) else []
        result: list[ClientAccount] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                result.append(ClientAccount(**item))
            except TypeError:
                continue
        return result

    def save(self, accounts: list[ClientAccount]) -> None:
        payload: dict[str, Any] = {"version": 1, "accounts": [asdict(account) for account in accounts]}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def upsert(self, account: ClientAccount) -> ClientAccount:
        accounts = self.load()
        by_id = {item.account_id: item for item in accounts}
        by_id[account.account_id] = account
        self.save(sorted(by_id.values(), key=lambda item: item.username.lower()))
        return account

    def account_paths(self, account_id: str) -> tuple[Path, Path]:
        root = self.accounts_root / account_id
        session = root / "session"
        profile = root / "edge-profile"
        session.mkdir(parents=True, exist_ok=True)
        profile.mkdir(parents=True, exist_ok=True)
        return session, profile
