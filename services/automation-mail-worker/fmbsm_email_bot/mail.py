from __future__ import annotations

import imaplib
import logging
import mimetypes
import re
import smtplib
from contextlib import contextmanager
from dataclasses import dataclass
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path
from typing import Iterable, Mapping

from .config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundEmail:
    mailbox: str
    uid: str
    raw: bytes
    message: EmailMessage
    subject: str
    sender: str
    reply_to: str
    message_id: str
    references: str
    bot_generated: bool


def parse_inbound_email(*, raw: bytes, mailbox: str, uid: str) -> InboundEmail:
    """Rebuild a queued message from its durable RFC-822 copy."""

    message = BytesParser(policy=policy.default).parsebytes(raw)
    return InboundEmail(
        mailbox=mailbox,
        uid=uid,
        raw=raw,
        message=message,
        subject=_header_to_str(message.get("subject")),
        sender=parseaddr(_header_to_str(message.get("from")))[1],
        reply_to=parseaddr(
            _header_to_str(message.get("reply-to") or message.get("from"))
        )[1],
        message_id=_header_to_str(message.get("message-id")).strip(),
        references=_header_to_str(message.get("references")).strip(),
        bot_generated=_is_bot_generated(message),
    )


class GmailClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._resolved_mailboxes: tuple[str, ...] | None = None

    def fetch_unread(self) -> list[InboundEmail]:
        with self._imap() as imap:
            self._login(imap)
            messages: list[InboundEmail] = []

            for mailbox in self._get_monitored_mailboxes(imap):
                if not self._select_mailbox(imap, mailbox):
                    continue

                search_terms = _unread_subject_search_terms(
                    self.settings.subject_prefix,
                    self.settings.fs_subject_prefix,
                    self.settings.effectif_subject_prefix,
                    self.settings.balance_subject_prefix,
                )
                status, data = imap.uid("SEARCH", None, *search_terms)
                self._require_ok(status, f"search unread trigger emails in {mailbox}")

                uids = data[0].split() if data and data[0] else []
                for uid_bytes in uids:
                    uid = uid_bytes.decode("ascii", errors="replace")
                    status, fetch_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                    self._require_ok(status, f"fetch email UID {uid} from {mailbox}")
                    raw = _extract_raw_message(fetch_data)
                    messages.append(parse_inbound_email(raw=raw, mailbox=mailbox, uid=uid))
            return messages

    def mark_seen(self, uid: str, mailbox: str | None = None) -> None:
        with self._imap() as imap:
            self._login_and_select(imap, mailbox or self.settings.inbox_mailbox)
            status, _ = imap.uid("STORE", uid, "+FLAGS.SILENT", r"(\Seen)")
            self._require_ok(status, f"mark email UID {uid} as seen in {mailbox}")

    def move_to_inbox(self, inbound: InboundEmail) -> None:
        if inbound.mailbox == self.settings.inbox_mailbox:
            return

        with self._imap() as imap:
            self._login_and_select(imap, inbound.mailbox)

            status, _ = imap.uid("MOVE", inbound.uid, _quote_mailbox(self.settings.inbox_mailbox))
            if status == "OK":
                logger.info("Moved UID %s from %s to %s", inbound.uid, inbound.mailbox, self.settings.inbox_mailbox)
                return

            logger.warning(
                "UID MOVE from %s to %s failed with status %s; falling back to COPY",
                inbound.mailbox,
                self.settings.inbox_mailbox,
                status,
            )
            status, _ = imap.uid("COPY", inbound.uid, _quote_mailbox(self.settings.inbox_mailbox))
            self._require_ok(status, f"copy UID {inbound.uid} to {self.settings.inbox_mailbox}")
            logger.info("Copied UID %s from %s to %s", inbound.uid, inbound.mailbox, self.settings.inbox_mailbox)

    def send_reply(
        self,
        inbound: InboundEmail,
        *,
        subject: str,
        body: str,
        attachments: Iterable[Path] = (),
        headers: Mapping[str, str] | None = None,
        smtp: smtplib.SMTP_SSL | None = None,
    ) -> None:
        if not inbound.reply_to:
            raise ValueError("Cannot reply because the email has no From or Reply-To address")

        outbound = EmailMessage()
        outbound["From"] = self.settings.gmail_address
        outbound["To"] = inbound.reply_to
        outbound["Subject"] = subject
        outbound["X-FMBSM-Bot"] = "true"
        outbound["Auto-Submitted"] = "auto-replied"
        outbound["Precedence"] = "bulk"
        for key, value in (headers or {}).items():
            outbound[key] = value
        if inbound.message_id:
            outbound["In-Reply-To"] = inbound.message_id
            outbound["References"] = inbound.references or inbound.message_id
        outbound.set_content(body)

        for attachment in attachments:
            content_type, _ = mimetypes.guess_type(attachment.name)
            if not content_type:
                content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            maintype, subtype = content_type.split("/", 1)
            outbound.add_attachment(
                attachment.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=attachment.name,
            )

        if smtp:
            smtp.send_message(outbound)
            return

        with self.smtp_session() as session:
            session.send_message(outbound)

    @contextmanager
    def smtp_session(self):
        with smtplib.SMTP_SSL(
            self.settings.smtp_host,
            self.settings.smtp_port,
            timeout=self.settings.smtp_timeout_seconds,
        ) as smtp:
            smtp.login(self.settings.gmail_address, self.settings.gmail_app_password)
            yield smtp

    def _imap(self) -> imaplib.IMAP4_SSL:
        return imaplib.IMAP4_SSL(
            self.settings.imap_host,
            self.settings.imap_port,
            timeout=self.settings.imap_timeout_seconds,
        )

    def _login(self, imap: imaplib.IMAP4_SSL) -> None:
        status, _ = imap.login(self.settings.gmail_address, self.settings.gmail_app_password)
        self._require_ok(status, "login to Gmail IMAP")

    def _login_and_select(self, imap: imaplib.IMAP4_SSL, mailbox: str) -> None:
        self._login(imap)
        if not self._select_mailbox(imap, mailbox):
            raise RuntimeError(f"Failed to select mailbox {mailbox}")

    def _select_mailbox(self, imap: imaplib.IMAP4_SSL, mailbox: str) -> bool:
        status, _ = imap.select(_quote_mailbox(mailbox))
        if status == "OK":
            return True
        logger.warning("Mailbox %s is unavailable; skipping", mailbox)
        return False

    def _get_monitored_mailboxes(self, imap: imaplib.IMAP4_SSL) -> tuple[str, ...]:
        if self._resolved_mailboxes is not None:
            return self._resolved_mailboxes

        available = _list_mailboxes(imap)
        requested = list(dict.fromkeys(self.settings.monitored_mailboxes))

        resolved: list[str] = []
        for mailbox in requested:
            if mailbox in available or mailbox.upper() == "INBOX":
                resolved.append(mailbox)
                continue
            if mailbox.lower() in {"spam", "junk", "[gmail]/spam"}:
                spam_mailbox = _find_spam_mailbox(available)
                if spam_mailbox and spam_mailbox not in resolved:
                    resolved.append(spam_mailbox)

        spam_mailbox = _find_spam_mailbox(available)
        if spam_mailbox and any(_looks_like_spam_name(name) for name in requested) and spam_mailbox not in resolved:
            resolved.append(spam_mailbox)

        self._resolved_mailboxes = tuple(resolved or (self.settings.inbox_mailbox,))
        logger.info("Monitoring mailboxes: %s", ", ".join(self._resolved_mailboxes))
        return self._resolved_mailboxes

    @staticmethod
    def _require_ok(status: str, action: str) -> None:
        if status != "OK":
            raise RuntimeError(f"Failed to {action}: IMAP status {status}")


