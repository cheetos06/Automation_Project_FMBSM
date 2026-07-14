from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError, sync_playwright

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILE_DIR = PACKAGE_DIR / "browser_profile"
DEFAULT_SESSION_DIR = PACKAGE_DIR / "api_session"
DEFAULT_OUTPUT_DIR = PACKAGE_DIR / "outputs"
DEFAULT_PROMPT = 'Reply with only this JSON: {"bootstrap": true}'
_INTERACTION_CALLBACK: Callable[[str], None] | None = None


def set_interaction_callback(callback: Callable[[str], None] | None) -> None:
    global _INTERACTION_CALLBACK
    _INTERACTION_CALLBACK = callback


def _pause(message: str) -> None:
    if _INTERACTION_CALLBACK is not None:
        _INTERACTION_CALLBACK(message)
    else:
        input(message)


def _resolve_path(value: str | Path | None, env_name: str, default: Path) -> Path:
    selected = value or os.getenv(env_name) or default
    return Path(selected).expanduser().resolve()


def _run_id(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _prepare_input_image(
    input_path: Path,
    work_dir: Path,
    prefix: str,
    page: int,
    dpi: int,
) -> tuple[Path, str]:
    del work_dir, prefix, page, dpi
    path = input_path.expanduser().resolve()
    if not path.exists() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("Token Pool Client bootstrap requires its bundled sample image")
    return path, "image"


SITE_ALIASES = {
    "consumer": "https://copilot.microsoft.com/",
    "copilot": "https://copilot.microsoft.com/",
    "work": "https://copilot.cloud.microsoft/",
    "cloud": "https://copilot.cloud.microsoft/",
    "m365": "https://copilot.cloud.microsoft/",
}


def resolve_site(value: str | None) -> str:
    site = value or os.getenv("COPILOT_RUNTIME_SITE") or "https://copilot.cloud.microsoft/"
    return SITE_ALIASES.get(site.lower(), site)


def launch_context(playwright: Any, profile_dir: Path, *, channel: str | None, headless: bool) -> Any:
    profile_dir.mkdir(parents=True, exist_ok=True)
    selected_channel = (channel or os.getenv("COPILOT_BROWSER_CHANNEL") or "msedge").strip().lower()
    kwargs: dict[str, Any] = {
        "headless": headless,
        "accept_downloads": True,
        "viewport": {"width": 1440, "height": 950},
        "slow_mo": 0 if headless else 50,
        "args": ["--start-maximized"],
    }
    if selected_channel and selected_channel != "chromium":
        kwargs["channel"] = selected_channel
    try:
        return playwright.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    except PlaywrightError:
        if "channel" not in kwargs:
            raise
        kwargs.pop("channel", None)
        return playwright.chromium.launch_persistent_context(str(profile_dir), **kwargs)


def wait_for_page_settle(page: Page, timeout_ms: int = 15000) -> None:
    for state in ("domcontentloaded", "load"):
        try:
            page.wait_for_load_state(state, timeout=timeout_ms)
        except TimeoutError:
            pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except TimeoutError:
        pass
    page.wait_for_timeout(2000)


def visible_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def first_visible(page: Page, selectors: list[str], *, prefer_last: bool = False) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 30)
        except Exception:
            continue
        indexes = range(count - 1, -1, -1) if prefer_last else range(count)
        for index in indexes:
            item = locator.nth(index)
            try:
                if item.is_visible(timeout=500):
                    return item
            except Exception:
                continue
    return None


def prompt_box(page: Page) -> Locator | None:
    return first_visible(
        page,
        [
            'textarea[placeholder*="Ask" i]',
            'textarea[placeholder*="Message" i]',
            'textarea[aria-label*="Ask" i]',
            'textarea[aria-label*="Message" i]',
            'div[role="textbox"][contenteditable="true"]',
            '[role="textbox"][contenteditable="true"]',
            '[contenteditable="true"]',
            "textarea",
        ],
        prefer_last=True,
    )


def looks_like_login_or_blocked(page: Page) -> bool:
    text = visible_text(page).lower()
    return any(
        term in text
        for term in [
            "sign in",
            "log in",
            "se connecter",
            "connectez-vous",
            "pick an account",
            "verify your identity",
            "stay signed in",
            "mfa",
        ]
    )


