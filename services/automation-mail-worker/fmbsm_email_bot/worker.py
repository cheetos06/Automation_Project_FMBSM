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
import threading
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from copilot_service.job_status import JobStatusStore

from .balance_job import run_balance_job
from .config import Settings, ensure_directories, load_settings
from .effectif_job import run_effectif_job
from .files import safe_filename, unique_path
from .fs_job import prepare_ticket, run_fs_review
from .logging_setup import setup_logging
from .mail import GmailClient, InboundEmail, get_payload_bytes, parse_inbound_email
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
        while process_next_queued(settings, mail_client, store):
            pass
        return

    queue_wakeup = threading.Event()
    queue_worker = threading.Thread(
        target=_job_worker,
        args=(settings, mail_client, store, queue_wakeup),
        name="durable-mail-job-worker",
        daemon=True,
    )
    queue_worker.start()
    health_store = JobStatusStore(settings.state_dir / "job-status")
    consecutive_failures = 0
    last_health_update = 0.0
    while True:
        try:
            process_cycle(settings, mail_client, store)
            queue_wakeup.set()
            consecutive_failures = 0
            delay = settings.poll_interval_seconds
            if time.monotonic() - last_health_update >= 60:
                health_store.update(
                    "mail-worker-health",
                    kind="service_health",
                    stage="healthy",
                    message="Gmail polling and the durable job worker are active",
                    queued_jobs=store.queued_count(),
                    active_jobs=store.active_count(),
                    worker_thread_alive=queue_worker.is_alive(),
                )
                last_health_update = time.monotonic()
        except Exception as exc:
            consecutive_failures += 1
            delay = min(300, max(settings.poll_interval_seconds, 2 ** min(consecutive_failures, 8)))
            logger.exception("Inbox cycle failed")
            health_store.update(
                "mail-worker-health",
                kind="service_health",
                stage="imap_degraded",
                message=f"Gmail polling failed; retrying in {delay}s",
                consecutive_failures=consecutive_failures,
                retry_in_seconds=delay,
                error=f"{type(exc).__name__}: {exc}"[:2000],
                queued_jobs=store.queued_count(),
                active_jobs=store.active_count(),
                worker_thread_alive=queue_worker.is_alive(),
            )
        if not queue_worker.is_alive():
            raise RuntimeError("Durable mail job worker stopped unexpectedly")
        time.sleep(delay)


def process_cycle(settings: Settings, mail_client: GmailClient, store: MessageStore) -> None:
    unread = mail_client.fetch_unread()
    jobs_accepted = 0

    for inbound in unread:
        if _should_mark_bot_message_seen(settings, inbound):
            logger.info("Marking bot-generated UID %s in %s as read", inbound.uid, inbound.mailbox)
            mail_client.mark_seen(inbound.uid, inbound.mailbox)
            continue

        job_kind = _job_kind(settings, inbound.subject)
        if job_kind is None:
            logger.debug("Skipping UID %s with subject: %s", inbound.uid, inbound.subject)
            continue
        if not _is_authorized_job_sender(settings, inbound.sender):
            logger.warning(
                "Rejected unauthorized %s request UID %s from %s",
                job_kind,
                inbound.uid,
                inbound.sender or "<missing sender>",
            )
            mail_client.mark_seen(inbound.uid, inbound.mailbox)
            continue

        if jobs_accepted >= settings.max_messages_per_cycle:
            logger.warning(
                "Reached MAX_MESSAGES_PER_CYCLE=%s; remaining matching emails wait for next cycle",
                settings.max_messages_per_cycle,
            )
            break

        key = inbound.message_id or f"{inbound.mailbox}:uid:{inbound.uid}"
        should_skip, reason = store.should_skip(key, processing_lock_seconds=settings.processing_lock_seconds)
        if should_skip and reason in {"processed", "failed", "rejected"}:
            logger.info("UID %s already has terminal status %s; marking read", inbound.uid, reason)
            _finalize_message(settings, mail_client, inbound)
            continue
        if should_skip and reason in {"queued", "processing"}:
            record = store.get(key) or {}
            if not record.get("ack_sent_at"):
                _send_queue_ack(settings, store, mail_client, inbound, key, record)
            _finalize_message(settings, mail_client, inbound)
            continue
        if reason == "stale_processing":
            logger.warning("UID %s has a stale processing lock; queueing recovery", inbound.uid)

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

        if store.queued_count() + store.active_count() >= settings.max_queued_jobs:
            _reject_overloaded(settings, mail_client, store, inbound, key, reason="queue capacity")
            jobs_accepted += 1
            continue
        free_bytes = shutil.disk_usage(settings.data_dir).free
        if free_bytes < settings.min_free_disk_bytes:
            _reject_overloaded(settings, mail_client, store, inbound, key, reason="low server disk space")
            jobs_accepted += 1
            continue

        _enqueue_email(settings, mail_client, store, inbound, key, job_kind=job_kind)
        jobs_accepted += 1


