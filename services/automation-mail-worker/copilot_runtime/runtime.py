from __future__ import annotations

import argparse
import base64
import copy
import ctypes
import ctypes.wintypes
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import websocket
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILE_DIR = PACKAGE_DIR / "browser_profile"
DEFAULT_SESSION_DIR = PACKAGE_DIR / "api_session"
DEFAULT_OUTPUT_DIR = PACKAGE_DIR / "outputs"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
PDF_SUFFIXES = {".pdf"}
SIGNALR_RECORD_SEPARATOR = "\x1e"

DEFAULT_PROMPT = """You are extracting data from a French financial/audit report page image. Return ONLY valid JSON matching the schema. Do not invent values. Use null when absent or unreadable. Keep French dates as seen if unsure.

Schema:
{
  "document_type": null,
  "entity_name": null,
  "period_end_date": null,
  "total_bilan": null,
  "chiffre_affaires": null,
  "signers": [
    {
      "name": null,
      "role": null,
      "date": null
    }
  ],
  "confidence": null,
  "notes": []
}
"""


class CopilotRuntimeError(RuntimeError):
    pass


class CopilotSessionError(CopilotRuntimeError):
    pass


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes, Path))


def _resolve_path(value: str | Path | None, env_name: str, default: Path) -> Path:
    raw = value or os.getenv(env_name) or str(default)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def _run_id(prefix: str = "copilot_runtime") -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_file(directory: Path, pattern: str) -> Path | None:
    matches = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _pdf_page_count(input_path: Path) -> int:
    try:
        import fitz
    except ImportError as exc:
        raise CopilotRuntimeError("PyMuPDF is required for PDF input. Install requirements.txt.") from exc
    document = fitz.open(str(input_path))
    try:
        return len(document)
    finally:
        document.close()


def _prepare_input_image(input_path: Path, work_dir: Path, prefix: str, page: int, dpi: int) -> tuple[Path, str]:
    suffix = input_path.suffix.lower()
    work_dir.mkdir(parents=True, exist_ok=True)
    if suffix in PDF_SUFFIXES:
        try:
            import fitz
        except ImportError as exc:
            raise CopilotRuntimeError("PyMuPDF is required for PDF input. Install requirements.txt.") from exc
        document = fitz.open(str(input_path))
        try:
            if page < 1 or page > len(document):
                raise ValueError(f"PDF page {page} is outside the document range 1..{len(document)}")
            matrix = fitz.Matrix(dpi / 72, dpi / 72)
            pixmap = document[page - 1].get_pixmap(matrix=matrix, alpha=False)
            image_path = work_dir / f"{prefix}_page{page}.png"
            pixmap.save(str(image_path))
            return image_path, "pdf_first_page_render" if page == 1 else "pdf_page_render"
        finally:
            document.close()
    if suffix in IMAGE_SUFFIXES:
        target = work_dir / f"{prefix}{suffix}"
        if input_path.resolve() != target.resolve():
            shutil.copy2(input_path, target)
        return target, "image"
    raise ValueError(f"Unsupported input type: {input_path.suffix}. Use a PDF or image.")


def _json_value_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    start: int | None = None
    stack: list[str] = []
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            if not stack:
                start = index
            stack.append(char)
        elif char in "]}":
            if not stack:
                continue
            expected = "]" if stack[-1] == "[" else "}"
            if char != expected:
                start = None
                stack.clear()
                continue
            stack.pop()
            if not stack and start is not None:
                candidates.append(text[start : index + 1])
                start = None
    return candidates


