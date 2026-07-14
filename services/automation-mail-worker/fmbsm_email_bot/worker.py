from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from copilot_service.job_status import JobStatusStore

from .config import Settings, ensure_directories, load_settings
from .files import safe_filename, unique_path
from .fs_job import prepare_ticket, run_fs_review
from .logging_setup import setup_logging
from .mail import GmailClient, InboundEmail, get_payload_bytes
from .state import MessageStore, OutboundRateLimitExceeded
from .zip_utils import safe_extract_files, safe_extract_pdfs

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="FMB SM automation email worker")
    parser.add_argument("--once", action="store_true", help="Run one inbox check and exit")
    args = parser.parse_args()

    settings = load_settings()
    ensure_directories(settings)
    setup_logging(settings)
    store = MessageStore(settings.state_dir / "processed_messages.json")
    interrupted = store.mark_interrupted_processing()
    if interrupted:
        logger.warning("Marked %s interrupted processing job(s) for retry after restart", interrupted)
    mail_client = GmailClient(settings)

    logger.info("Starting fmbsm-email-bot for %s", settings.gmail_address)
    if args.once:
        process_cycle(settings, mail_client, store)
        return

    while True:
        try:
            process_cycle(settings, mail_client, store)
        except Exception:
            logger.exception("Inbox cycle failed")
        time.sleep(settings.poll_interval_seconds)


def process_cycle(settings: Settings, mail_client: GmailClient, store: MessageStore) -> None:
    unread = mail_client.fetch_unread()
    jobs_started = 0

    for inbound in unread:
        if _should_mark_bot_message_seen(settings, inbound):
            logger.info("Marking bot-generated UID %s in %s as read", inbound.uid, inbound.mailbox)
            mail_client.mark_seen(inbound.uid, inbound.mailbox)
            continue

        job_kind = _job_kind(settings, inbound.subject)
        if job_kind is None:
            logger.debug("Skipping UID %s with subject: %s", inbound.uid, inbound.subject)
            continue

        if jobs_started >= settings.max_messages_per_cycle:
            logger.warning(
                "Reached MAX_MESSAGES_PER_CYCLE=%s; remaining matching emails wait for next cycle",
                settings.max_messages_per_cycle,
            )
            break

        key = inbound.message_id or f"{inbound.mailbox}:uid:{inbound.uid}"
        should_skip, reason = store.should_skip(key, processing_lock_seconds=settings.processing_lock_seconds)
        if should_skip and reason in {"processed", "failed"}:
            logger.info("UID %s already has terminal status %s; marking read", inbound.uid, reason)
            _finalize_message(settings, mail_client, inbound)
            continue
        if should_skip:
            logger.debug("UID %s is already being processed; skipping this cycle", inbound.uid)
            continue
        if reason == "interrupted":
            logger.warning("UID %s was interrupted by a previous restart; retrying", inbound.uid)
        if reason == "stale_processing":
            logger.warning("UID %s has a stale processing lock; retrying", inbound.uid)

        record = store.get(key) or {}
        attempts = _safe_int(record.get("attempts"), default=0)
        if attempts >= settings.max_processing_attempts:
            logger.error(
                "UID %s exceeded MAX_PROCESSING_ATTEMPTS=%s; marking read and failed",
                inbound.uid,
                settings.max_processing_attempts,
            )
            store.mark_finished(
                key,
                status="failed",
                job_id=str(record.get("job_id") or ""),
                error="Exceeded max processing attempts after interrupted/crashed runs",
            )
            _finalize_message(settings, mail_client, inbound)
            continue

        process_email(settings, mail_client, store, inbound, key, job_kind=job_kind)
        jobs_started += 1