def _job_worker(
    settings: Settings,
    mail_client: GmailClient,
    store: MessageStore,
    wakeup: threading.Event,
) -> None:
    while True:
        try:
            if process_next_queued(settings, mail_client, store):
                continue
        except Exception:
            logger.exception("Unexpected durable queue worker failure")
        wakeup.wait(timeout=1.0)
        wakeup.clear()


def process_next_queued(settings: Settings, mail_client: GmailClient, store: MessageStore) -> bool:
    selected = store.next_queued()
    if selected is None:
        return False
    key, record = selected
    raw_path = Path(str(record.get("raw_path") or ""))
    job_id = str(record.get("job_id") or "")
    archived = settings.processed_dir / job_id
    if record.get("result_sent_at") and job_id and archived.is_dir():
        store.mark_finished(key, status="processed", job_id=job_id)
        JobStatusStore(settings.state_dir / "job-status").update(
            job_id,
            kind=str(record.get("job_kind") or "unknown"),
            stage="email_sent",
            message="Recovered completed archive without duplicate result delivery",
            finished_at=time.time(),
        )
        return True
    if not job_id or not raw_path.is_file():
        error = "Queued job is missing its durable email copy"
        logger.error("Job %s cannot be recovered: %s", job_id or "<missing>", error)
        store.mark_finished(key, status="failed", job_id=job_id, error=error)
        return True
    inbound = parse_inbound_email(
        raw=raw_path.read_bytes(),
        mailbox=str(record.get("mailbox") or settings.inbox_mailbox),
        uid=str(record.get("uid") or "queued"),
    )
    attempts = _safe_int(record.get("attempts"), default=0)
    if attempts >= settings.max_processing_attempts:
        error = f"Exceeded maximum processing attempts ({settings.max_processing_attempts})"
        try:
            _send_limited_reply(
                settings,
                store,
                mail_client,
                inbound,
                subject=f"Re: {inbound.subject}",
                body=(
                    "Hello,\n\nThe automation job could not recover after repeated interrupted "
                    f"attempts.\n\nJob ID: {job_id}\nError: {error}\n"
                ),
                headers={"X-FMBSM-Job-ID": job_id, "X-FMBSM-Reply-Type": "error"},
            )
        except Exception:
            logger.exception("Could not send exhausted-retry notice for job %s", job_id)
        store.mark_finished(key, status="failed", job_id=job_id, error=error)
        JobStatusStore(settings.state_dir / "job-status").update(
            job_id,
            kind=str(record.get("job_kind") or "unknown"),
            stage="failed",
            message=error,
            finished_at=time.time(),
        )
        if raw_path.parent.exists():
            _move_job(raw_path.parent, settings.failed_dir / job_id)
        return True
    process_email(
        settings,
        mail_client,
        store,
        inbound,
        key,
        job_kind=str(record.get("job_kind") or _job_kind(settings, inbound.subject) or ""),
        job_id=job_id,
        job_dir=raw_path.parent,
    )
    return True


