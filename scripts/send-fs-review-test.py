from __future__ import annotations

import argparse
import imaplib
import smtplib
import ssl
import time
import uuid
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

from dotenv import dotenv_values


def main() -> int:
    parser = argparse.ArgumentParser(description="Send and verify one live FS review email")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--n", type=Path, required=True)
    parser.add_argument("--prior", type=Path)
    parser.add_argument("--bg", type=Path, required=True)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--timeout", type=int, default=3 * 60 * 60)
    parser.add_argument("--output", type=Path, default=Path("test-email-result"))
    args = parser.parse_args()
    settings = dotenv_values(args.env)
    address = str(settings["GMAIL_ADDRESS"])
    password = str(settings["GMAIL_APP_PASSWORD"]).replace(" ", "")
    token = uuid.uuid4().hex[:10]
    subject = f"[fs-review] year={args.year} e2e={token}"
    message = EmailMessage()
    message["From"] = address
    message["To"] = address
    message["Subject"] = subject
    message.set_content(f"Automated end-to-end FS review test {token}.")
    attachments = [
        (args.n, "financial_statements_N.pdf"),
        (args.bg, "bg_standardized.xlsx"),
    ]
    if args.prior:
        attachments.append((args.prior, "financial_statements_N_1.pdf"))
    for path, filename in attachments:
        path = path.resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        maintype, subtype = ("application", "pdf") if path.suffix.lower() == ".pdf" else (
            "application",
            "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=filename)
    with smtplib.SMTP_SSL(
        str(settings.get("SMTP_HOST") or "smtp.gmail.com"),
        int(settings.get("SMTP_PORT") or 465),
        context=ssl.create_default_context(),
        timeout=60,
    ) as smtp:
        smtp.login(address, password)
        smtp.send_message(message)
    print(f"sent subject={subject}", flush=True)

    deadline = time.monotonic() + args.timeout
    seen_ids: set[bytes] = set()
    while time.monotonic() < deadline:
        with imaplib.IMAP4_SSL(
            str(settings.get("IMAP_HOST") or "imap.gmail.com"),
            int(settings.get("IMAP_PORT") or 993),
            timeout=45,
        ) as imap:
            imap.login(address, password)
            selected = False
            for mailbox in ('"[Gmail]/All Mail"', "INBOX"):
                status, _ = imap.select(mailbox, readonly=True)
                if status == "OK":
                    selected = True
                    break
            if not selected:
                raise RuntimeError("Could not select Gmail All Mail or Inbox")
            status, data = imap.search(None, "SUBJECT", f'"{token}"')
            if status != "OK":
                raise RuntimeError("IMAP subject search failed")
            for message_id in reversed(data[0].split()):
                if message_id in seen_ids:
                    continue
                seen_ids.add(message_id)
                status, fetched = imap.fetch(message_id, "(RFC822)")
                if status != "OK":
                    continue
                raw = next((item[1] for item in fetched if isinstance(item, tuple)), None)
                if not raw:
                    continue
                parsed = BytesParser(policy=policy.default).parsebytes(raw)
                reply_type = parsed.get("X-FMBSM-Reply-Type", "")
                print(f"received type={reply_type or 'request'} subject={parsed.get('Subject')}", flush=True)
                if reply_type == "error":
                    raise RuntimeError(parsed.get_body(preferencelist=("plain",)).get_content()[:2000])
                if reply_type != "result":
                    continue
                args.output.mkdir(parents=True, exist_ok=True)
                saved = []
                for part in parsed.iter_attachments():
                    filename = part.get_filename() or "result.bin"
                    target = args.output / filename
                    target.write_bytes(part.get_payload(decode=True) or b"")
                    saved.append(target)
                if not saved or any(path.stat().st_size == 0 for path in saved):
                    raise RuntimeError("Result email had no non-empty attachment")
                print("verified attachments=" + ",".join(str(path) for path in saved), flush=True)
                return 0
        time.sleep(15)
    raise TimeoutError(f"No result email arrived within {args.timeout} seconds")


if __name__ == "__main__":
    raise SystemExit(main())
