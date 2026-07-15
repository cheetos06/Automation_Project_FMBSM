from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1


def default_data_dir() -> Path:
    configured = os.getenv("COPILOT_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[1] / "data" / "copilot").resolve()


@dataclass(frozen=True)
class AccountRecord:
    account_id: str
    username: str
    tenant_id: str
    object_id: str
    session_path: Path
    uploaded_at: float
    access_expires_at: float
    refresh_expires_at: float | None
    enabled: bool
    cooldown_until: float
    cooldown_reason: str | None
    last_error: str | None
    last_used_at: float | None
    total_turns: int

    def as_public_dict(self, *, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else now
        data = {
            "account_id": self.account_id,
            "username": _mask_username(self.username),
            "uploaded_at": self.uploaded_at,
            "access_expires_at": self.access_expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "enabled": self.enabled,
            "cooldown_until": self.cooldown_until,
            "cooldown_reason": self.cooldown_reason,
            "last_used_at": self.last_used_at,
            "total_turns": self.total_turns,
        }
        data["access_valid"] = self.access_expires_at > current + 60
        data["cooling_down"] = self.cooldown_until > current
        data["cooldown_remaining_seconds"] = round(max(0.0, self.cooldown_until - current), 1)
        data["access_remaining_seconds"] = round(max(0.0, self.access_expires_at - current), 1)
        return data


def _mask_username(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return (local[:1] + "***") if local else "unknown"
    visible = local[:2] if len(local) > 1 else local[:1]
    return f"{visible}***@{domain}"


class CopilotRegistry:
    """Process-safe account metadata, turn accounting, and cooldown state."""

    def __init__(self, data_dir: Path | str | None = None, db_path: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir or default_data_dir()).expanduser().resolve()
        self.accounts_dir = self.data_dir / "accounts"
        self.db_path = Path(db_path).expanduser().resolve() if db_path else self.data_dir / "registry.sqlite3"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_env(cls) -> "CopilotRegistry":
        data_dir = os.getenv("COPILOT_DATA_DIR") or None
        db_path = os.getenv("COPILOT_REGISTRY_DB") or None
        return cls(data_dir=data_dir, db_path=db_path)

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    session_path TEXT NOT NULL,
                    uploaded_at REAL NOT NULL,
                    access_expires_at REAL NOT NULL,
                    refresh_expires_at REAL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    cooldown_reason TEXT,
                    last_error TEXT,
                    last_used_at REAL,
                    total_turns INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    dispatched_at REAL NOT NULL,
                    job_id TEXT,
                    operation TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_turns_account_time
                    ON turns(account_id, dispatched_at);
                CREATE TABLE IF NOT EXISTS uploads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    uploaded_at REAL NOT NULL,
                    source_version TEXT,
                    bundle_sha256 TEXT NOT NULL,
                    remote_address TEXT
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def upsert_account(
        self,
        *,
        account_id: str,
        username: str,
        tenant_id: str,
        object_id: str,
        session_path: Path,
        uploaded_at: float,
        access_expires_at: float,
        refresh_expires_at: float | None,
        source_version: str,
        bundle_sha256: str,
        remote_address: str | None,
    ) -> AccountRecord:
        with self._connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO accounts(
                    account_id, username, tenant_id, object_id, session_path,
                    uploaded_at, access_expires_at, refresh_expires_at, enabled,
                    cooldown_until, cooldown_reason, last_error, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, 0, NULL, NULL, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    username=excluded.username,
                    tenant_id=excluded.tenant_id,
                    object_id=excluded.object_id,
                    session_path=excluded.session_path,
                    uploaded_at=excluded.uploaded_at,
                    access_expires_at=excluded.access_expires_at,
                    refresh_expires_at=excluded.refresh_expires_at,
                    enabled=1,
                    last_error=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    account_id,
                    username,
                    tenant_id,
                    object_id,
                    str(session_path.resolve()),
                    uploaded_at,
                    access_expires_at,
                    refresh_expires_at,
                    uploaded_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO uploads(account_id, uploaded_at, source_version, bundle_sha256, remote_address)
                VALUES(?, ?, ?, ?, ?)
                """,
                (account_id, uploaded_at, source_version, bundle_sha256, remote_address),
            )
        record = self.get_account(account_id)
        if record is None:
            raise RuntimeError(f"Account {account_id} disappeared after upsert")
        return record

    def get_account(self, account_id: str) -> AccountRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
        return self._record(row) if row else None

    def list_accounts(self, *, enabled_only: bool = True) -> list[AccountRecord]:
        query = "SELECT * FROM accounts"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY uploaded_at DESC, account_id"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._record(row) for row in rows]

    def update_token_state(
        self,
        account_id: str,
        *,
        session_path: Path | None = None,
        access_expires_at: float,
        refresh_expires_at: float | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect(immediate=True) as connection:
            if session_path is None:
                connection.execute(
                    """
                    UPDATE accounts
                    SET access_expires_at=?, refresh_expires_at=COALESCE(?, refresh_expires_at),
                        last_error=?, updated_at=?
                    WHERE account_id=?
                    """,
                    (access_expires_at, refresh_expires_at, error, time.time(), account_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE accounts
                    SET session_path=?, access_expires_at=?,
                        refresh_expires_at=COALESCE(?, refresh_expires_at),
                        last_error=?, updated_at=?
                    WHERE account_id=?
                    """,
                    (
                        str(session_path.resolve()),
                        access_expires_at,
                        refresh_expires_at,
                        error,
                        time.time(),
                        account_id,
                    ),
                )

    def mark_error(self, account_id: str, message: str, *, disable: bool = False) -> None:
        with self._connect(immediate=True) as connection:
            connection.execute(
                "UPDATE accounts SET last_error=?, enabled=CASE WHEN ? THEN 0 ELSE enabled END, updated_at=? WHERE account_id=?",
                (message[:1000], int(disable), time.time(), account_id),
            )

    def reserve_turn(
        self,
        account_id: str,
        *,
        turn_limit: int,
        window_seconds: float,
        cooldown_seconds: float,
        job_id: str | None = None,
        operation: str | None = None,
    ) -> tuple[bool, float, str | None, int]:
        """Atomically reserve one turn and return availability plus current count."""

        now = time.time()
        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT enabled, cooldown_until FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if row is None:
                return False, 0.0, "missing", 0
            if not bool(row["enabled"]):
                return False, 0.0, "disabled", 0

            cooldown_until = float(row["cooldown_until"] or 0.0)
            if cooldown_until > now:
                count = self._turn_count(connection, account_id, now - window_seconds)
                return False, cooldown_until - now, "cooldown", count
            if cooldown_until:
                connection.execute(
                    "UPDATE accounts SET cooldown_until=0, cooldown_reason=NULL, updated_at=? WHERE account_id=?",
                    (now, account_id),
                )

            connection.execute(
                "DELETE FROM turns WHERE account_id=? AND dispatched_at<?",
                (account_id, now - window_seconds),
            )
            count = self._turn_count(connection, account_id, now - window_seconds)
            if count >= turn_limit:
                until = now + cooldown_seconds
                connection.execute(
                    "UPDATE accounts SET cooldown_until=?, cooldown_reason='turn_limit', updated_at=? WHERE account_id=?",
                    (until, now, account_id),
                )
                return False, cooldown_seconds, "turn_limit", count

            connection.execute(
                "INSERT INTO turns(account_id, dispatched_at, job_id, operation) VALUES(?, ?, ?, ?)",
                (account_id, now, job_id, operation),
            )
            new_count = count + 1
            cooldown_until_after = now + cooldown_seconds if new_count >= turn_limit else 0.0
            cooldown_reason = "turn_limit" if cooldown_until_after else None
            connection.execute(
                """
                UPDATE accounts
                SET last_used_at=?, total_turns=total_turns+1,
                    cooldown_until=?, cooldown_reason=?, updated_at=?
                WHERE account_id=?
                """,
                (now, cooldown_until_after, cooldown_reason, now, account_id),
            )
            return True, 0.0, cooldown_reason, new_count

    def start_cooldown(self, account_id: str, *, seconds: float, reason: str, error: str | None = None) -> None:
        now = time.time()
        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT cooldown_until FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if row is None:
                return
            until = max(float(row["cooldown_until"] or 0.0), now + seconds)
            connection.execute(
                """
                UPDATE accounts SET cooldown_until=?, cooldown_reason=?, last_error=?, updated_at=?
                WHERE account_id=?
                """,
                (until, reason, error[:1000] if error else None, now, account_id),
            )

    def status(self) -> dict[str, Any]:
        now = time.time()
        accounts = self.list_accounts(enabled_only=False)
        with self._connect() as connection:
            recent_turns = connection.execute(
                "SELECT COUNT(*) FROM turns WHERE dispatched_at>=?",
                (now - 3600,),
            ).fetchone()[0]
            upload_count = connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
        return {
            "schema_version": SCHEMA_VERSION,
            "now": now,
            "account_count": len(accounts),
            "enabled_account_count": sum(account.enabled for account in accounts),
            "available_account_count": sum(
                account.enabled
                and account.cooldown_until <= now
                and (account.access_expires_at > now + 60 or account.refresh_expires_at is None or account.refresh_expires_at > now)
                for account in accounts
            ),
            "recent_turns": int(recent_turns),
            "upload_count": int(upload_count),
            "accounts": [account.as_public_dict(now=now) for account in accounts],
        }

    @staticmethod
    def _turn_count(connection: sqlite3.Connection, account_id: str, cutoff: float) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM turns WHERE account_id=? AND dispatched_at>=?",
                (account_id, cutoff),
            ).fetchone()[0]
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> AccountRecord:
        return AccountRecord(
            account_id=str(row["account_id"]),
            username=str(row["username"]),
            tenant_id=str(row["tenant_id"]),
            object_id=str(row["object_id"]),
            session_path=Path(str(row["session_path"])),
            uploaded_at=float(row["uploaded_at"]),
            access_expires_at=float(row["access_expires_at"]),
            refresh_expires_at=float(row["refresh_expires_at"]) if row["refresh_expires_at"] is not None else None,
            enabled=bool(row["enabled"]),
            cooldown_until=float(row["cooldown_until"] or 0.0),
            cooldown_reason=str(row["cooldown_reason"]) if row["cooldown_reason"] else None,
            last_error=str(row["last_error"]) if row["last_error"] else None,
            last_used_at=float(row["last_used_at"]) if row["last_used_at"] is not None else None,
            total_turns=int(row["total_turns"] or 0),
        )