def process_email(
    settings: Settings,
    mail_client: GmailClient,
    store: MessageStore,
    inbound: InboundEmail,
    key: str,
    *,
    job_kind: str,
) -> None:
    job_id = _new_job_id(inbound.uid)
    job_dir = settings.jobs_dir / job_id
    attachments_dir = job_dir / "attachments"
    extracted_dir = job_dir / "extracted"
    output_dir = job_dir / "output"
    job_dir.mkdir(parents=True, exist_ok=False)

    store.mark_processing(key, uid=inbound.uid, subject=inbound.subject, job_id=job_id)
    status_store = JobStatusStore(settings.state_dir / "job-status")
    status_store.update(
        job_id,
        kind=job_kind,
        stage="email_received",
        message=f"Accepted {job_kind} request from email UID {inbound.uid}",
    )
    logger.info("Processing UID %s as %s job %s", inbound.uid, job_kind, job_id)

    notification_sent = False
    try:
        (job_dir / "email.eml").write_bytes(inbound.raw)
        with mail_client.smtp_session() as smtp:
            if not (store.get(key) or {}).get("ack_sent_at"):
                _send_limited_reply(
                    settings,
                    store,
                    mail_client,
                    inbound,
                    subject=f"Re: {inbound.subject}",
                    body=_ack_body(job_kind, job_id),
                    headers={"X-FMBSM-Job-ID": job_id, "X-FMBSM-Reply-Type": "ack"},
                    smtp=smtp,
                )
                store.mark_ack_sent(key)
                status_store.update(
                    job_id,
                    kind=job_kind,
                    stage="acknowledged",
                    message="Acknowledgement email sent; input validation is starting",
                )
                notification_sent = True
            else:
                logger.info("Acknowledgement already sent for UID %s; not sending another ack", inbound.uid)

            if job_kind == "fs_review":
                input_paths = save_fs_attachments(inbound, attachments_dir, extracted_dir, settings)
                ticket_dir = prepare_ticket(input_paths, job_dir)
                logger.info("Job %s collected %s FS input file(s)", job_id, len(input_paths))
                outputs = run_fs_review(
                    ticket_dir=ticket_dir,
                    output_dir=output_dir,
                    job_id=job_id,
                    year=_subject_year(inbound.subject, settings.fs_default_year),
                    timeout_seconds=settings.fs_review_timeout_seconds,
                    project_dir=settings.project_dir,
                    status_store=status_store,
                    maximum_result_bytes=settings.max_result_attachment_bytes,
                )
                processed_label = "financial-statement review"
            else:
                pdf_paths = save_accepted_attachments(inbound, attachments_dir, extracted_dir, settings)
                if not pdf_paths:
                    raise RuntimeError(
                        "No accepted PDF files found. Attach .pdf files or a .zip/.7z containing PDFs."
                    )
                logger.info("Job %s collected %s PDF(s)", job_id, len(pdf_paths))
                outputs = create_excel_outputs_isolated(
                    pdf_paths,
                    output_dir,
                    job_id,
                    label_root=job_dir,
                    settings=settings,
                )
                processed_label = f"{len(pdf_paths)} PDF signature/date extraction"
            result_subject = f"Re: {inbound.subject}"
            result_body = (
                "Hello,\n\n"
                f"Completed {processed_label}. The result file(s) are attached.\n\n"
                f"Job ID: {job_id}\n"
            )
            result_headers = {"X-FMBSM-Job-ID": job_id, "X-FMBSM-Reply-Type": "result"}
            try:
                _send_limited_reply(
                    settings,
                    store,
                    mail_client,
                    inbound,
                    subject=result_subject,
                    body=result_body,
                    attachments=outputs,
                    headers=result_headers,
                    smtp=smtp,
                )
            except (OSError, smtplib.SMTPException):
                logger.warning("Reused SMTP session failed for job %s; retrying result with a fresh session", job_id)
                _send_limited_reply(
                    settings,
                    store,
                    mail_client,
                    inbound,
                    subject=result_subject,
                    body=result_body,
                    attachments=outputs,
                    headers=result_headers,
                )
        notification_sent = True
        _finalize_message(settings, mail_client, inbound)
        _move_job(job_dir, settings.processed_dir / job_id)
        store.mark_finished(key, status="processed", job_id=job_id)
        status_store.update(
            job_id,
            kind=job_kind,
            stage="email_sent",
            message="Result email sent and job archived",
            finished_at=time.time(),
        )
        logger.info("Job %s completed successfully", job_id)
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        try:
            _send_limited_reply(
                settings,
                store,
                mail_client,
                inbound,
                subject=f"Re: {inbound.subject}",
                body=(
                    "Hello,\n\n"
                    f"The {job_kind.replace('_', ' ')} job failed for your email.\n\n"
                    f"Job ID: {job_id}\n"
                    f"Error: {exc}\n\n"
                    "Check the required attachment names and resend the request.\n"
                ),
                headers={"X-FMBSM-Job-ID": job_id, "X-FMBSM-Reply-Type": "error"},
            )
            notification_sent = True
        except OutboundRateLimitExceeded:
            logger.critical("Outbound rate limit reached; failure reply suppressed for job %s", job_id)
        except Exception:
            logger.exception("Could not send failure reply for job %s", job_id)

        if settings.mark_failed_as_read or notification_sent:
            try:
                _finalize_message(settings, mail_client, inbound)
            except Exception:
                logger.exception("Could not mark failed UID %s as read", inbound.uid)

        failed_destination = settings.failed_dir / job_id
        if job_dir.exists():
            _move_job(job_dir, failed_destination)
        store.mark_finished(key, status="failed", job_id=job_id, error=str(exc))
        status_store.update(
            job_id,
            kind=job_kind,
            stage="failed",
            message=str(exc),
            error=str(exc)[:4000],
            finished_at=time.time(),
        )