def _enqueue_email(
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
    job_dir.mkdir(parents=True, exist_ok=False)
    raw_path = job_dir / "email.eml"
    temporary = job_dir / ".email.eml.tmp"
    with temporary.open("wb") as stream:
        stream.write(inbound.raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(raw_path)
    store.mark_queued(
        key,
        uid=inbound.uid,
        mailbox=inbound.mailbox,
        subject=inbound.subject,
        job_id=job_id,
        job_kind=job_kind,
        raw_path=raw_path,
    )
    status_store = JobStatusStore(settings.state_dir / "job-status")
    snapshot = store.queue_snapshot(key, default_seconds=_queue_defaults(settings))
    status_store.update(
        job_id,
        kind=job_kind,
        stage="queued",
        message="Email saved durably and accepted into the processing queue",
        queue_position=snapshot["position"],
        queue_depth=snapshot["total"],
        estimated_start_seconds=snapshot["estimated_start_seconds"],
        estimated_completion_seconds=snapshot["estimated_completion_seconds"],
    )
    _send_queue_ack(settings, store, mail_client, inbound, key, store.get(key) or {})
    _finalize_message(settings, mail_client, inbound)
    logger.info(
        "Queued UID %s as %s job %s at position %s/%s",
        inbound.uid,
        job_kind,
        job_id,
        snapshot["position"],
        snapshot["total"],
    )


def _send_queue_ack(
    settings: Settings,
    store: MessageStore,
    mail_client: GmailClient,
    inbound: InboundEmail,
    key: str,
    record: dict[str, object],
) -> None:
    snapshot = store.queue_snapshot(key, default_seconds=_queue_defaults(settings))
    job_id = str(record.get("job_id") or "")
    job_kind = str(record.get("job_kind") or _job_kind(settings, inbound.subject) or "request")
    _send_limited_reply(
        settings,
        store,
        mail_client,
        inbound,
        subject=f"Re: {inbound.subject}",
        body=_ack_body(job_kind, job_id, snapshot),
        headers={"X-FMBSM-Job-ID": job_id, "X-FMBSM-Reply-Type": "ack"},
    )
    store.mark_ack_sent(key, queue_position=snapshot["position"])


def _reject_overloaded(
    settings: Settings,
    mail_client: GmailClient,
    store: MessageStore,
    inbound: InboundEmail,
    key: str,
    *,
    reason: str,
) -> None:
    job_id = _new_job_id(inbound.uid)
    _send_limited_reply(
        settings,
        store,
        mail_client,
        inbound,
        subject=f"Re: {inbound.subject}",
        body=(
            "Hello,\n\nThe automation service cannot safely accept this request right now "
            f"because of {reason}. No files were queued. Please resend later.\n\n"
            f"Reference ID: {job_id}\n"
        ),
        headers={"X-FMBSM-Job-ID": job_id, "X-FMBSM-Reply-Type": "busy"},
    )
    store.mark_finished(key, status="rejected", job_id=job_id, error=reason)
    _finalize_message(settings, mail_client, inbound)


def _queue_defaults(settings: Settings) -> dict[str, int]:
    return {
        "fs_review": settings.queue_default_fs_seconds,
        "effectif_payroll": settings.queue_default_effectif_seconds,
        "signature_dates": settings.queue_default_signature_seconds,
        "balance_cleaner": settings.queue_default_balance_seconds,
    }


def process_email(
    settings: Settings,
    mail_client: GmailClient,
    store: MessageStore,
    inbound: InboundEmail,
    key: str,
    *,
    job_kind: str,
    job_id: str,
    job_dir: Path,
) -> None:
    attachments_dir = job_dir / "attachments"
    extracted_dir = job_dir / "extracted"
    output_dir = job_dir / "output"
    job_dir.mkdir(parents=True, exist_ok=True)

    store.mark_processing(
        key,
        uid=inbound.uid,
        subject=inbound.subject,
        job_id=job_id,
        job_kind=job_kind,
    )
    status_store = JobStatusStore(settings.state_dir / "job-status")
    status_store.update(
        job_id,
        kind=job_kind,
        stage="processing_started",
        message=f"Dequeued {job_kind} request and started processing",
        queue_position=1,
        queue_depth=store.queued_count() + 1,
        attempt=(store.get(key) or {}).get("attempts", 1),
    )
    logger.info("Processing UID %s as %s job %s", inbound.uid, job_kind, job_id)

    notification_sent = False
    try:
        record = store.get(key) or {}
        if record.get("result_sent_at"):
            logger.warning(
                "Job %s result was already sent before an interrupted archive step; "
                "finalizing without duplicate delivery",
                job_id,
            )
            _move_job(job_dir, settings.processed_dir / job_id)
            store.mark_finished(key, status="processed", job_id=job_id)
            status_store.update(
                job_id,
                kind=job_kind,
                stage="email_sent",
                message="Recovered post-delivery archive without resending the result",
                finished_at=time.time(),
            )
            return
        if _safe_int(record.get("ack_queue_position"), default=1) > 1 and not record.get(
            "start_sent_at"
        ):
            try:
                _send_limited_reply(
                    settings,
                    store,
                    mail_client,
                    inbound,
                    subject=f"Re: {inbound.subject}",
                    body=_start_body(job_kind, job_id, _queue_defaults(settings)[job_kind]),
                    headers={"X-FMBSM-Job-ID": job_id, "X-FMBSM-Reply-Type": "started"},
                )
                store.mark_start_sent(key)
            except Exception:
                logger.exception("Could not send processing-started notice for job %s", job_id)

        with nullcontext(None) as smtp:
            quality_gate_status = ""
            quality_gate_reasons: list[str] = []
            if job_kind == "fs_review":
                ticket_dir, input_count = _prepare_fs_inputs(
                    inbound, attachments_dir, extracted_dir, job_dir, settings
                )
                logger.info("Job %s has %s prepared FS input file(s)", job_id, input_count)
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
                completed_status = status_store.get(job_id) or {}
                quality_gate_status = str(
                    completed_status.get("quality_gate_status") or "review_required"
                )
                quality_gate_reasons = [
                    str(reason)
                    for reason in completed_status.get("quality_gate_reasons", [])
                ]
            elif job_kind == "effectif_payroll":
                pdf_paths = _prepare_pdf_inputs(
                    inbound, attachments_dir, extracted_dir, job_dir, settings
                )
                if not pdf_paths:
                    raise RuntimeError(
                        "No accepted PDF files found. Attach .pdf files or a .zip/.7z containing PDFs."
                    )
                logger.info("Job %s collected %s effectif PDF(s)", job_id, len(pdf_paths))
                outputs = run_effectif_job(
                    pdf_paths=pdf_paths,
                    output_dir=output_dir,
                    job_id=job_id,
                    label_root=job_dir,
                    timeout_seconds=settings.effectif_timeout_seconds,
                    project_dir=settings.project_dir,
                    status_store=status_store,
                )
                processed_label = f"{len(pdf_paths)} PDF effectif/payroll extraction"
            elif job_kind == "balance_cleaner":
                workbook_paths = _prepare_balance_inputs(
                    inbound, attachments_dir, extracted_dir, job_dir, settings
                )
                logger.info(
                    "Job %s collected %s Excel balance workbook(s)",
                    job_id,
                    len(workbook_paths),
                )
                outputs = run_balance_job(
                    workbook_paths=workbook_paths,
                    output_dir=output_dir,
                    job_id=job_id,
                    timeout_seconds=settings.balance_timeout_seconds,
                    project_dir=settings.project_dir,
                    status_store=status_store,
                )
                processed_label = f"{len(workbook_paths)} cleaned trial balance workbook(s)"
            else:
                pdf_paths = _prepare_pdf_inputs(
                    inbound, attachments_dir, extracted_dir, job_dir, settings
                )
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
            quality_subject = (
                "[REVIEW REQUIRED] "
                if quality_gate_status == "review_required"
                else "[PASS WITH WARNINGS] "
                if quality_gate_status == "pass_with_warnings"
                else ""
            )
            result_subject = f"{quality_subject}Re: {inbound.subject}"
            result_body = (
                "Hello,\n\n"
                f"Completed {processed_label}. The result file(s) are attached.\n\n"
            )
            if quality_gate_status:
                result_body += f"Automated quality gate: {quality_gate_status.upper()}.\n"
                if quality_gate_status == "review_required":
                    result_body += (
                        "Do not treat this output as final; professional review is required.\n"
                    )
                for reason in quality_gate_reasons[:5]:
                    result_body += f"- {reason}\n"
                result_body += (
                    "See production_quality_summary.txt and the mapping/review workbooks "
                    "inside the ZIP for full details.\n\n"
                )
            result_body += f"Job ID: {job_id}\n"
            result_headers = {
                "X-FMBSM-Job-ID": job_id,
                "X-FMBSM-Reply-Type": "result",
                **(
                    {"X-FMBSM-Quality-Gate": quality_gate_status}
                    if quality_gate_status
                    else {}
                ),
            }
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
        store.mark_result_sent(key)
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
        attempts = _safe_int((store.get(key) or {}).get("attempts"), default=1)
        if attempts < settings.max_processing_attempts and _is_retryable_job_error(exc):
            delay = settings.queue_retry_delay_seconds * attempts
            store.mark_retry(key, error=str(exc), delay_seconds=delay)
            status_store.update(
                job_id,
                kind=job_kind,
                stage="retry_queued",
                message=f"Transient failure; automatic attempt {attempts + 1} queued in {delay}s",
                error=f"{type(exc).__name__}: {exc}"[:4000],
                attempt=attempts,
                next_attempt_in_seconds=delay,
            )
            record = store.get(key) or {}
            if settings.send_retry_notifications and not record.get("retry_notice_sent_at"):
                try:
                    _send_limited_reply(
                        settings,
                        store,
                        mail_client,
                        inbound,
                        subject=f"Re: {inbound.subject}",
                        body=(
                            "Hello,\n\nYour job encountered a temporary service/account error. "
                            "Its files are safe and it has been queued for an automatic retry.\n\n"
                            f"Job ID: {job_id}\n"
                            f"Next attempt: approximately {_format_duration(delay)}\n"
                        ),
                        headers={
                            "X-FMBSM-Job-ID": job_id,
                            "X-FMBSM-Reply-Type": "retry",
                        },
                    )
                    store.mark_retry_notice_sent(key)
                except Exception:
                    logger.exception("Could not send retry notice for job %s", job_id)
            return
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


def _prepare_fs_inputs(
    inbound: InboundEmail,
    attachments_dir: Path,
    extracted_dir: Path,
    job_dir: Path,
    settings: Settings,
) -> tuple[Path, int]:
    manifest = _read_input_manifest(job_dir)
    if manifest.get("kind") == "fs_review":
        ticket_dir = Path(str(manifest.get("ticket_dir") or ""))
        expected = [ticket_dir / "Input" / "financial_statements_N.pdf", ticket_dir / "Input" / "bg_standardized.xlsx"]
        if ticket_dir.is_dir() and all(path.is_file() for path in expected):
            return ticket_dir, _safe_int(manifest.get("input_count"), default=len(expected))

    input_paths = save_fs_attachments(inbound, attachments_dir, extracted_dir, settings)
    ticket_dir = prepare_ticket(input_paths, job_dir)
    _write_input_manifest(
        job_dir,
        {"kind": "fs_review", "ticket_dir": str(ticket_dir), "input_count": len(input_paths)},
    )
    return ticket_dir, len(input_paths)


def _prepare_pdf_inputs(
    inbound: InboundEmail,
    attachments_dir: Path,
    extracted_dir: Path,
    job_dir: Path,
    settings: Settings,
) -> list[Path]:
    manifest = _read_input_manifest(job_dir)
    paths = [Path(str(value)) for value in manifest.get("pdf_paths", [])]
    if manifest.get("kind") == "pdfs" and paths and all(path.is_file() for path in paths):
        return paths
    paths = save_accepted_attachments(inbound, attachments_dir, extracted_dir, settings)
    _write_input_manifest(job_dir, {"kind": "pdfs", "pdf_paths": [str(path) for path in paths]})
    return paths


def _prepare_balance_inputs(
    inbound: InboundEmail,
    attachments_dir: Path,
    extracted_dir: Path,
    job_dir: Path,
    settings: Settings,
) -> list[Path]:
    manifest = _read_input_manifest(job_dir)
    paths = [Path(str(value)) for value in manifest.get("workbook_paths", [])]
    if manifest.get("kind") == "balance_workbooks" and paths and all(
        path.is_file() for path in paths
    ):
        return paths
    paths = save_balance_attachments(inbound, attachments_dir, extracted_dir, settings)
    _write_input_manifest(
        job_dir,
        {"kind": "balance_workbooks", "workbook_paths": [str(path) for path in paths]},
    )
    return paths


def _read_input_manifest(job_dir: Path) -> dict[str, object]:
    path = job_dir / "input_manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_input_manifest(job_dir: Path, payload: dict[str, object]) -> None:
    path = job_dir / "input_manifest.json"
    temporary = job_dir / ".input_manifest.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


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


def save_balance_attachments(
    inbound: InboundEmail,
    attachments_dir: Path,
    extracted_dir: Path,
    settings: Settings,
) -> list[Path]:
    accepted: list[Path] = []
    allowed = {".xlsx", ".xlsm", ".xls", ".xlsb"}
    excel_content_types = {
        "application/vnd.ms-excel": ".xls",
        "application/vnd.ms-excel.sheet.binary.macroenabled.12": ".xlsb",
        "application/vnd.ms-excel.sheet.macroenabled.12": ".xlsm",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }
    attachment_index = 0
    for part in inbound.message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        content_type = part.get_content_type()
        if not filename and content_type not in {
            *excel_content_types,
            "application/zip",
            "application/x-zip-compressed",
            "application/x-7z-compressed",
        }:
            continue
        attachment_index += 1
        if filename:
            suffix = Path(filename).suffix.lower()
        else:
            suffix = excel_content_types.get(content_type, ".7z" if content_type == "application/x-7z-compressed" else ".zip")
            filename = f"attachment_{attachment_index}{suffix}"
        if suffix not in {*allowed, ".zip", ".7z"}:
            continue
        payload = get_payload_bytes(part)
        if not payload:
            logger.warning("Skipping empty balance attachment %s on UID %s", filename, inbound.uid)
            continue
        if len(payload) > settings.max_attachment_bytes:
            raise RuntimeError(f"Attachment {filename} exceeds size limit")
        attachment_path = unique_path(
            attachments_dir,
            safe_filename(filename, f"attachment_{attachment_index}{suffix}"),
        )
        attachment_path.write_bytes(payload)
        logger.info("Saved balance attachment %s", attachment_path)
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
        logger.info(
            "Extracted %s Excel balance input(s) from %s",
            len(extracted),
            attachment_path.name,
        )
        accepted.extend(extracted)
    if not accepted:
        raise RuntimeError(
            "No Excel balance found. Attach an .xlsx, .xlsm, .xls, or .xlsb file "
            "(directly or inside a .zip/.7z archive)."
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
        for prefix in (
            settings.subject_prefix,
            settings.fs_subject_prefix,
            settings.effectif_subject_prefix,
            settings.balance_subject_prefix,
        )
    )


def _job_kind(settings: Settings, subject: str) -> str | None:
    if subject.startswith(settings.subject_prefix):
        return "signature_dates"
    if subject.startswith(settings.fs_subject_prefix):
        return "fs_review"
    if subject.startswith(settings.effectif_subject_prefix):
        return "effectif_payroll"
    if subject.startswith(settings.balance_subject_prefix):
        return "balance_cleaner"
    return None


def _is_authorized_job_sender(settings: Settings, sender: str) -> bool:
    address = sender.strip().lower()
    if not address or "@" not in address:
        return False
    if address in settings.authorized_job_senders:
        return True
    domain = address.rsplit("@", 1)[1]
    return domain in settings.authorized_job_sender_domains


def _subject_year(subject: str, default: int) -> int:
    match = re.search(r"(?:year|annee|année)\s*[:= -]\s*(20\d{2})", subject, flags=re.IGNORECASE)
    return int(match.group(1)) if match else default


def _ack_body(job_kind: str, job_id: str, queue: dict[str, int]) -> str:
    labels = {
        "fs_review": "financial-statement review",
        "effectif_payroll": "effectif/payroll evidence extraction",
        "signature_dates": "PDF signature/date extraction",
        "balance_cleaner": "trial balance cleaning",
    }
    label = labels.get(job_kind, job_kind.replace("_", " "))
    if queue["position"] == 1:
        queue_text = "Your job is first in the queue and will start shortly."
    else:
        queue_text = (
            f"Queue position: {queue['position']} of {queue['total']}.\n"
            f"Estimated wait before processing: about {_format_duration(queue['estimated_start_seconds'])}."
        )
    return (
        "Hello,\n\n"
        f"We received your {label} request and saved its email and attachments durably.\n\n"
        f"{queue_text}\n"
        f"Estimated result time: about {_format_duration(queue['estimated_completion_seconds'])} from now.\n\n"
        f"Job ID: {job_id}\n"
        "You will receive another message when processing starts if this job had to wait. "
        "Live progress and recovery events are recorded on the automation server.\n"
    )


def _start_body(job_kind: str, job_id: str, expected_seconds: int) -> str:
    labels = {
        "fs_review": "financial-statement review",
        "effectif_payroll": "effectif/payroll evidence extraction",
        "signature_dates": "PDF signature/date extraction",
        "balance_cleaner": "trial balance cleaning",
    }
    label = labels.get(job_kind, job_kind.replace("_", " "))
    return (
        "Hello,\n\n"
        f"Your queued {label} job is now processing.\n"
        f"Typical remaining time: about {_format_duration(expected_seconds)}.\n\n"
        f"Job ID: {job_id}\n"
    )


def _format_duration(seconds: int | float) -> str:
    minutes = max(1, int(round(float(seconds) / 60)))
    if minutes < 60:
        return f"{minutes} minute" + ("s" if minutes != 1 else "")
    hours, remainder = divmod(minutes, 60)
    if remainder:
        return f"{hours}h {remainder}m"
    return f"{hours} hour" + ("s" if hours != 1 else "")


def _is_retryable_job_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            OSError,
            smtplib.SMTPException,
            subprocess.SubprocessError,
        ),
    ):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "no usable copilot session",
        "rate limit",
        "throttl",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "service unavailable",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "timed out",
        "timeout",
        "websocket",
        "token_refresh_failed",
    )
    return any(marker in text for marker in markers)


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