def start_new_chat_if_possible(page: Page) -> bool:
    button = first_visible(
        page,
        [
            '[data-testid="newChatButton"]',
            'button[aria-label="New chat"]',
            'button:has-text("New chat")',
            'button:has-text("Nouvelle conversation")',
        ],
        prefer_last=True,
    )
    if button is None:
        return False
    try:
        button.click(timeout=3000)
        page.wait_for_timeout(2500)
        return True
    except Exception:
        return False


def wait_for_file_input(page: Page, timeout_ms: int = 10000) -> Locator | None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        locator = page.locator('input[type="file"]')
        try:
            if locator.count() > 0:
                return locator.first()
        except Exception:
            pass
        page.wait_for_timeout(500)
    return None


def click_sources_menu_if_present(page: Page) -> bool:
    source_button = first_visible(
        page,
        [
            'button[aria-label*="Add and manage sources" i]',
            'button[aria-label*="Add source" i]',
            'button[aria-label*="Manage sources" i]',
            'button[title*="Add and manage sources" i]',
            'button[aria-label*="Sources" i]',
        ],
        prefer_last=True,
    )
    if source_button is None:
        print("Upload: sources button not detected.")
        return False
    try:
        print("Upload: opening sources menu.")
        source_button.click(timeout=3000)
        page.wait_for_timeout(1500)
        return True
    except Exception as exc:
        print(f"Upload: sources button click failed: {exc}")
        return False


def upload_image_or_pause(page: Page, image_path: Path, *, interactive: bool = True) -> str:
    opened_sources_menu = click_sources_menu_if_present(page)
    direct_input = wait_for_file_input(page, timeout_ms=10000 if opened_sources_menu else 1000)
    if direct_input is not None:
        try:
            print("Upload: attaching image through hidden file input.")
            direct_input.set_input_files(str(image_path), timeout=10000)
            page.wait_for_timeout(8000)
            return "input[type=file] after sources menu" if opened_sources_menu else "input[type=file]"
        except Exception as exc:
            print(f"Upload: hidden file input failed, trying file chooser. Details: {exc}")
    else:
        print("Upload: hidden file input unavailable, trying file chooser.")

    button_names = re.compile(
        r"(attach|attachment|upload|image|file|paperclip|joindre|"
        r"device|charger|televerser|téléverser)",
        re.IGNORECASE,
    )
    button_selectors = [
        '[role="menuitem"]:has-text("Upload images and files")',
        '[role="menuitem"]:has-text("Upload")',
        '[role="menuitem"]:has-text("images and files")',
        'button[aria-label*="Attach" i]',
        'button[aria-label*="Upload" i]',
        'button[aria-label*="device" i]',
        'button[aria-label*="Image" i]',
        'button[title*="Attach" i]',
        'button[title*="Upload" i]',
        'button[title*="device" i]',
        'button[title*="Image" i]',
    ]
    candidates: list[Locator] = []
    for role in ("button", "menuitem"):
        locator = page.get_by_role(role, name=button_names)
        try:
            count = min(locator.count(), 20)
        except Exception:
            count = 0
        for index in range(count):
            candidates.append(locator.nth(index))
    for selector in button_selectors:
        item = first_visible(page, [selector])
        if item is not None:
            candidates.append(item)

    for candidate in candidates:
        try:
            with page.expect_file_chooser(timeout=3000) as chooser_info:
                candidate.click(timeout=3000)
            chooser_info.value.set_files(str(image_path))
            page.wait_for_timeout(8000)
            return "filechooser"
        except Exception:
            continue

    if not interactive:
        raise RuntimeError("Could not find an upload control for the image.")

    print("\nI could not identify Copilot's upload control automatically.")
    print("In the browser window:")
    print("1. Make sure you are on the Copilot chat page.")
    print("2. Click the plus, paperclip, image, or attachment button near the chat box.")
    print(f"3. Choose this rendered image: {image_path}")
    print("4. Wait until the image preview/attachment appears.")
    _pause("Attach the displayed image, then continue in the Token Pool Client.")
    return "manual"


def fill_prompt_or_pause(page: Page, prompt: str, *, interactive: bool = True) -> str:
    box = prompt_box(page)
    if box is not None:
        try:
            box.click(timeout=5000)
            box.fill(prompt, timeout=5000)
            return "filled"
        except Exception:
            try:
                box.click(timeout=5000)
                page.keyboard.insert_text(prompt)
                return "keyboard"
            except Exception:
                pass
    if not interactive:
        raise RuntimeError("Could not find Copilot's prompt input box.")
    print("\nI could not identify Copilot's chat input automatically.")
    _pause("Click inside the Copilot message box, then continue in the Token Pool Client.")
    page.keyboard.insert_text(prompt)
    return "manual"