def save_accepted_attachments(
    inbound: InboundEmail,
    attachments_dir: Path,
    extracted_dir: Path,
    settings: Settings,
) -> list[Path]:
    pdf_paths: list[Path] = []
    attachment_index = 0

    for part in inbound.message.walk():
        if part.is_multipart():
            continue

        filename = part.get_filename()
        content_type = part.get_content_type()
        if not filename and content_type not in {
            "application/pdf",
            "application/zip",
            "application/x-zip-compressed",
            "application/x-7z-compressed",
        }:
            continue

        attachment_index += 1
        filename = filename or _fallback_attachment_name(attachment_index, content_type)
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".zip", ".7z"}:
            continue

        payload = get_payload_bytes(part)
        if not payload:
            logger.warning("Skipping empty attachment %s on UID %s", filename, inbound.uid)
            continue
        if len(payload) > settings.max_attachment_bytes:
            raise RuntimeError(f"Attachment {filename} exceeds size limit")

        safe_name = safe_filename(filename, f"attachment_{attachment_index}{suffix}")
        attachment_path = unique_path(attachments_dir, safe_name)
        attachment_path.write_bytes(payload)
        logger.info("Saved attachment %s", attachment_path)

        if suffix == ".pdf":
            pdf_paths.append(attachment_path)
        else:
            archive_destination = extracted_dir / attachment_path.stem
            extracted_pdfs = safe_extract_pdfs(
                attachment_path,
                archive_destination,
                max_extracted_bytes=settings.max_zip_extracted_bytes,
                max_files=settings.max_zip_files,
                max_depth=settings.max_archive_depth,
            )
            logger.info("Extracted %s PDF(s) from %s", len(extracted_pdfs), attachment_path.name)
            pdf_paths.extend(extracted_pdfs)

    return pdf_paths


def save_fs_attachments(
    inbound: InboundEmail,
    attachments_dir: Path,
    extracted_dir: Path,
    settings: Settings,
) -> list[Path]:
    accepted: list[Path] = []
    allowed = {".pdf", ".xlsx", ".xlsm"}
    attachment_index = 0
    for part in inbound.message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        content_type = part.get_content_type()
        if not filename and content_type not in {
            "application/pdf",
            "application/zip",
            "application/x-zip-compressed",
            "application/x-7z-compressed",
        }:
            continue
        attachment_index += 1
        filename = filename or _fallback_attachment_name(attachment_index, content_type)
        suffix = Path(filename).suffix.lower()
        if suffix not in {*allowed, ".zip", ".7z"}:
            continue
        payload = get_payload_bytes(part)
        if not payload:
            logger.warning("Skipping empty FS attachment %s on UID %s", filename, inbound.uid)
            continue
        if len(payload) > settings.max_attachment_bytes:
            raise RuntimeError(f"Attachment {filename} exceeds size limit")
        attachment_path = unique_path(
            attachments_dir,
            safe_filename(filename, f"attachment_{attachment_index}{suffix}"),
        )
        attachment_path.write_bytes(payload)
        logger.info("Saved FS attachment %s", attachment_path)
        if suffix in allowed:
            accepted.append(attachment_path)
            continue
        archive_destination = extracted_dir / attachment_path.stem
        extracted = safe_extract_files(
            attachment_path,
            archive_destination,
            allowed_suffixes=allowed,
            max_extracted_bytes=settings.max_zip_extracted_bytes,
            max_files=settings.max_zip_files,
            max_depth=settings.max_archive_depth,
        )
        logger.info("Extracted %s FS input(s) from %s", len(extracted), attachment_path.name)
        accepted.extend(extracted)
    if not accepted:
        raise RuntimeError(
            "No FS review inputs found. Attach financial_statements_N.pdf, optional "
            "financial_statements_N_1.pdf, and bg_standardized.xlsx."
        )
    return accepted