def _parse_first_json_value(text: str) -> dict[str, Any] | list[Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    candidates = [stripped, *_json_value_candidates(stripped)]
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    raise CopilotRuntimeError("Copilot returned text, but no valid JSON object or array could be parsed.")


def _parse_first_json_object(text: str) -> dict[str, Any]:
    parsed = _parse_first_json_value(text)
    if isinstance(parsed, dict):
        return parsed
    raise CopilotRuntimeError("Copilot returned JSON, but the response was not an object.")


def _select_response_text(texts: list[str]) -> str:
    """Prefer a complete structured answer over longer progress messages.

    Copilot can emit both cumulative answer fragments and localized status text
    during one turn.  A status such as ``Je travaille sur la generation...``
    can be longer than the requested JSON, so choosing by length turns a valid
    response into a parsing failure.  Runtime prompts require JSON; the newest
    parseable object is therefore the strongest final-answer signal.
    """

    for text in reversed(texts):
        try:
            _parse_first_json_value(text)
        except CopilotRuntimeError:
            continue
        return text
    return texts[-1]


def _signalr_payload(*messages: dict[str, Any]) -> str:
    return "".join(
        json.dumps(message, ensure_ascii=False, separators=(",", ":")) + SIGNALR_RECORD_SEPARATOR
        for message in messages
    )


def _decode_signalr_frames(payload: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index, part in enumerate(str(payload).split(SIGNALR_RECORD_SEPARATOR)):
        if not part:
            continue
        entry: dict[str, Any] = {"index": index, "raw_text": part, "json": None, "parse_error": None}
        try:
            entry["json"] = json.loads(part)
        except Exception as exc:
            entry["parse_error"] = str(exc)
        messages.append(entry)
    return messages


def _parse_payload_messages(payload_text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for frame in _decode_signalr_frames(payload_text):
        parsed = frame.get("json")
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _find_chathub_url(frames: list[dict[str, Any]]) -> str:
    for frame in frames:
        url = frame.get("url") or ""
        if frame.get("is_chathub") and "substrate.office.com/m365Copilot/Chathub" in url:
            return url
    raise CopilotSessionError("No unredacted Chathub websocket URL was found in the API session capture.")


def _query_value(url: str, name: str) -> str | None:
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key == name:
            return value
    return None


def _mutate_url_ids(url: str, *, mutate_conversation_id: bool = True) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    replacements = {
        "chatsessionid": uuid.uuid4().hex,
        "XRoutingParameterSessionKey": uuid.uuid4().hex,
        "clientrequestid": uuid.uuid4().hex,
    }
    if mutate_conversation_id:
        replacements["ConversationId"] = str(uuid.uuid4())
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, replacements.get(key, value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment)), replacements


def _first_argument(prompt: dict[str, Any]) -> dict[str, Any]:
    args = prompt.get("arguments")
    if not isinstance(args, list) or not args or not isinstance(args[0], dict):
        raise CopilotSessionError("Captured prompt frame does not have arguments[0].")
    return args[0]


def _find_prompt_template(frames: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    for frame in frames:
        if not (frame.get("is_chathub") and frame.get("direction") == "sent"):
            continue
        messages = _parse_payload_messages(frame.get("payload_text") or "")
        prompt = None
        metrics = None
        for candidate in messages:
            if candidate.get("type") == 4 and candidate.get("target") == "chat":
                prompt = candidate
            if candidate.get("type") == 1 and candidate.get("target") == "Metrics":
                metrics = candidate
        if prompt:
            return prompt, metrics
    raise CopilotSessionError("No captured SignalR target=chat prompt frame was found in the API session capture.")


def _mutate_metrics(template: dict[str, Any] | None) -> dict[str, Any]:
    if not template:
        return {"type": 1, "target": "Metrics", "arguments": [{"Timestamps": []}]}
    metrics = copy.deepcopy(template)
    try:
        timestamps = metrics["arguments"][0]["Timestamps"]
        if isinstance(timestamps, list):
            now_ms = int(time.time() * 1000)
            for item in timestamps:
                if isinstance(item, dict) and "Timestamp" in item:
                    item["Timestamp"] = now_ms
    except Exception:
        pass
    return metrics


def _mutate_prompt(template: dict[str, Any], *, prompt_text: str, invocation_id: str, keep_images: bool) -> dict[str, Any]:
    prompt = copy.deepcopy(template)
    turn_id = uuid.uuid4().hex
    prompt["invocationId"] = invocation_id
    arg = _first_argument(prompt)
    arg["clientCorrelationId"] = turn_id
    arg["traceId"] = turn_id
    message = arg.get("message")
    if not isinstance(message, dict):
        raise CopilotSessionError("Captured prompt frame does not have a message object.")
    message["text"] = prompt_text
    message["requestId"] = turn_id
    if not keep_images:
        message["messageAnnotations"] = []
        message["adaptiveCards"] = []
    return prompt


def _set_prompt_image_annotations(prompt: dict[str, Any], upload_results: list[dict[str, Any]]) -> None:
    arg = _first_argument(prompt)
    message = arg.get("message")
    if not isinstance(message, dict):
        raise CopilotSessionError("Captured prompt frame does not have a message object.")
    if not upload_results:
        message["messageAnnotations"] = []
        return
    annotations = message.get("messageAnnotations")
    if not isinstance(annotations, list) or not annotations or not isinstance(annotations[0], dict):
        raise CopilotSessionError("Captured prompt frame does not contain an image annotation template.")
    template = copy.deepcopy(annotations[0])
    patched = []
    for upload_json in upload_results:
        if not upload_json.get("docId"):
            raise CopilotRuntimeError(f"UploadFile did not return docId: {upload_json}")
        item = copy.deepcopy(template)
        item["id"] = upload_json["docId"]
        metadata = item.setdefault("messageAnnotationMetadata", {})
        if isinstance(metadata, dict):
            metadata["@type"] = metadata.get("@type") or "File"
            metadata["annotationType"] = metadata.get("annotationType") or "File"
            metadata["fileName"] = upload_json.get("fileName") or metadata.get("fileName")
            metadata["fileType"] = str(upload_json.get("fileType") or ".png").lstrip(".")
        item["messageAnnotationType"] = item.get("messageAnnotationType") or "ImageFile"
        patched.append(item)
    message["messageAnnotations"] = patched


def _find_upload_template(templates: list[dict[str, Any]]) -> dict[str, Any]:
    scored = []
    for template in templates:
        url = str(template.get("url") or "").lower()
        content_type = str(template.get("content_type") or template.get("headers", {}).get("content-type") or "").lower()
        score = 0
        if "substrate.office.com/m365copilot/uploadfile" in url:
            score += 20
        elif "upload" in url or "file" in url:
            score += 8
        if "multipart/form-data" in content_type:
            score += 6
        if template.get("method", "").upper() == "POST":
            score += 2
        if score:
            scored.append((score, template))
    if not scored:
        raise CopilotSessionError("No UploadFile request template was found in the API session capture.")
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _clean_headers(headers: dict[str, str]) -> dict[str, str]:
    skip = {"host", "content-length", "connection", "accept-encoding", "priority", "http2-settings"}
    out = {}
    for key, value in headers.items():
        if key.startswith(":"):
            continue
        if key.lower() in skip:
            continue
        out[key] = value
    return out


def _body_bytes(template: dict[str, Any]) -> bytes | None:
    body = template.get("body")
    if body is None:
        return None
    if template.get("body_encoding") == "base64":
        return base64.b64decode(body)
    return str(body).encode("utf-8")


def _replace_multipart_field(body: bytes, field_name: str, value: str) -> bytes:
    text = body.decode("utf-8", errors="replace")
    pattern = rf'(name="{re.escape(field_name)}"\r?\n\r?\n)(.*?)(\r?\n--)'
    replaced, count = re.subn(pattern, rf"\g<1>{value}\g<3>", text, count=1, flags=re.DOTALL)
    if count != 1:
        raise CopilotSessionError(f"Could not replace multipart field {field_name!r} in captured UploadFile body.")
    return replaced.encode("utf-8")


def _image_data_url(image_path: Path) -> str:
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
    raw = image_path.read_bytes()
    return f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _crypt_unprotect_data(encrypted: bytes) -> bytes:
    data_in = _DATA_BLOB(len(encrypted), ctypes.cast(ctypes.create_string_buffer(encrypted), ctypes.POINTER(ctypes.c_char)))
    data_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(data_in), None, None, None, None, 0, ctypes.byref(data_out)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)


def _chromium_master_key(profile_dir: Path) -> bytes:
    local_state_candidates = [
        profile_dir / "Local State",
        profile_dir.parent / "Local State",
    ]
    local_state_path = next((path for path in local_state_candidates if path.exists()), None)
    if local_state_path is None:
        raise CopilotSessionError(f"Could not find Chromium Local State near {profile_dir}")
    data = json.loads(local_state_path.read_text(encoding="utf-8"))
    encrypted_key = base64.b64decode(data["os_crypt"]["encrypted_key"])
    if encrypted_key.startswith(b"DPAPI"):
        encrypted_key = encrypted_key[5:]
    return _crypt_unprotect_data(encrypted_key)


def _decrypt_chromium_cookie(encrypted_value: bytes, master_key: bytes) -> str:
    if encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11"):
        nonce = encrypted_value[3:15]
        ciphertext = encrypted_value[15:]
        return AESGCM(master_key).decrypt(nonce, ciphertext, None).decode("utf-8", errors="replace")
    return _crypt_unprotect_data(encrypted_value).decode("utf-8", errors="replace")


def _extract_cookies_for_domains(profile_dir: Path) -> list[dict[str, Any]]:
    domain_terms = [
        "m365.cloud.microsoft",
        "copilot.cloud.microsoft",
        ".cloud.microsoft",
        ".office.com",
        "login.microsoftonline.com",
    ]
    cookies_candidates = [
        profile_dir / "Network" / "Cookies",
        profile_dir / "Cookies",
        profile_dir / "Default" / "Network" / "Cookies",
        profile_dir / "Default" / "Cookies",
    ]
    cookies_db = next((path for path in cookies_candidates if path.exists()), None)
    if cookies_db is None:
        raise CopilotSessionError(f"Could not find Chromium cookies DB under {profile_dir}")
    master_key = _chromium_master_key(profile_dir)
    temp_path: Path | None = None
    cookies: list[dict[str, Any]] = []
    try:
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite") as temp_file:
                temp_path = Path(temp_file.name)
            shutil.copy2(cookies_db, temp_path)
            conn = sqlite3.connect(temp_path)
        except PermissionError:
            if temp_path:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            temp_path = None
            conn = sqlite3.connect(f"{cookies_db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select host_key, name, path, encrypted_value, expires_utc, is_secure, is_httponly, samesite from cookies"
        ).fetchall()
        conn.close()
        for row in rows:
            host = row["host_key"]
            if not any(term in host for term in domain_terms):
                continue
            try:
                value = _decrypt_chromium_cookie(row["encrypted_value"], master_key)
            except Exception:
                continue
            cookies.append(
                {
                    "domain": host,
                    "name": row["name"],
                    "value": value,
                    "path": row["path"],
                    "secure": bool(row["is_secure"]),
                    "httpOnly": bool(row["is_httponly"]),
                    "sameSite": row["samesite"],
                    "expires_utc": row["expires_utc"],
                }
            )
    finally:
        if temp_path:
            try:
                temp_path.unlink()
            except OSError:
                pass
    return cookies


def _load_cookies(session_dir: Path, profile_dir: Path) -> list[dict[str, Any]]:
    cookie_snapshot = _latest_file(session_dir, "private_playwright_cookies_*.json")
    if cookie_snapshot:
        cookies = _load_json(cookie_snapshot)
        if isinstance(cookies, list) and cookies:
            return cookies
    return _extract_cookies_for_domains(profile_dir)


def _add_cookies_to_client(client: httpx.Client, cookies: list[dict[str, Any]]) -> None:
    for cookie in cookies:
        value = cookie.get("value")
        if not value:
            continue
        client.cookies.set(
            cookie["name"],
            value,
            domain=cookie.get("domain") or cookie.get("host_key"),
            path=cookie.get("path", "/"),
        )


def _upload_image(
    *,
    image_path: Path,
    conversation_id: str,
    chathub_url: str,
    templates: list[dict[str, Any]],
    session_dir: Path,
    profile_dir: Path,
) -> dict[str, Any]:
    template = _find_upload_template(templates)
    headers = _clean_headers(template.get("headers", {}))
    token = _query_value(chathub_url, "access_token")
    if token:
        headers["authorization"] = f"Bearer {token}"
    original_body = _body_bytes(template)
    if original_body is None:
        raise CopilotSessionError("UploadFile template did not contain a body.")
    body = _replace_multipart_field(original_body, "conversationId", conversation_id)
    body = _replace_multipart_field(body, "FileBase64", _image_data_url(image_path))
    cookies = _load_cookies(session_dir, profile_dir)
    with httpx.Client(http2=True, follow_redirects=False) as client:
        _add_cookies_to_client(client, cookies)
        response = client.request(
            template.get("method", "POST"),
            template["url"],
            headers=headers,
            content=body,
            timeout=180,
        )
    if not response.is_success:
        raise CopilotRuntimeError(f"UploadFile failed with HTTP {response.status_code}: {response.text[:500]}")
    try:
        data = response.json()
    except Exception as exc:
        raise CopilotRuntimeError(f"UploadFile returned non-JSON response: {response.text[:500]}") from exc
    if not isinstance(data, dict) or not data.get("docId"):
        raise CopilotRuntimeError(f"UploadFile response did not include docId: {data}")
    return data


def _collect_response_texts(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, dict):
        result = value.get("result")
        if isinstance(result, dict) and isinstance(result.get("message"), str) and result["message"].strip():
            texts.append(result["message"])
        messages = value.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict):
                    author = str(message.get("author") or "").lower()
                    text = message.get("text")
                    if isinstance(text, str) and text.strip() and author != "user":
                        texts.append(text)
        cursor = value.get("writeAtCursor")
        if isinstance(cursor, str) and cursor.strip():
            texts.append(cursor)
        for child in value.values():
            texts.extend(_collect_response_texts(child))
    elif isinstance(value, list):
        for child in value:
            texts.extend(_collect_response_texts(child))
    deduped: list[str] = []
    for text in texts:
        if text not in deduped:
            deduped.append(text)
    return deduped


def _collect_result_objects(value: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if isinstance(value, dict):
        result = value.get("result")
        if isinstance(result, dict):
            results.append(result)
        for child in value.values():
            results.extend(_collect_result_objects(child))
    elif isinstance(value, list):
        for child in value:
            results.extend(_collect_result_objects(child))
    return results


def _connect_ws(url: str, timeout_seconds: int) -> websocket.WebSocket:
    headers = [
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        "Accept-Language: en-US,en;q=0.9,fr;q=0.8",
        "Cache-Control: no-cache",
        "Pragma: no-cache",
    ]
    return websocket.create_connection(
        url,
        timeout=timeout_seconds,
        origin="https://m365.cloud.microsoft",
        header=headers,
        enable_multithread=False,
    )


def _handshake(ws: websocket.WebSocket, timeout_seconds: int) -> None:
    ws.send(_signalr_payload({"protocol": "json", "version": 1}))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        payload = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        for part in _decode_signalr_frames(payload):
            if part.get("json") == {}:
                return
    raise CopilotRuntimeError("Websocket opened, but SignalR handshake acknowledgement was not received.")


def _send_turn(ws: websocket.WebSocket, payload_text: str, timeout_seconds: int) -> tuple[str, list[dict[str, Any]]]:
    ws.send(payload_text)
    frames: list[dict[str, Any]] = [{"direction": "sent", "payload_text": payload_text, "at_epoch": time.time()}]
    answer_texts: list[str] = []
    result_objects: list[dict[str, Any]] = []
    saw_completion = False
    error_message = None
    start = time.monotonic()
    last_ping = start
    while time.monotonic() - start < timeout_seconds:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            now = time.monotonic()
            if now - last_ping >= 15:
                ws.send(_signalr_payload({"type": 6}))
                frames.append({"direction": "sent", "payload_text": _signalr_payload({"type": 6}), "at_epoch": time.time()})
                last_ping = now
            continue
        except Exception as exc:
            error_message = repr(exc)
            break
        payload = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        frames.append({"direction": "received", "payload_text": payload, "at_epoch": time.time()})
        for part in _decode_signalr_frames(payload):
            parsed = part.get("json")
            if not isinstance(parsed, dict):
                continue
            for text in _collect_response_texts(parsed):
                if text not in answer_texts:
                    answer_texts.append(text)
            for result in _collect_result_objects(parsed):
                if result not in result_objects:
                    result_objects.append(result)
            if parsed.get("type") in {3, 7}:
                saw_completion = True
        if saw_completion and answer_texts:
            break
    invalid = [
        item
        for item in result_objects
        if str(item.get("value") or "").lower() not in {"", "success"} or item.get("errorCode")
    ]
    if invalid:
        raise CopilotRuntimeError(f"Copilot returned an invalid request result: {invalid[:2]}")
    if not answer_texts:
        detail = f" Last websocket error: {error_message}" if error_message else ""
        raise CopilotRuntimeError(f"No answer text was received from Copilot before timeout.{detail}")
    return _select_response_text(answer_texts), frames


class _SessionBundle:
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.frames_path = _latest_file(session_dir, "private_websocket_raw_frames_*.json")
        self.templates_path = _latest_file(session_dir, "private_replay_templates_*.json")
        if not self.frames_path or not self.templates_path:
            raise CopilotSessionError(
                "No direct-call API session was found. Run: "
                'python -m copilot_runtime.login --site cloud --bootstrap-input "sample.pdf" --page 5'
            )
        self.frames = _load_json(self.frames_path)
        self.templates = _load_json(self.templates_path)
        if not isinstance(self.frames, list) or not isinstance(self.templates, list):
            raise CopilotSessionError("API session files are malformed.")


class CopilotRuntime:
    def __init__(
        self,
        *,
        profile_dir: str | Path | None = None,
        session_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        timeout_seconds: int = 180,
        cleanup: bool = True,
        save_private_frames: bool | None = None,
    ) -> None:
        self.profile_dir = _resolve_path(profile_dir, "COPILOT_RUNTIME_PROFILE", DEFAULT_PROFILE_DIR)
        self.session_dir = _resolve_path(session_dir, "COPILOT_RUNTIME_SESSION", DEFAULT_SESSION_DIR)
        self.output_dir = _resolve_path(output_dir, "COPILOT_RUNTIME_OUTPUTS", DEFAULT_OUTPUT_DIR)
        self.timeout_seconds = timeout_seconds
        self.cleanup = cleanup
        self.save_private_frames = (
            os.getenv("COPILOT_RUNTIME_SAVE_PRIVATE_FRAMES", "").lower() in {"1", "true", "yes"}
            if save_private_frames is None
            else save_private_frames
        )
        self.last_metadata: dict[str, Any] | None = None

    def extract(
        self,
        input_path: str | Path | list[str | Path],
        *,
        page: int | list[int] = 1,
        pages: str | int | list[int] | None = None,
        prompt: str | None = None,
        dpi: int = 200,
        cleanup: bool | None = None,
        combine_inputs: bool = False,
        batch_size: int | None = None,
        return_metadata: bool = False,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        page_spec: str | int | list[int] = pages if pages is not None else page
        jobs = self._expand_jobs(input_path, page_spec)
        prompt_text = prompt or DEFAULT_PROMPT
        if isinstance(page_spec, str) and page_spec.lower() == "all":
            return self._extract_full_pdf_jobs(jobs, prompt_text, dpi, cleanup, batch_size, return_metadata)
        if combine_inputs:
            return self._extract_group(jobs, prompt_text, dpi, cleanup, return_metadata)
        results = [self._extract_group([job], prompt_text, dpi, cleanup, return_metadata) for job in jobs]
        return results if len(results) != 1 else results[0]

    def extract_full_pdf(
        self,
        input_path: str | Path,
        *,
        prompt: str | None = None,
        dpi: int = 200,
        cleanup: bool | None = None,
        batch_size: int | None = None,
        return_metadata: bool = False,
    ) -> dict[str, Any]:
        return self.extract(
            input_path=input_path,
            pages="all",
            prompt=prompt,
            dpi=dpi,
            cleanup=cleanup,
            batch_size=batch_size,
            return_metadata=return_metadata,
        )  # type: ignore[return-value]

    def _expand_one_path(self, path: Path, page: str | int | list[int]) -> list[tuple[Path, int]]:
        if isinstance(page, str):
            if page.lower() != "all":
                raise ValueError("pages/page string value must be 'all'.")
            if path.suffix.lower() in PDF_SUFFIXES:
                return [(path, page_number) for page_number in range(1, _pdf_page_count(path) + 1)]
            return [(path, 1)]
        if _is_sequence(page):
            return [(path, int(item)) for item in page]  # type: ignore[arg-type]
        return [(path, int(page))]

    def _expand_jobs(self, input_path: str | Path | list[str | Path], page: str | int | list[int]) -> list[tuple[Path, int]]:
        if _is_sequence(input_path):
            paths = [Path(item).expanduser().resolve() for item in input_path]  # type: ignore[arg-type]
            if isinstance(page, str) and page.lower() == "all":
                jobs: list[tuple[Path, int]] = []
                for path in paths:
                    jobs.extend(self._expand_one_path(path, page))
                return jobs
            pages = list(page) if _is_sequence(page) else [int(page)] * len(paths)  # type: ignore[arg-type]
            if len(pages) != len(paths):
                raise ValueError("When input_path and page are both lists, they must have the same length.")
            return [(path, int(pages[index])) for index, path in enumerate(paths)]
        path = Path(input_path).expanduser().resolve()  # type: ignore[arg-type]
        return self._expand_one_path(path, page)

    def _extract_full_pdf_jobs(
        self,
        jobs: list[tuple[Path, int]],
        prompt: str,
        dpi: int,
        cleanup_override: bool | None,
        batch_size: int | None,
        return_metadata: bool,
    ) -> dict[str, Any]:
        if not jobs:
            raise ValueError("No pages were selected for full-PDF extraction.")
        pdf_paths = sorted({str(path) for path, _ in jobs if path.suffix.lower() in PDF_SUFFIXES})
        page_counts = {path: _pdf_page_count(Path(path)) for path in pdf_paths}
        selected_pages = [page_number for _, page_number in jobs]
        if batch_size is None:
            effective_batch_size = int(os.getenv("COPILOT_RUNTIME_FULL_PDF_BATCH_SIZE", "8"))
        else:
            effective_batch_size = int(batch_size)
        if effective_batch_size <= 0:
            effective_batch_size = len(jobs)
        batches = [jobs[index : index + effective_batch_size] for index in range(0, len(jobs), effective_batch_size)]

        batch_records: list[dict[str, Any]] = []
        for batch_index, batch_jobs in enumerate(batches, start=1):
            batch_pages = [page_number for _, page_number in batch_jobs]
            batch_prompt = self._full_pdf_batch_prompt(prompt, batch_index, len(batches), batch_pages)
            batch_metadata = self._extract_group(batch_jobs, batch_prompt, dpi, cleanup_override, True)
            batch_records.append(
                {
                    "batch": batch_index,
                    "pages": batch_pages,
                    "parsed": batch_metadata.get("parsed"),
                    "raw_response": batch_metadata.get("raw_response"),
                    "metadata_path": str(self.output_dir / batch_metadata["run_id"] / f"{batch_metadata['run_id']}_metadata.json"),
                    "upload_count": batch_metadata.get("upload_count"),
                }
            )

        if len(batch_records) == 1:
            final_metadata = batch_metadata
            final_metadata["full_pdf"] = {
                "enabled": True,
                "pdf_page_counts": page_counts,
                "selected_pages": selected_pages,
                "batch_size": effective_batch_size,
                "batch_count": len(batches),
                "combine_inputs": True,
                "batches": batch_records,
                "final_consolidation": False,
            }
            self.last_metadata = final_metadata
            _write_json(
                self.output_dir / final_metadata["run_id"] / f"{final_metadata['run_id']}_metadata.json",
                final_metadata,
            )
            return final_metadata if return_metadata else final_metadata["parsed"]

        final_prompt = self._full_pdf_consolidation_prompt(prompt, page_counts, batch_records)
        final_metadata = self._extract_group([], final_prompt, dpi, cleanup_override, True)
        final_metadata["full_pdf"] = {
            "enabled": True,
            "pdf_page_counts": page_counts,
            "selected_pages": selected_pages,
            "batch_size": effective_batch_size,
            "batch_count": len(batches),
            "combine_inputs": True,
            "batches": batch_records,
            "final_consolidation": True,
        }
        self.last_metadata = final_metadata
        _write_json(
            self.output_dir / final_metadata["run_id"] / f"{final_metadata['run_id']}_metadata.json",
            final_metadata,
        )
        return final_metadata if return_metadata else final_metadata["parsed"]

    def _full_pdf_batch_prompt(self, prompt: str, batch_index: int, batch_count: int, pages: list[int]) -> str:
        return (
            f"{prompt.strip()}\n\n"
            f"Batch context: this is batch {batch_index} of {batch_count} from the same PDF. "
            f"The attached images are PDF pages: {pages}. "
            "Extract only evidence visible in these attached pages. "
            "In notes, explicitly mention the page numbers where evidence was found."
        )

    def _full_pdf_consolidation_prompt(
        self,
        prompt: str,
        page_counts: dict[str, int],
        batch_records: list[dict[str, Any]],
    ) -> str:
        compact_batches = [
            {"batch": item["batch"], "pages": item["pages"], "parsed": item["parsed"]}
            for item in batch_records
        ]
        return (
            f"{prompt.strip()}\n\n"
            "You are now consolidating batch-level JSON extractions from the SAME full PDF. "
            "Use all batches together. Prefer non-null values supported by page-number notes. "
            "Do not invent values. Return ONLY one final valid JSON matching the schema.\n\n"
            f"PDF page counts: {json.dumps(page_counts, ensure_ascii=False)}\n"
            f"Batch extraction JSON array: {json.dumps(compact_batches, ensure_ascii=False)}"
        )

    def _extract_group(
        self,
        jobs: list[tuple[Path, int]],
        prompt: str,
        dpi: int,
        cleanup_override: bool | None,
        return_metadata: bool,
    ) -> dict[str, Any]:
        for path, _ in jobs:
            if not path.exists():
                raise FileNotFoundError(path)
        run_id = _run_id("copilot_direct")
        work_dir = self.output_dir / run_id
        work_dir.mkdir(parents=True, exist_ok=True)
        prepared_images: list[Path] = []
        metadata: dict[str, Any] = {
            "run_id": run_id,
            "mode": "direct_signalr_websocket",
            "profile_dir": str(self.profile_dir),
            "session_dir": str(self.session_dir),
            "inputs": [],
            "cleanup": {"enabled": self.cleanup if cleanup_override is None else cleanup_override, "attempted": False, "ok": False},
        }
        for index, (path, page_number) in enumerate(jobs, start=1):
            image, mode = _prepare_input_image(path, work_dir, f"input_{index}", page_number, dpi)
            prepared_images.append(image)
            metadata["inputs"].append({"input_path": str(path), "page": page_number, "mode": mode, "image_path": str(image)})

        session = _SessionBundle(self.session_dir)
        chathub_url = _find_chathub_url(session.frames)
        chathub_url, url_replacements = _mutate_url_ids(chathub_url, mutate_conversation_id=True)
        conversation_id = _query_value(chathub_url, "ConversationId")
        if not conversation_id:
            raise CopilotSessionError("Chathub URL did not contain ConversationId.")
        prompt_template, metrics_template = _find_prompt_template(session.frames)
        metadata.update(
            {
                "session_frames": str(session.frames_path),
                "session_templates": str(session.templates_path),
                "conversation_id": conversation_id,
                "url_id_replacements": url_replacements,
                "upload_count": len(prepared_images),
            }
        )

        upload_results = [
            _upload_image(
                image_path=image,
                conversation_id=conversation_id,
                chathub_url=chathub_url,
                templates=session.templates,
                session_dir=self.session_dir,
                profile_dir=self.profile_dir,
            )
            for image in prepared_images
        ]
        metadata["upload_results"] = [
            {key: value for key, value in item.items() if key.lower() not in {"sasurl", "url"}}
            for item in upload_results
        ]

        prompt_frame = _mutate_prompt(prompt_template, prompt_text=prompt, invocation_id="0", keep_images=True)
        _set_prompt_image_annotations(prompt_frame, upload_results)
        metrics = _mutate_metrics(metrics_template)
        payload = _signalr_payload(prompt_frame, metrics)

        ws = None
        try:
            ws = _connect_ws(chathub_url, min(30, self.timeout_seconds))
            _handshake(ws, min(30, self.timeout_seconds))
            raw_response, websocket_frames = _send_turn(ws, payload, self.timeout_seconds)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

        parsed = _parse_first_json_value(raw_response)
        metadata["raw_response"] = raw_response
        metadata["parsed"] = parsed
        metadata["websocket_frame_count"] = len(websocket_frames)

        cleanup_enabled = self.cleanup if cleanup_override is None else cleanup_override
        if cleanup_enabled:
            metadata["cleanup"] = self._cleanup_conversation(conversation_id, chathub_url)

        _write_text(work_dir / f"{run_id}_raw_response.txt", raw_response)
        _write_json(work_dir / f"{run_id}_parsed.json", parsed)
        _write_json(work_dir / f"{run_id}_metadata.json", metadata)
        if self.save_private_frames:
            _write_json(work_dir / f"private_{run_id}_websocket_frames.json", websocket_frames)

        self.last_metadata = metadata
        return metadata if return_metadata else parsed

    def _cleanup_conversation(self, conversation_id: str, chathub_url: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": True,
            "attempted": False,
            "ok": False,
            "conversation_id": conversation_id,
            "method": "POST https://substrate.office.com/m365Copilot/DeleteConversation",
            "note": "Best-effort direct cleanup; some tenants reject this endpoint/payload.",
        }
        token = _query_value(chathub_url, "access_token")
        anchor = None
        try:
            anchor = urlsplit(chathub_url).path.split("/Chathub/", 1)[1].split("/", 1)[0]
        except Exception:
            pass
        if not token:
            result["reason"] = "No access_token was present in the Chathub URL."
            return result
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "referer": "https://m365.cloud.microsoft/",
            "x-scenario": "OfficeWebIncludedCopilot",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if anchor:
            headers["x-anchormailbox"] = f"Oid:{anchor}"
        result["attempted"] = True
        start = time.monotonic()
        try:
            response = httpx.post(
                "https://substrate.office.com/m365Copilot/DeleteConversation",
                headers=headers,
                json={"conversationId": conversation_id, "source": "officeweb", "threadType": "bizchat"},
                timeout=2,
            )
            result["status_code"] = response.status_code
            result["ok"] = response.is_success
            if not response.is_success:
                result["reason"] = response.text[:300]
        except Exception as exc:
            result["reason"] = repr(exc)
        result["elapsed_seconds"] = round(time.monotonic() - start, 3)
        return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a direct Microsoft Copilot SignalR/websocket extraction.")
    parser.add_argument("--input", required=True, action="append", help="PDF or image path. Repeat for multiple files.")
    parser.add_argument("--page", type=int, default=1, help="PDF page to render. Ignored for images.")
    parser.add_argument("--pages", default=None, help='Use "all" to render every PDF page, or omit to use --page.')
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--profile-dir", default=None)
    parser.add_argument("--session-dir", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--no-cleanup", action="store_true")
    parser.add_argument("--save-private-frames", action="store_true")
    parser.add_argument("--combine-inputs", action="store_true")
    parser.add_argument("--batch-size", type=int, default=None, help="Pages/images per full-PDF batch. Use 0 for one turn.")
    parser.add_argument("--metadata", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    runtime = CopilotRuntime(
        profile_dir=args.profile_dir,
        session_dir=args.session_dir,
        output_dir=args.output_dir,
        cleanup=not args.no_cleanup,
        save_private_frames=args.save_private_frames,
    )
    inputs: str | list[str] = args.input[0] if len(args.input) == 1 else args.input
    result = runtime.extract(
        inputs,
        page=args.page,
        pages=args.pages,
        prompt=args.prompt,
        combine_inputs=args.combine_inputs,
        batch_size=args.batch_size,
        return_metadata=args.metadata,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