def submit_prompt_or_pause(page: Page, *, interactive: bool = True) -> str:
    send_name = re.compile(r"(send|submit|ask|go|envoyer|soumettre)", re.IGNORECASE)
    candidates: list[Locator] = []
    role_buttons = page.get_by_role("button", name=send_name)
    try:
        count = min(role_buttons.count(), 20)
    except Exception:
        count = 0
    for index in range(count):
        candidates.append(role_buttons.nth(index))
    for selector in [
        'button[aria-label*="Send" i]',
        'button[aria-label*="Submit" i]',
        'button[aria-label*="Envoyer" i]',
        'button[title*="Send" i]',
        'button[title*="Submit" i]',
        'button[type="submit"]',
    ]:
        item = first_visible(page, [selector], prefer_last=True)
        if item is not None:
            candidates.append(item)
    for candidate in candidates:
        try:
            if candidate.is_enabled(timeout=1000):
                candidate.click(timeout=3000)
                return "button"
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
        return "enter"
    except Exception:
        pass
    if not interactive:
        raise RuntimeError("Could not submit the Copilot prompt.")
    _pause("Click the send button in the browser, then continue in the Token Pool Client.")
    return "manual"


def frame_payload(frame: Any) -> Any:
    if isinstance(frame, dict):
        return frame.get("payload", frame)
    return getattr(frame, "payload", frame)


def payload_to_text(payload: Any) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, dict) and "payload" in payload:
        return payload_to_text(payload["payload"])
    return str(payload)