def _fallback_attachment_name(index: int, content_type: str) -> str:
    if content_type == "application/pdf":
        return f"attachment_{index}.pdf"
    if content_type == "application/x-7z-compressed":
        return f"attachment_{index}.7z"
    return f"attachment_{index}.zip"


def create_excel_outputs_isolated(
    pdf_paths: list[Path],
    output_dir: Path,
    job_id: str,
    *,
    label_root: Path,
    settings: Settings,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "extract_manifest.json"
    result_path = output_dir / "extract_outputs.json"
    manifest_path.write_text(
        json.dumps(
            {
                "pdf_paths": [str(path) for path in pdf_paths],
                "output_dir": str(output_dir),
                "job_id": job_id,
                "label_root": str(label_root),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"

    started_at = time.perf_counter()
    logger.info("Job %s starting isolated extraction for %s PDF(s)", job_id, len(pdf_paths))
    completed = subprocess.run(
        [sys.executable, "-m", "fmbsm_email_bot.extract_cli", str(manifest_path), str(result_path)],
        cwd=str(settings.project_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=settings.extraction_timeout_seconds,
    )
    elapsed = time.perf_counter() - started_at

    if completed.stdout:
        logger.info("Job %s extractor stdout: %s", job_id, completed.stdout.strip())
    if completed.stderr:
        logger.warning("Job %s extractor stderr: %s", job_id, completed.stderr.strip())
    if completed.returncode != 0:
        raise RuntimeError(
            f"Extractor failed after {elapsed:.2f}s with exit code {completed.returncode}. "
            f"stderr: {completed.stderr.strip()[:1000]}"
        )
    if not result_path.exists():
        raise RuntimeError("Extractor finished without writing output manifest")

    outputs = [Path(value) for value in json.loads(result_path.read_text(encoding="utf-8"))]
    logger.info("Job %s extraction finished in %.2fs", job_id, elapsed)
    return outputs


def _send_limited_reply(
    settings: Settings,
    store: MessageStore,
    mail_client: GmailClient,
    inbound: InboundEmail,
    *,
    subject: str,
    body: str,
    attachments: list[Path] | tuple[Path, ...] = (),
    headers: dict[str, str] | None = None,
    smtp=None,
) -> None:
    store.reserve_outbound_send(
        max_per_hour=settings.max_outbound_emails_per_hour,
        max_per_day=settings.max_outbound_emails_per_day,
    )
    mail_client.send_reply(
        inbound,
        subject=subject,
        body=body,
        attachments=attachments,
        headers=headers,
        smtp=smtp,
    )


def _should_mark_bot_message_seen(settings: Settings, inbound: InboundEmail) -> bool:
    if inbound.bot_generated:
        return True
    sender = inbound.sender.lower()
    own_address = settings.gmail_address.lower()
    return sender == own_address and any(
        inbound.subject.startswith(f"Re: {prefix}")
        for prefix in (settings.subject_prefix, settings.fs_subject_prefix)
    )


def _job_kind(settings: Settings, subject: str) -> str | None:
    if subject.startswith(settings.subject_prefix):
        return "signature_dates"
    if subject.startswith(settings.fs_subject_prefix):
        return "fs_review"
    return None


def _subject_year(subject: str, default: int) -> int:
    match = re.search(r"(?:year|annee|année)\s*[:= -]\s*(20\d{2})", subject, flags=re.IGNORECASE)
    return int(match.group(1)) if match else default


def _ack_body(job_kind: str, job_id: str) -> str:
    label = (
        "financial-statement review"
        if job_kind == "fs_review"
        else "PDF signature/date extraction"
    )
    return (
        "Hello,\n\n"
        f"We received your {label} request and processing has started.\n\n"
        f"Job ID: {job_id}\n"
        "Live progress is recorded on the automation server for support/debugging.\n"
    )


def _finalize_message(settings: Settings, mail_client: GmailClient, inbound: InboundEmail) -> None:
    mail_client.mark_seen(inbound.uid, inbound.mailbox)
    if settings.move_spam_to_inbox and inbound.mailbox != settings.inbox_mailbox:
        try:
            mail_client.move_to_inbox(inbound)
        except Exception:
            logger.exception("Could not move UID %s from %s to inbox", inbound.uid, inbound.mailbox)


def _safe_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _new_job_id(uid: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_uid = safe_filename(uid, "uid")
    return f"{timestamp}_uid-{safe_uid}_{uuid.uuid4().hex[:8]}"


def _move_job(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination = unique_path(destination.parent, destination.name)
    shutil.move(str(source), str(destination))


if __name__ == "__main__":
    main()
