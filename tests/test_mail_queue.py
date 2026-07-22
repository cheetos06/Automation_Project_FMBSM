from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "automation-mail-worker"
FS_REVIEW = SERVICE / "fs_review"
for candidate in (str(SERVICE), str(FS_REVIEW)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from copilot_service.job_status import JobStatusStore  # noqa: E402
from fmbsm_email_bot.mail import parse_inbound_email  # noqa: E402
from fmbsm_email_bot.state import MessageStore  # noqa: E402
from fmbsm_email_bot.worker import (  # noqa: E402
    _enqueue_email,
    _is_retryable_job_error,
    process_next_queued,
)
from canonical_extraction import extract_canonical_documents  # noqa: E402
from copilot_pool import run_account_pool  # noqa: E402


class FakeMailClient:
    def __init__(self) -> None:
        self.replies: list[dict] = []
        self.seen: list[tuple[str, str]] = []

    def send_reply(self, inbound, **kwargs) -> None:
        self.replies.append({"inbound": inbound, **kwargs})

    def mark_seen(self, uid: str, mailbox: str) -> None:
        self.seen.append((uid, mailbox))

    def move_to_inbox(self, inbound) -> None:
        return None


def queue_settings(root: Path):
    return SimpleNamespace(
        jobs_dir=root / "jobs",
        processed_dir=root / "processed",
        failed_dir=root / "failed",
        state_dir=root / "state",
        inbox_mailbox="INBOX",
        move_spam_to_inbox=False,
        max_outbound_emails_per_hour=100,
        max_outbound_emails_per_day=1000,
        queue_default_fs_seconds=900,
        queue_default_effectif_seconds=900,
        queue_default_signature_seconds=300,
        fs_subject_prefix="[fs-review]",
        effectif_subject_prefix="[optimda-effectif]",
        subject_prefix="[optimda-extract-dates]",
        max_processing_attempts=3,
    )


def inbound_message(subject: str = "[fs-review] year=2025"):
    message = EmailMessage()
    message["From"] = "authorized@example.com"
    message["To"] = "bot@example.com"
    message["Subject"] = subject
    message["Message-ID"] = "<queue-test@example.com>"
    message.set_content("queue test")
    return parse_inbound_email(raw=message.as_bytes(), mailbox="INBOX", uid="42")


class DurableQueueTests(unittest.TestCase):
    def test_fifo_queue_and_restart_recovery_are_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = MessageStore(root / "processed_messages.json")
            for key, job_id in (("a", "job-a"), ("b", "job-b")):
                raw = root / f"{job_id}.eml"
                raw.write_bytes(b"message")
                store.mark_queued(
                    key,
                    uid=key,
                    mailbox="INBOX",
                    subject="subject",
                    job_id=job_id,
                    job_kind="fs_review",
                    raw_path=raw,
                )
            snapshot = store.queue_snapshot("b", default_seconds={"fs_review": 600})
            self.assertEqual(snapshot["position"], 2)
            self.assertGreaterEqual(snapshot["estimated_start_seconds"], 600)

            store.mark_processing(
                "a", uid="a", subject="subject", job_id="job-a", job_kind="fs_review"
            )
            self.assertEqual(store.mark_interrupted_processing(), 1)

            reloaded = MessageStore(root / "processed_messages.json")
            key, record = reloaded.next_queued() or (None, {})
            self.assertEqual(key, "a")
            self.assertEqual(record.get("recovery_count"), 1)
            self.assertEqual(reloaded.queued_count(), 2)

    def test_fast_history_never_undercuts_configured_queue_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = MessageStore(root / "processed_messages.json")
            store._data["messages"]["history"] = {
                "status": "processed",
                "job_kind": "fs_review",
                "duration_seconds": 120,
                "finished_at": "2026-07-22T08:00:00+00:00",
            }
            raw = root / "queued.eml"
            raw.write_bytes(b"message")
            store.mark_queued(
                "queued",
                uid="queued",
                mailbox="INBOX",
                subject="subject",
                job_id="queued-job",
                job_kind="fs_review",
                raw_path=raw,
            )

            snapshot = store.queue_snapshot(
                "queued",
                default_seconds={"fs_review": 900},
            )

            self.assertGreaterEqual(snapshot["estimated_completion_seconds"], 900)

    def test_slowest_recent_success_raises_queue_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = MessageStore(root / "processed_messages.json")
            store._data["messages"].update(
                {
                    "fast": {
                        "status": "processed",
                        "job_kind": "fs_review",
                        "duration_seconds": 120,
                        "finished_at": "2026-07-22T08:00:00+00:00",
                    },
                    "slow": {
                        "status": "processed",
                        "job_kind": "fs_review",
                        "duration_seconds": 1_514,
                        "finished_at": "2026-07-22T09:00:00+00:00",
                    },
                }
            )
            raw = root / "queued.eml"
            raw.write_bytes(b"message")
            store.mark_queued(
                "queued",
                uid="queued",
                mailbox="INBOX",
                subject="subject",
                job_id="queued-job",
                job_kind="fs_review",
                raw_path=raw,
            )

            snapshot = store.queue_snapshot(
                "queued",
                default_seconds={"fs_review": 900},
            )

            self.assertGreaterEqual(snapshot["estimated_completion_seconds"], 1_514)

    def test_enqueue_saves_rfc822_before_ack_and_reports_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = queue_settings(root)
            store = MessageStore(root / "state" / "processed_messages.json")
            mail = FakeMailClient()
            inbound = inbound_message()
            _enqueue_email(
                settings,
                mail,
                store,
                inbound,
                inbound.message_id,
                job_kind="fs_review",
            )
            record = store.get(inbound.message_id) or {}
            self.assertEqual(record.get("status"), "queued")
            self.assertTrue(Path(str(record["raw_path"])).is_file())
            self.assertIn("first in the queue", mail.replies[0]["body"])
            self.assertEqual(mail.replies[0]["headers"]["X-FMBSM-Reply-Type"], "ack")
            self.assertEqual(mail.seen, [("42", "INBOX")])

            with patch("fmbsm_email_bot.worker.process_email") as process:
                self.assertTrue(process_next_queued(settings, mail, store))
                recovered = process.call_args.args[3]
                self.assertEqual(recovered.message_id, inbound.message_id)
                self.assertEqual(recovered.raw, inbound.raw)

    def test_transient_error_classification_is_bounded(self) -> None:
        self.assertTrue(_is_retryable_job_error(TimeoutError("slow")))
        self.assertTrue(_is_retryable_job_error(RuntimeError("Copilot HTTP 503")))
        self.assertFalse(_is_retryable_job_error(RuntimeError("Missing BG workbook")))

    def test_concurrent_queue_writes_do_not_lose_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = MessageStore(root / "processed_messages.json")

            def enqueue(index: int) -> None:
                raw = root / f"{index}.eml"
                raw.write_bytes(b"message")
                store.mark_queued(
                    str(index),
                    uid=str(index),
                    mailbox="INBOX",
                    subject="subject",
                    job_id=f"job-{index:03d}",
                    job_kind="fs_review",
                    raw_path=raw,
                )

            threads = [threading.Thread(target=enqueue, args=(index,)) for index in range(100)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(store.queued_count(), 100)
            reloaded = MessageStore(root / "processed_messages.json")
            self.assertEqual(reloaded.queued_count(), 100)


class AtomicStatusTests(unittest.TestCase):
    def test_concurrent_updates_leave_valid_status_and_complete_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStatusStore(Path(temp))
            threads = [
                threading.Thread(
                    target=store.update,
                    kwargs={"job_id": "job", "stage": f"s{index}", "message": "ok"},
                )
                for index in range(20)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            payload = json.loads((Path(temp) / "job.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["job_id"], "job")
            events = (Path(temp) / "job.events.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 20)
            self.assertTrue(all(json.loads(line)["job_id"] == "job" for line in events))


class CopilotResumeTests(unittest.TestCase):
    def test_canonical_page_cache_avoids_duplicate_turn(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "one-page.pdf"
            document = fitz.open()
            document.new_page()
            document.save(pdf)
            document.close()
            runs = root / "runs"
            cache = runs / "N" / "page_1" / "canonical_N_page_1_parsed.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(
                json.dumps(
                    {
                        "scope": "annual",
                        "page_role": "blank",
                        "reviewable": False,
                        "blocks": [],
                        "fs_lines": [],
                        "tables": [],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "canonical.json"
            stats = extract_canonical_documents(
                {"N": pdf},
                output_paths={"N": output},
                runs_dir=runs,
                settings={"canonical_page_prompt": "unused.md"},
            )
            self.assertEqual(stats["vision_calls"], 0)
            self.assertEqual(stats["cached_vision_pages"], 1)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))["pages"]), 1)


class CopilotPoolScalingTests(unittest.TestCase):
    class Registry:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._turns: dict[str, int] = {}

        def reserve_turn(self, account: str, **kwargs):
            with self._lock:
                count = self._turns.get(account, 0) + 1
                self._turns[account] = count
            return True, 0.0, "reserved", count

        def start_cooldown(self, *args, **kwargs) -> None:
            return None

    def _run(self, account_count: int) -> float:
        accounts = [SimpleNamespace(name=f"a{index}") for index in range(account_count)]
        registry = self.Registry()

        def operation(job, settings, account):
            time.sleep(0.02)
            return (job, account)

        with patch("copilot_pool.load_copilot_accounts", return_value=accounts), patch(
            "copilot_pool.CopilotRegistry.from_env", return_value=registry
        ), patch("builtins.print"):
            run = run_account_pool(list(range(40)), {}, operation)
        self.assertEqual([value[0] for value in run.results], list(range(40)))
        return run.elapsed_seconds

    def test_scheduler_throughput_scales_with_accounts_and_preserves_results(self) -> None:
        one = self._run(1)
        four = self._run(4)
        eight = self._run(8)
        self.assertLess(four, one * 0.45)
        self.assertLess(eight, one * 0.3)

    def test_one_broken_account_is_quarantined_without_losing_work(self) -> None:
        accounts = [SimpleNamespace(name="broken"), SimpleNamespace(name="healthy")]
        registry = self.Registry()

        def operation(job, settings, account):
            if account == "broken":
                raise RuntimeError("account session is invalid")
            time.sleep(0.005)
            return job

        with patch("copilot_pool.load_copilot_accounts", return_value=accounts), patch(
            "copilot_pool.CopilotRegistry.from_env", return_value=registry
        ), patch("builtins.print"):
            run = run_account_pool(list(range(12)), {}, operation)
        self.assertEqual(run.results, list(range(12)))
        self.assertEqual(run.account_stats["broken"]["account_errors"], 1)
        self.assertEqual(run.account_stats["healthy"]["jobs"], 12)


if __name__ == "__main__":
    unittest.main()