def _unread_subject_search_terms(*prefixes: str) -> tuple[str, ...]:
    unique = [value for value in dict.fromkeys(prefixes) if value]
    if not unique:
        raise ValueError("At least one subject prefix is required")
    clauses = [("HEADER", "Subject", _quote_search_value(prefix)) for prefix in unique]
    expression = clauses[0]
    for clause in clauses[1:]:
        expression = ("OR", *expression, *clause)
    return ("UNSEEN", *expression)


def _extract_raw_message(fetch_data) -> bytes:
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError("IMAP fetch did not return a raw message")


def _header_to_str(value) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def get_payload_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    content = part.get_payload()
    if isinstance(content, str):
        return content.encode("utf-8")
    return b""


def _is_bot_generated(message: EmailMessage) -> bool:
    marker = _header_to_str(message.get("x-fmbsm-bot")).strip().lower()
    return marker in {"1", "true", "yes", "fmbsm-email-bot"}


def _quote_search_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', r"\"")
    return f'"{escaped}"'


def _quote_mailbox(mailbox: str) -> str:
    escaped = mailbox.replace("\\", "\\\\").replace('"', r"\"")
    return f'"{escaped}"'


def _list_mailboxes(imap: imaplib.IMAP4_SSL) -> dict[str, bytes]:
    status, data = imap.list()
    if status != "OK" or not data:
        logger.warning("Could not list Gmail mailboxes; using configured names")
        return {}

    mailboxes: dict[str, bytes] = {}
    for raw in data:
        if not isinstance(raw, bytes):
            continue
        name = _parse_mailbox_name(raw)
        if name:
            mailboxes[name] = raw
    return mailboxes


def _parse_mailbox_name(raw: bytes) -> str | None:
    text = raw.decode("utf-8", errors="replace")
    quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    if quoted:
        return quoted[-1].replace(r"\"", '"').replace(r"\\", "\\")

    parts = text.rsplit(" ", 1)
    return parts[-1] if parts else None


def _find_spam_mailbox(mailboxes: dict[str, bytes]) -> str | None:
    for name, raw in mailboxes.items():
        text = raw.decode("utf-8", errors="replace").lower()
        if "\\junk" in text or "\\spam" in text:
            return name
    for name in mailboxes:
        if _looks_like_spam_name(name):
            return name
    return None


def _looks_like_spam_name(name: str) -> bool:
    lowered = name.lower()
    return lowered == "spam" or lowered.endswith("/spam") or lowered == "junk" or lowered.endswith("/junk")
