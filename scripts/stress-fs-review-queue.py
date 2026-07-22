from __future__ import annotations

import argparse
import imaplib
import json
import smtplib
import ssl
import time
import uuid
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

from dotenv import dotenv_values


def _input_dir(case_dir: Path) -> Path:
    candidate = case_dir / "Input"
    return candidate if candidate.is_dir() else case_dir


def _case_files(case_dir: Path) -> tuple[Path, Path, Path | None]:
    source = _input_dir(case_dir)
    current = source / "financial_statements_N.pdf"
    bg = source / "bg_standardized.xlsx"
    prior = next(
        (
            path
            for path in (
                source / "financial_statements_N-1.pdf",
                source / "financial_statements_N_1.pdf",
            )
            if path.is_file()
        ),
        None,
    )
    for required in (current, bg):
        if not required.is_file():
            raise FileNotFoundError(required)
    return current, bg, prior


def _attach(message: EmailMessage, path: Path, filename: str) -> None:
    if path.suffix.lower() == ".pdf":
        maintype, subtype = "application", "pdf"
    else:
        maintype = "application"
        subtype = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    message.add_attachment(
        path.read_bytes(), maintype=maintype, subtype=subtype, filename=filename
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Queue several live FS reviews and verify every response"
    )
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument(
        "--case",
        action="append",
        required=True,
        metavar="YEAR::DIRECTORY",
        help="Repeat for each service-desk case",
    )
    parser.add_argument("--timeout", type=int, default=6 * 60 * 60)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--send-only",
        action="store_true",
        help="Submit the messages and exit without monitoring replies",
    )
    args = parser.parse_args()

    settings = dotenv_values(args.env)
    address = str(settings["GMAIL_ADDRESS"])
    password = str(settings["GMAIL_APP_PASSWORD"]).replace(" ", "")
    smtp_host = str(settings.get("SMTP_HOST") or "smtp.gmail.com")
    smtp_port = int(settings.get("SMTP_PORT") or 465)
    imap_host = str(settings.get("IMAP_HOST") or "imap.gmail.com")
    imap_port = int(settings.get("IMAP_PORT") or 993)

    tests: dict[str, dict[str, object]] = {}
    messages: list[EmailMessage] = []
    for raw_case in args.case:
        year_text, separator, path_text = raw_case.partition("::")
        if not separator:
            raise ValueError(f"Invalid --case {raw_case!r}; expected YEAR::DIRECTORY")
        year = int(year_text)
        case_dir = Path(path_text).resolve()
        current, bg, prior = _case_files(case_dir)
        token = uuid.uuid4().hex[:12]
        subject = f"[fs-review] year={year} queue-e2e={token}"
        message = EmailMessage()
        message["From"] = address
        message["To"] = address
        message["Subject"] = subject
        message.set_content(
            f"Automated production-readiness queue test for {case_dir.name}: {token}."
        )
        _attach(message, current, "financial_statements_N.pdf")
        _attach(message, bg, "bg_standardized.xlsx")
        if prior:
            _attach(message, prior, "financial_statements_N_1.pdf")
        tests[token] = {
            "case": case_dir.name,
            "year": year,
            "subject": subject,
            "sent_at": None,
            "replies": [],
            "result_received_at": None,
        }
        messages.append(message)

    with smtplib.SMTP_SSL(
        smtp_host,
        smtp_port,
        context=ssl.create_default_context(),
        timeout=60,
    ) as smtp:
        smtp.login(address, password)
        for message, record in zip(messages, tests.values()):
            smtp.send_message(message)
            record["sent_at"] = time.time()
            print(f"sent case={record['case']} subject={record['subject']}", flush=True)

    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "queue-test-report.json"
    report_path.write_text(
        json.dumps({"tests": tests}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.send_only:
        return 0

    seen_message_ids: set[tuple[str, bytes]] = set()
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        with imaplib.IMAP4_SSL(imap_host, imap_port, timeout=45) as imap:
            imap.login(address, password)
            selected = False
            for mailbox in ('"[Gmail]/All Mail"', "INBOX"):
                status, _ = imap.select(mailbox, readonly=True)
                if status == "OK":
                    selected = True
                    break
            if not selected:
                raise RuntimeError("Could not select Gmail All Mail or Inbox")
            for token, record in tests.items():
                status, data = imap.search(None, "SUBJECT", f'"{token}"')
                if status != "OK":
                    raise RuntimeError(f"IMAP subject search failed for {token}")
                for message_id in data[0].split():
                    seen_key = (token, message_id)
                    if seen_key in seen_message_ids:
                        continue
                    seen_message_ids.add(seen_key)
                    status, fetched = imap.fetch(message_id, "(RFC822)")
                    if status != "OK":
                        continue
                    raw = next(
                        (item[1] for item in fetched if isinstance(item, tuple)), None
                    )
                    if not raw:
                        continue
                    parsed = BytesParser(policy=policy.default).parsebytes(raw)
                    reply_type = str(parsed.get("X-FMBSM-Reply-Type", ""))
                    if not reply_type:
                        continue
                    body_part = parsed.get_body(preferencelist=("plain",))
                    body = body_part.get_content() if body_part else ""
                    reply = {
                        "type": reply_type,
                        "received_at": time.time(),
                        "subject": str(parsed.get("Subject", "")),
                        "body": body[:4000],
                    }
                    record["replies"].append(reply)
                    print(
                        f"received case={record['case']} type={reply_type}", flush=True
                    )
                    if reply_type == "error":
                        report_path.write_text(
                            json.dumps({"tests": tests}, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        raise RuntimeError(f"{record['case']}: {body[:2000]}")
                    if reply_type != "result":
                        continue
                    case_output = args.output / str(record["case"])
                    case_output.mkdir(parents=True, exist_ok=True)
                    saved: list[str] = []
                    for part in parsed.iter_attachments():
                        target = case_output / (part.get_filename() or "result.bin")
                        target.write_bytes(part.get_payload(decode=True) or b"")
                        if target.stat().st_size:
                            saved.append(str(target))
                    if not saved:
                        raise RuntimeError(f"{record['case']}: empty result attachment")
                    record["result_received_at"] = time.time()
                    record["attachments"] = saved
        report_path.write_text(
            json.dumps({"tests": tests}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if all(record["result_received_at"] for record in tests.values()):
            print(f"verified results={len(tests)} report={report_path}", flush=True)
            return 0
        time.sleep(10)

    missing = [
        str(record["case"])
        for record in tests.values()
        if not record["result_received_at"]
    ]
    raise TimeoutError(f"No result arrived for {missing} within {args.timeout}s")


if __name__ == "__main__":
    raise SystemExit(main())