def payload_messages(payload_text: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for part in str(payload_text).split("\x1e"):
        if not part:
            continue
        try:
            parsed = json.loads(part)
        except Exception:
            continue
        if isinstance(parsed, dict):
            messages.append(parsed)
    return messages


class BootstrapCapture:
    def __init__(self) -> None:
        self.private_replay_templates: list[dict[str, Any]] = []
        self.private_frames: list[dict[str, Any]] = []
        self._sequence = 0

    def attach(self, page: Page) -> None:
        page.on("request", self.on_request)
        page.on("websocket", self.on_websocket)

    def on_request(self, request: Any) -> None:
        if request.method.upper() not in {"POST", "PUT", "PATCH"}:
            return
        if request.resource_type not in {"xhr", "fetch"}:
            return
        url = request.url
        if not any(term in url.lower() for term in ["copilot", "substrate", "office", "cloud.microsoft"]):
            return
        content_type = request.headers.get("content-type") if request.headers else None
        post_data = None
        post_data_buffer = None
        try:
            post_data = request.post_data
        except Exception:
            pass
        try:
            post_data_buffer = request.post_data_buffer
        except Exception:
            pass
        private_body = None
        private_body_encoding = "none"
        if post_data is not None and len(post_data) <= 10_000_000:
            private_body = post_data
            private_body_encoding = "text"
        elif post_data_buffer is not None and len(post_data_buffer) <= 10_000_000:
            private_body = base64.b64encode(post_data_buffer).decode("ascii")
            private_body_encoding = "base64"
        self.private_replay_templates.append(
            {
                "sequence": len(self.private_replay_templates) + 1,
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "headers": request.headers,
                "body": private_body,
                "body_encoding": private_body_encoding,
                "body_size": len(post_data_buffer or (post_data.encode("utf-8") if post_data else b"")),
                "content_type": content_type,
            }
        )

    def on_websocket(self, websocket: Any) -> None:
        is_chathub = "substrate.office.com/m365Copilot/Chathub" in websocket.url

        def add_frame(direction: str, frame: Any) -> None:
            self._sequence += 1
            payload_text = payload_to_text(frame_payload(frame))
            self.private_frames.append(
                {
                    "sequence": self._sequence,
                    "direction": direction,
                    "at_epoch": time.time(),
                    "url": websocket.url,
                    "is_chathub": is_chathub,
                    "payload_text": payload_text,
                    "payload_size": len(payload_text.encode("utf-8", errors="replace")),
                }
            )

        websocket.on("framesent", lambda frame: add_frame("sent", frame))
        websocket.on("framereceived", lambda frame: add_frame("received", frame))

    def has_upload_template(self) -> bool:
        return any("UploadFile" in str(item.get("url")) for item in self.private_replay_templates)

    def has_chathub_prompt_template(self) -> bool:
        for frame in self.private_frames:
            if not (frame.get("is_chathub") and frame.get("direction") == "sent"):
                continue
            for message in payload_messages(frame.get("payload_text") or ""):
                if message.get("type") == 4 and message.get("target") == "chat":
                    return True
        return False

    def ready(self) -> bool:
        return self.has_upload_template() and self.has_chathub_prompt_template()


def save_msal_state(page: Page, cookies: list[dict[str, Any]], session_dir: Path) -> dict[str, Any]:
    state = page.evaluate(
        """() => ({
          href: location.href,
          origin: location.origin,
          records: Object.keys(localStorage)
            .filter(key => key.toLowerCase().includes('msal'))
            .map(key => ({key, value: localStorage.getItem(key)}))
        })"""
    )
    records = state.get("records", []) if isinstance(state, dict) else []
    wrapper_ids: set[str] = set()
    refresh_ids: set[str] = set()
    for item in records:
        try:
            key = str(item.get("key") or "")
            wrapper = json.loads(item.get("value") or "{}")
            wrapper_id = str(wrapper.get("id") or "")
        except Exception:
            continue
        if wrapper_id:
            wrapper_ids.add(wrapper_id)
            if "refreshtoken" in key.lower():
                refresh_ids.add(wrapper_id)

    encryption_cookies = [item for item in cookies if item.get("name") == "msal.cache.encryption"]

    def cookie_id(cookie: dict[str, Any]) -> str:
        from urllib.parse import unquote

        value = str(cookie.get("value") or "")
        for _ in range(2):
            decoded = unquote(value)
            if decoded == value:
                break
            value = decoded
        try:
            return str(json.loads(value).get("id") or "")
        except Exception:
            return ""

    selected = next((item for item in encryption_cookies if cookie_id(item) in refresh_ids), None)
    selected = selected or next((item for item in encryption_cookies if cookie_id(item) in wrapper_ids), None)
    selected = selected or (encryption_cookies[0] if encryption_cookies else None)
    if selected is None:
        raise RuntimeError("The signed-in browser did not expose the MSAL encryption cookie")
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "private_msal_cache_encryption.txt").write_text(
        f"msal.cache.encryption={selected['value']}\n",
        encoding="utf-8",
    )
    _write_json(
        session_dir / "private_msal_local_storage_current.json",
        {
            "href": state.get("href"),
            "origin": state.get("origin"),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "records": records,
        },
    )
    return {
        "record_count": len(records),
        "encryption_cookie_count": len(encryption_cookies),
        "matched_refresh_cookie": cookie_id(selected) in refresh_ids,
    }


