from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def _get_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    raise RuntimeError(f"Environment variable {name} must be true or false")


@dataclass(frozen=True)
class Settings:
    gmail_address: str
    gmail_app_password: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    mailbox: str
    monitored_mailboxes: tuple[str, ...]
    inbox_mailbox: str
    move_spam_to_inbox: bool
    subject_prefix: str
    fs_subject_prefix: str
    fs_default_year: int
    authorized_job_senders: tuple[str, ...]
    authorized_job_sender_domains: tuple[str, ...]
    poll_interval_seconds: int
    log_level: str
    project_dir: Path
    data_dir: Path
    logs_dir: Path
    jobs_dir: Path
    processed_dir: Path
    failed_dir: Path
    state_dir: Path
    max_attachment_bytes: int
    max_zip_extracted_bytes: int
    max_zip_files: int
    max_archive_depth: int
    max_messages_per_cycle: int
    max_outbound_emails_per_hour: int
    max_outbound_emails_per_day: int
    max_processing_attempts: int
    extraction_timeout_seconds: int
    fs_review_timeout_seconds: int
    max_result_attachment_bytes: int
    imap_timeout_seconds: int
    smtp_timeout_seconds: int
    processing_lock_seconds: int
    mark_failed_as_read: bool


def load_settings() -> Settings:
    project_dir = Path(__file__).resolve().parents[1]
    env_file = os.getenv("ENV_FILE")
    load_dotenv(dotenv_path=Path(env_file) if env_file else project_dir / ".env", encoding="utf-8-sig")

    data_dir = Path(os.getenv("BOT_DATA_DIR", str(project_dir))).expanduser()
    authorized_job_senders = tuple(
        value.lower() for value in _get_csv_env("AUTHORIZED_JOB_SENDERS", ())
    )
    authorized_job_sender_domains = tuple(
        value.lower().lstrip("@")
        for value in _get_csv_env("AUTHORIZED_JOB_SENDER_DOMAINS", ())
    )
    if not authorized_job_senders and not authorized_job_sender_domains:
        raise RuntimeError(
            "Configure AUTHORIZED_JOB_SENDERS or AUTHORIZED_JOB_SENDER_DOMAINS; "
            "the mail worker fails closed without a sender allowlist"
        )

    return Settings(
        gmail_address=_get_required_env("GMAIL_ADDRESS"),
        gmail_app_password=_get_required_env("GMAIL_APP_PASSWORD").replace(" ", ""),
        imap_host=os.getenv("IMAP_HOST", "imap.gmail.com").strip(),
        imap_port=_get_int_env("IMAP_PORT", 993),
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
        smtp_port=_get_int_env("SMTP_PORT", 465),
        mailbox=os.getenv("MAILBOX", "INBOX").strip(),
        monitored_mailboxes=_get_csv_env("MONITORED_MAILBOXES", ("INBOX", "[Gmail]/Spam")),
        inbox_mailbox=os.getenv("INBOX_MAILBOX", "INBOX").strip(),
        move_spam_to_inbox=_get_bool_env("MOVE_SPAM_TO_INBOX", True),
        subject_prefix=os.getenv("SUBJECT_PREFIX", "[optimda-extract-dates]"),
        fs_subject_prefix=os.getenv("FS_SUBJECT_PREFIX", "[fs-review]"),
        fs_default_year=_get_int_env("FS_DEFAULT_YEAR", 2025),
        authorized_job_senders=authorized_job_senders,
        authorized_job_sender_domains=authorized_job_sender_domains,
        poll_interval_seconds=_get_int_env("POLL_INTERVAL_SECONDS", 2),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        project_dir=project_dir,
        data_dir=data_dir,
        logs_dir=data_dir / "logs",
        jobs_dir=data_dir / "jobs",
        processed_dir=data_dir / "processed",
        failed_dir=data_dir / "failed",
        state_dir=data_dir / "state",
        max_attachment_bytes=_get_int_env("MAX_ATTACHMENT_BYTES", 100 * 1024 * 1024),
        max_zip_extracted_bytes=_get_int_env("MAX_ZIP_EXTRACTED_BYTES", 500 * 1024 * 1024),
        max_zip_files=_get_int_env("MAX_ZIP_FILES", 1000),
        max_archive_depth=_get_int_env("MAX_ARCHIVE_DEPTH", 5),
        max_messages_per_cycle=_get_int_env("MAX_MESSAGES_PER_CYCLE", 10),
        max_outbound_emails_per_hour=_get_int_env("MAX_OUTBOUND_EMAILS_PER_HOUR", 40),
        max_outbound_emails_per_day=_get_int_env("MAX_OUTBOUND_EMAILS_PER_DAY", 200),
        max_processing_attempts=_get_int_env("MAX_PROCESSING_ATTEMPTS", 3),
        extraction_timeout_seconds=_get_int_env("EXTRACTION_TIMEOUT_SECONDS", 300),
        fs_review_timeout_seconds=_get_int_env("FS_REVIEW_TIMEOUT_SECONDS", 3 * 60 * 60),
        max_result_attachment_bytes=_get_int_env("MAX_RESULT_ATTACHMENT_BYTES", 20 * 1024 * 1024),
        imap_timeout_seconds=_get_int_env("IMAP_TIMEOUT_SECONDS", 30),
        smtp_timeout_seconds=_get_int_env("SMTP_TIMEOUT_SECONDS", 60),
        processing_lock_seconds=_get_int_env("PROCESSING_LOCK_SECONDS", 6 * 60 * 60),
        mark_failed_as_read=_get_bool_env("MARK_FAILED_AS_READ", True),
    )


def ensure_directories(settings: Settings) -> None:
    for directory in (
        settings.logs_dir,
        settings.jobs_dir,
        settings.processed_dir,
        settings.failed_dir,
        settings.state_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def _get_csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    return values or default