def wait_for_capture_ready(page: Page, capture: BootstrapCapture, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if capture.ready():
            return True
        try:
            page.wait_for_timeout(500)
        except Exception:
            return capture.ready()
    return capture.ready()


def bootstrap_api_session(
    *,
    site: str,
    profile_dir: Path,
    session_dir: Path,
    input_path: Path,
    page_number: int,
    prompt: str,
    dpi: int,
    channel: str | None,
    timeout_seconds: int,
    headless: bool,
) -> Path:
    run_id = _run_id("api_session_bootstrap")
    work_dir = session_dir.parent / "bootstrap-work" / run_id
    image_path, input_mode = _prepare_input_image(input_path, work_dir, run_id, page_number, dpi)
    capture = BootstrapCapture()
    cookies: list[dict[str, Any]] = []
    msal_summary: dict[str, Any] = {}
    session_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = launch_context(playwright, profile_dir, channel=channel, headless=headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            capture.attach(page)
            page.goto(site, wait_until="domcontentloaded")
            wait_for_page_settle(page)
            if start_new_chat_if_possible(page):
                wait_for_page_settle(page, timeout_ms=5000)

            if looks_like_login_or_blocked(page) or prompt_box(page) is None:
                print("\nSign in or finish MFA in the browser window. Stop when the Copilot message box is visible.")
                _pause("Finish sign-in/MFA until the Copilot chat box is visible, then continue.")
                wait_for_page_settle(page)
            if looks_like_login_or_blocked(page) or prompt_box(page) is None:
                raise RuntimeError("Copilot chat input was not detected after login.")

            upload_method = upload_image_or_pause(page, image_path, interactive=True)
            prompt_method = fill_prompt_or_pause(page, prompt, interactive=True)
            submit_method = submit_prompt_or_pause(page, interactive=True)
            ready = wait_for_capture_ready(page, capture, timeout_seconds)
            try:
                cookies = context.cookies()
            except Exception:
                cookies = []
            msal_summary = save_msal_state(page, cookies, session_dir)

            _write_json(
                session_dir / f"{run_id}_summary.json",
                {
                    "run_id": run_id,
                    "input_path": str(input_path),
                    "input_mode": input_mode,
                    "image_path": str(image_path),
                    "site": site,
                    "profile_dir": str(profile_dir),
                    "upload_method": upload_method,
                    "prompt_method": prompt_method,
                    "submit_method": submit_method,
                    "ready": ready,
                    "upload_template_count": len(
                        [item for item in capture.private_replay_templates if "UploadFile" in str(item.get("url"))]
                    ),
                    "websocket_frame_count": len(capture.private_frames),
                    "msal": msal_summary,
                },
            )
        finally:
            _write_json(session_dir / f"private_websocket_raw_frames_{run_id}.json", capture.private_frames)
            _write_json(session_dir / f"private_replay_templates_{run_id}.json", capture.private_replay_templates)
            _write_json(session_dir / f"private_playwright_cookies_{run_id}.json", cookies)
            try:
                context.close()
            except Exception:
                pass

    if not capture.has_upload_template():
        raise RuntimeError("Bootstrap did not capture an UploadFile request.")
    if not capture.has_chathub_prompt_template():
        raise RuntimeError("Bootstrap did not capture a Chathub SignalR target=chat prompt frame.")
    return session_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Login and bootstrap direct Microsoft Copilot UploadFile + SignalR session.")
    parser.add_argument("--site", default=None, help="cloud/m365/work, consumer/copilot, or a full URL.")
    parser.add_argument("--profile-dir", default=None, help="Defaults to copilot_runtime/browser_profile.")
    parser.add_argument("--session-dir", default=None, help="Defaults to copilot_runtime/api_session.")
    parser.add_argument("--channel", default=None, help="Browser channel. Defaults to msedge, falls back to Chromium.")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--bootstrap-input", default=None, help="PDF or image used once to capture direct-call templates.")
    parser.add_argument("--page", type=int, default=1, help="PDF page to render for bootstrap.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt used for bootstrap capture.")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--timeout", type=int, default=60)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    site = resolve_site(args.site)
    profile_dir = _resolve_path(args.profile_dir, "COPILOT_RUNTIME_PROFILE", DEFAULT_PROFILE_DIR)
    session_dir = _resolve_path(args.session_dir, "COPILOT_RUNTIME_SESSION", DEFAULT_SESSION_DIR)

    if args.bootstrap_input:
        bootstrap_api_session(
            site=site,
            profile_dir=profile_dir,
            session_dir=session_dir,
            input_path=Path(args.bootstrap_input).expanduser().resolve(),
            page_number=args.page,
            prompt=args.prompt,
            dpi=args.dpi,
            channel=args.channel,
            timeout_seconds=args.timeout,
            headless=args.headless,
        )
        print(f"\nDirect-call API session saved under:\n  {session_dir}")
        print("Runtime extraction can now use direct UploadFile + SignalR websocket calls without opening the browser.")
        return 0

    with sync_playwright() as playwright:
        context = launch_context(playwright, profile_dir, channel=args.channel, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(site, wait_until="domcontentloaded")
        wait_for_page_settle(page)
        print("\nCopilot Runtime browser profile:")
        print(f"  {profile_dir}")
        print("\nIn the browser window:")
        print("1. Sign in with your company Microsoft account if prompted.")
        print("2. Complete MFA/conditional-access prompts.")
        print("3. Accept any Copilot welcome/terms dialogs.")
        print("4. Stop when the Copilot chat message box is visible.")
        _pause("When the Copilot chat box is visible, continue in the Token Pool Client.")
        wait_for_page_settle(page)
        ok = prompt_box(page) is not None and not looks_like_login_or_blocked(page)
        context.close()
    if not ok:
        print("\nI did not detect the chat box. Run login again when login is complete.", file=sys.stderr)
        return 1
    print("\nLogin/profile check complete.")
    print('To enable direct calls, run again with --bootstrap-input "sample.pdf" --page 5.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
