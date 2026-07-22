from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PIPELINE_DIR = Path(__file__).resolve().parent
DEFAULT_SETTINGS_PATH = PIPELINE_DIR / "config" / "settings.json"
_ACCOUNT_REFRESH_LOCK = threading.Lock()
_RATE_LIMIT_MARKERS = (
    "peruserthrottled",
    "throttl",
    "too many requests",
    "http 429",
    "rate limit",
    "rate-limit",
    "rate_limit",
    "rate-limit-exceeded",
    "nextavailableat",
    "usage limit reached",
    "quota exceeded",
    "limite du nombre de demandes",
    "volume de requ",
)


@dataclass(frozen=True)
class CopilotAccount:
    optimda_dir: Path | None
    profile_dir: Path
    session_dir: Path
    name: str


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_settings(path: Path | None = None) -> dict[str, Any]:
    settings_path = (path or DEFAULT_SETTINGS_PATH).resolve()
    settings = read_json(settings_path)
    settings["_settings_path"] = str(settings_path)
    if os.getenv("FMBSM_JOB_ID"):
        settings["_job_id"] = os.environ["FMBSM_JOB_ID"]
    return settings


def resolve_from_pipeline(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (PIPELINE_DIR / path).resolve()


def prompt_path(settings: dict[str, Any], key: str) -> Path:
    return resolve_from_pipeline(settings[key])


def load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def load_copilot_accounts(settings: dict[str, Any]) -> list[CopilotAccount]:
    registry_db = str(settings.get("copilot_registry_db") or os.getenv("COPILOT_REGISTRY_DB") or "").strip()
    accounts_dir = str(settings.get("copilot_accounts_dir") or os.getenv("COPILOT_DATA_DIR") or "").strip()
    if registry_db or accounts_dir or "optimda_build2_dir" not in settings:
        from copilot_service.registry import CopilotRegistry
        from copilot_service.token_refresh import fresh_runtime_accounts

        registry = CopilotRegistry(
            data_dir=Path(accounts_dir) if accounts_dir else None,
            db_path=Path(registry_db) if registry_db else None,
        )
        records = fresh_runtime_accounts(registry)
        requested_names = settings.get("copilot_account_names")
        if isinstance(requested_names, str):
            requested_names = [requested_names]
        requested = {str(value).lower() for value in (requested_names or []) if value}
        if requested:
            records = [
                record
                for record in records
                if record.account_id.lower() in requested or record.username.lower() in requested
            ]
        if not records:
            status = registry.status()
            raise RuntimeError(
                "No usable Copilot session is available in the AWS pool. "
                f"Uploaded={status['account_count']} available={status['available_account_count']}. "
                "Run the Token Pool Client on a signed-in Windows computer."
            )
        return [
            CopilotAccount(
                optimda_dir=None,
                profile_dir=record.session_path,
                session_dir=record.session_path,
                name=record.account_id,
            )
            for record in records
        ]

    optimda_dir = resolve_from_pipeline(settings["optimda_build2_dir"])
    accounts_path = optimda_dir / "config" / "accounts.json"
    accounts_config = read_json(accounts_path)
    requested_names = settings.get("copilot_account_names")
    if isinstance(requested_names, str):
        requested_names = [requested_names]
    if not requested_names:
        requested_name = settings.get("copilot_account_name")
        requested_names = [requested_name] if requested_name else []
    candidates = [
        item
        for item in accounts_config.get("accounts", [])
        if item.get("enabled", True)
    ]
    if requested_names:
        by_name = {str(item.get("name")): item for item in candidates}
        missing = [name for name in requested_names if name not in by_name]
        if missing:
            raise ValueError(
                f"Enabled Copilot accounts not found in {accounts_path}: {missing}"
            )
        candidates = [by_name[name] for name in requested_names]
    if not candidates:
        raise ValueError(f"No enabled Copilot account found in {accounts_path}.")

    result: list[CopilotAccount] = []
    for account in candidates:
        profile_dir = Path(account["edge_profile_path"]).expanduser().resolve()
        session_dir = Path(account["session_dir"]).expanduser()
        if not session_dir.is_absolute():
            session_dir = optimda_dir / session_dir
        result.append(
            CopilotAccount(
                optimda_dir=optimda_dir,
                profile_dir=profile_dir.resolve(),
                session_dir=session_dir.resolve(),
                name=str(account.get("name") or "copilot"),
            )
        )
    return result


def load_copilot_account(settings: dict[str, Any]) -> CopilotAccount:
    return load_copilot_accounts(settings)[0]


def is_copilot_rate_limit_error(exc: BaseException) -> bool:
    """Recognize the throttle responses emitted by Copilot and Build 2."""

    messages: list[str] = []
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        messages.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    text = "\n".join(messages).lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _refresh_accounts_after_503(exc: Exception, account: CopilotAccount) -> bool:
    message = str(exc).lower()
    if "http 503" not in message and "service unavailable" not in message:
        return False
    if account.optimda_dir is None:
        from copilot_service.registry import CopilotRegistry
        from copilot_service.token_refresh import ensure_account_fresh

        registry = CopilotRegistry.from_env()
        record = registry.get_account(account.name)
        if record is None:
            return False
        with _ACCOUNT_REFRESH_LOCK:
            ensure_account_fresh(registry, record, minimum_remaining_seconds=3600)
        return True
    refresh_script = account.optimda_dir / "refresh_accounts.py"
    if not refresh_script.exists():
        return False
    with _ACCOUNT_REFRESH_LOCK:
        print("[copilot] HTTP 503 detected; refreshing Build 2 accounts before retry.")
        subprocess.run(
            [sys.executable, str(refresh_script)],
            cwd=account.optimda_dir,
            check=True,
            timeout=240,
        )
    return True


def _ensure_copilot_import_path(optimda_dir: Path | None) -> None:
    if optimda_dir is None:
        return
    bin_dir = str((optimda_dir / "bin").resolve())
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)


def run_copilot_pdf_extraction(
    pdf_path: Path,
    prompt_file: Path,
    run_dir: Path,
    settings: dict[str, Any],
    *,
    tag: str,
) -> dict[str, Any]:
    """Render PDF pages to images through the OPTIMDA runtime and ask Copilot."""

    account = load_copilot_account(settings)
    _ensure_copilot_import_path(account.optimda_dir)

    from copilot_runtime.runtime import CopilotRuntime  # type: ignore

    prompt_text = load_prompt(prompt_file)
    run_dir.mkdir(parents=True, exist_ok=True)
    attempts = max(1, int(settings.get("copilot_retries", 0)) + 1)
    retry_delay = float(settings.get("retry_delay_seconds", 10))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        attempt_dir = run_dir / f"attempt_{attempt}"
        runtime = CopilotRuntime(
            profile_dir=account.profile_dir,
            session_dir=account.session_dir,
            output_dir=attempt_dir,
            timeout_seconds=int(settings.get("timeout_seconds", 300)),
            cleanup=bool(settings.get("cleanup_rendered_images", True)),
            save_private_frames=False,
        )
        try:
            metadata = runtime.extract(
                input_path=pdf_path.resolve(),
                pages="all",
                prompt=prompt_text,
                dpi=int(settings.get("dpi", 180)),
                batch_size=settings.get("batch_size"),
                return_metadata=True,
            )
            break
        except Exception as exc:
            last_error = exc
            write_json(
                attempt_dir / "error.json",
                {
                    "tag": tag,
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            if is_copilot_rate_limit_error(exc):
                raise
            if attempt >= attempts:
                raise
            _refresh_accounts_after_503(exc, account)
            time.sleep(retry_delay * attempt)
    else:
        raise RuntimeError(f"Copilot extraction failed: {last_error}")

    if not isinstance(metadata, dict) or not isinstance(metadata.get("parsed"), dict):
        raise RuntimeError("Copilot did not return a parsed JSON object.")

    parsed = metadata["parsed"]
    write_json(run_dir / f"{tag}_parsed.json", parsed)
    write_json(run_dir / f"{tag}_metadata.json", metadata)
    (run_dir / f"{tag}_raw_response.txt").write_text(
        str(metadata.get("raw_response") or ""),
        encoding="utf-8",
    )
    return parsed


def run_copilot_single_page_extraction(
    pdf_path: Path,
    prompt_file: Path,
    run_dir: Path,
    settings: dict[str, Any],
    *,
    page: int,
    tag: str,
    prompt_context: str | None = None,
) -> dict[str, Any]:
    """Ask Copilot to extract one rendered PDF page image.

    The caller knows the actual PDF page number, so downstream layout
    normalization can override any printed page number Copilot may mention.
    """

    account = load_copilot_account(settings)
    _ensure_copilot_import_path(account.optimda_dir)

    from copilot_runtime.runtime import CopilotRuntime  # type: ignore

    prompt_text = (
        load_prompt(prompt_file).strip()
        + (f"\n\n{prompt_context.strip()}" if prompt_context else "")
        + f"\n\nThis single attached image is actual PDF page {page}. "
        + f"Every returned entry MUST use page: {page}."
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    attempts = max(1, int(settings.get("copilot_retries", 0)) + 1)
    retry_delay = float(settings.get("retry_delay_seconds", 10))
    for attempt in range(1, attempts + 1):
        attempt_dir = run_dir / f"attempt_{attempt}"
        runtime = CopilotRuntime(
            profile_dir=account.profile_dir,
            session_dir=account.session_dir,
            output_dir=attempt_dir,
            timeout_seconds=int(settings.get("timeout_seconds", 300)),
            cleanup=bool(settings.get("cleanup_rendered_images", True)),
            save_private_frames=False,
        )
        try:
            metadata = runtime.extract(
                input_path=pdf_path.resolve(),
                page=page,
                prompt=prompt_text,
                dpi=int(settings.get("dpi", 180)),
                return_metadata=True,
            )
            break
        except Exception as exc:
            write_json(
                attempt_dir / "error.json",
                {
                    "tag": tag,
                    "page": page,
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            if is_copilot_rate_limit_error(exc):
                raise
            if attempt >= attempts:
                raise
            _refresh_accounts_after_503(exc, account)
            time.sleep(retry_delay * attempt)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("parsed"), dict):
        raise RuntimeError("Copilot did not return a parsed JSON object.")

    parsed = metadata["parsed"]
    write_json(run_dir / f"{tag}_page_{page}_parsed.json", parsed)
    write_json(run_dir / f"{tag}_page_{page}_metadata.json", metadata)
    (run_dir / f"{tag}_page_{page}_raw_response.txt").write_text(
        str(metadata.get("raw_response") or ""),
        encoding="utf-8",
    )
    return parsed


def run_copilot_prompt_extraction(
    prompt_file: Path,
    run_dir: Path,
    settings: dict[str, Any],
    *,
    tag: str,
    prompt_context: str,
) -> dict[str, Any]:
    """Ask Copilot to reason over structured context without uploading images."""

    account = load_copilot_account(settings)
    _ensure_copilot_import_path(account.optimda_dir)
    from copilot_runtime.runtime import CopilotRuntime  # type: ignore

    prompt_text = load_prompt(prompt_file).strip() + f"\n\n{prompt_context.strip()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    attempts = max(1, int(settings.get("copilot_retries", 0)) + 1)
    retry_delay = float(settings.get("retry_delay_seconds", 10))
    metadata: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        attempt_dir = run_dir / f"attempt_{attempt}"
        runtime = CopilotRuntime(
            profile_dir=account.profile_dir,
            session_dir=account.session_dir,
            output_dir=attempt_dir,
            timeout_seconds=int(settings.get("timeout_seconds", 300)),
            cleanup=bool(settings.get("cleanup_rendered_images", True)),
            save_private_frames=False,
        )
        try:
            # Build 2 already uses this prompt-only path for full-PDF
            # consolidation. Keep the same transport without a dummy image.
            result = runtime._extract_group(  # noqa: SLF001
                [],
                prompt_text,
                int(settings.get("dpi", 180)),
                bool(settings.get("cleanup_rendered_images", True)),
                True,
            )
            metadata = result if isinstance(result, dict) else None
            break
        except Exception as exc:
            write_json(
                attempt_dir / "error.json",
                {
                    "tag": tag,
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            if is_copilot_rate_limit_error(exc):
                raise
            if attempt >= attempts:
                raise
            _refresh_accounts_after_503(exc, account)
            time.sleep(retry_delay * attempt)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("parsed"), dict):
        raise RuntimeError("Copilot did not return parsed JSON for structured review.")

    parsed = metadata["parsed"]
    write_json(run_dir / f"{tag}_parsed.json", parsed)
    write_json(run_dir / f"{tag}_metadata.json", metadata)
    (run_dir / f"{tag}_raw_response.txt").write_text(
        str(metadata.get("raw_response") or ""), encoding="utf-8"
    )
    return parsed


def run_copilot_page_pair_extraction(
    current_pdf: Path,
    prior_pdf: Path,
    prompt_file: Path,
    run_dir: Path,
    settings: dict[str, Any],
    *,
    current_page: int,
    prior_page: int,
    tag: str,
) -> dict[str, Any]:
    """Send one current page and one prior page together for visual comparison."""

    account = load_copilot_account(settings)
    _ensure_copilot_import_path(account.optimda_dir)
    from copilot_runtime.runtime import CopilotRuntime  # type: ignore

    prompt_text = (
        load_prompt(prompt_file).strip()
        + f"\n\nThe first image is CURRENT physical PDF page {current_page}. "
        + f"The second image is PRIOR-YEAR physical PDF page {prior_page}. "
        + f"Return current_page: {current_page} and prior_page: {prior_page}."
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    attempts = max(1, int(settings.get("copilot_retries", 0)) + 1)
    retry_delay = float(settings.get("retry_delay_seconds", 10))
    metadata: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        attempt_dir = run_dir / f"attempt_{attempt}"
        runtime = CopilotRuntime(
            profile_dir=account.profile_dir,
            session_dir=account.session_dir,
            output_dir=attempt_dir,
            timeout_seconds=int(settings.get("timeout_seconds", 300)),
            cleanup=bool(settings.get("cleanup_rendered_images", True)),
            save_private_frames=False,
        )
        try:
            result = runtime.extract(
                input_path=[current_pdf.resolve(), prior_pdf.resolve()],
                page=[current_page, prior_page],
                prompt=prompt_text,
                dpi=int(settings.get("dpi", 180)),
                combine_inputs=True,
                return_metadata=True,
            )
            metadata = result if isinstance(result, dict) else None
            break
        except Exception as exc:
            write_json(
                attempt_dir / "error.json",
                {
                    "tag": tag,
                    "current_page": current_page,
                    "prior_page": prior_page,
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            if is_copilot_rate_limit_error(exc):
                raise
            if attempt >= attempts:
                raise
            _refresh_accounts_after_503(exc, account)
            time.sleep(retry_delay * attempt)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("parsed"), dict):
        raise RuntimeError("Copilot did not return a parsed JSON object for page comparison.")
    parsed = metadata["parsed"]
    write_json(run_dir / f"{tag}_parsed.json", parsed)
    write_json(run_dir / f"{tag}_metadata.json", metadata)
    (run_dir / f"{tag}_raw_response.txt").write_text(
        str(metadata.get("raw_response") or ""), encoding="utf-8"
    )
    return parsed


def run_copilot_page_group_extraction(
    current_pdf: Path,
    prior_pdf: Path,
    prompt_file: Path,
    run_dir: Path,
    settings: dict[str, Any],
    *,
    current_pages: list[int],
    prior_pages: list[int],
    tag: str,
) -> dict[str, Any]:
    """Send a complete current/prior section as an ordered image group."""

    if not current_pages or not prior_pages:
        raise ValueError("Both current_pages and prior_pages are required.")
    account = load_copilot_account(settings)
    _ensure_copilot_import_path(account.optimda_dir)
    from copilot_runtime.runtime import CopilotRuntime  # type: ignore

    paths = [current_pdf.resolve()] * len(current_pages) + [prior_pdf.resolve()] * len(
        prior_pages
    )
    pages = current_pages + prior_pages
    image_labels = [
        *(f"image {index + 1}=CURRENT physical page {page}" for index, page in enumerate(current_pages)),
        *(
            f"image {len(current_pages) + index + 1}=PRIOR physical page {page}"
            for index, page in enumerate(prior_pages)
        ),
    ]
    prompt_text = (
        load_prompt(prompt_file).strip()
        + "\n\nAttached image order: "
        + "; ".join(image_labels)
        + f". Return current_pages: {current_pages} and prior_pages: {prior_pages}."
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    attempts = max(1, int(settings.get("copilot_retries", 0)) + 1)
    retry_delay = float(settings.get("retry_delay_seconds", 10))
    metadata: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        attempt_dir = run_dir / f"attempt_{attempt}"
        runtime = CopilotRuntime(
            profile_dir=account.profile_dir,
            session_dir=account.session_dir,
            output_dir=attempt_dir,
            timeout_seconds=int(settings.get("timeout_seconds", 300)),
            cleanup=bool(settings.get("cleanup_rendered_images", True)),
            save_private_frames=False,
        )
        try:
            result = runtime.extract(
                input_path=paths,
                page=pages,
                prompt=prompt_text,
                dpi=int(settings.get("dpi", 180)),
                combine_inputs=True,
                return_metadata=True,
            )
            metadata = result if isinstance(result, dict) else None
            break
        except Exception as exc:
            write_json(
                attempt_dir / "error.json",
                {
                    "tag": tag,
                    "current_pages": current_pages,
                    "prior_pages": prior_pages,
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            if is_copilot_rate_limit_error(exc):
                raise
            if attempt >= attempts:
                raise
            _refresh_accounts_after_503(exc, account)
            time.sleep(retry_delay * attempt)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("parsed"), dict):
        raise RuntimeError("Copilot did not return parsed JSON for section comparison.")
    parsed = metadata["parsed"]
    write_json(run_dir / f"{tag}_parsed.json", parsed)
    write_json(run_dir / f"{tag}_metadata.json", metadata)
    (run_dir / f"{tag}_raw_response.txt").write_text(
        str(metadata.get("raw_response") or ""), encoding="utf-8"
    )
    return parsed


def _as_lines(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("lines", "lignes", "fs_lines", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _as_entries(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("entries", "positions", "layout", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    for old, new in (
        ("\u00a0", ""),
        (" ", ""),
        ("EUR", ""),
        ("€", ""),
        (",", "."),
        ("(", ""),
        (")", ""),
    ):
        text = text.replace(old, new)
    try:
        number = float(text)
    except ValueError:
        return None
    if negative:
        number = -abs(number)
    return number if math.isfinite(number) else None


def _fold_label(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def _repair_mojibake(value: Any) -> str:
    text = str(value or "")
    markers = ("Ã", "Â", "â€", "â€™", "â€œ", "â€", "â‚¬")
    for _ in range(2):
        if not any(marker in text for marker in markers):
            break
        try:
            repaired = text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if repaired == text:
            break
        text = repaired
    return text


def _is_reference_note_label(value: Any) -> bool:
    raw = str(value or "").strip()
    folded = _fold_label(raw)
    return (
        bool(re.match(r"^\(?\d+\)", raw))
        or raw.startswith("*")
        or raw.startswith("-")
        or folded.startswith("dont ")
        or folded.startswith("y compris")
        or "redevances de credit bail" in folded
        or "concernant les entites liees" in folded
    )


def normalize_fs_extraction(
    parsed: Any,
    *,
    source: str,
    override_page: int | None = None,
) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    for item in _as_lines(parsed):
        label = item.get("libelle") or item.get("label") or item.get("name")
        if not label:
            continue
        if _is_reference_note_label(label):
            continue
        lines.append(
            {
                "statement": item.get("statement") or item.get("etat"),
                "scope": item.get("scope")
                or (parsed.get("scope") if isinstance(parsed, dict) else None),
                "statement_set_id": item.get("statement_set_id")
                or item.get("set_id"),
                "entity": item.get("entity")
                or (parsed.get("entity") if isinstance(parsed, dict) else None),
                "page": override_page if override_page is not None else item.get("page"),
                "libelle": str(label).strip(),
                "brut": _number(item.get("brut", item.get("gross"))),
                "amortissement": _number(
                    item.get("amortissement", item.get("depreciation"))
                ),
                "montant_n": _number(item.get("montant_n", item.get("amount_n"))),
                "montant_n_1": _number(
                    item.get("montant_n_1", item.get("amount_n_1"))
                ),
                "is_total": bool(item.get("is_total", item.get("summary", False))),
                "notes": item.get("notes") or item.get("evidence") or "",
            }
        )
    return {
        "source": source,
        "extraction_method": "Copilot vision over rendered PDF page images",
        "lines": lines,
        "quality_notes": parsed.get("quality_notes", []) if isinstance(parsed, dict) else [],
    }


def _page_rects(pdf_path: Path) -> dict[int, tuple[float, float]]:
    import fitz

    document = fitz.open(pdf_path)
    try:
        return {
            index + 1: (float(page.rect.width), float(page.rect.height))
            for index, page in enumerate(document)
        }
    finally:
        document.close()


def _coordinate(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    return float(number)


def _bbox_values(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, dict):
        raw = (
            value.get("x1", value.get("x0", value.get("left"))),
            value.get("y1", value.get("y0", value.get("top"))),
            value.get("x2", value.get("right")),
            value.get("y2", value.get("bottom")),
        )
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        raw = (value[0], value[1], value[2], value[3])
    else:
        return None
    coords = tuple(_coordinate(item) for item in raw)
    if any(item is None for item in coords):
        return None
    x1, y1, x2, y2 = (float(item) for item in coords if item is not None)
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return x1, y1, x2, y2


def _amount_bbox(item: dict[str, Any]) -> tuple[float, float, float, float, bool] | None:
    for key in (
        "amount_bbox_norm",
        "amount_box_norm",
        "bbox_norm",
        "amount_bbox",
        "amount_box",
        "bbox",
    ):
        bbox = _bbox_values(item.get(key))
        if bbox is None:
            continue
        is_normalized = key.endswith("_norm") or all(0 <= value <= 1 for value in bbox)
        return (*bbox, is_normalized)
    return None


def _field_key(value: Any, display_column: Any = None) -> str:
    text = " ".join(str(item or "") for item in (value, display_column)).strip()
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()
    folded = folded.replace("_", " ").replace("-", " ")
    if "brut" in folded or "gross" in folded:
        return "brut"
    if "amort" in folded or "deprec" in folded:
        return "amortissement"
    if "n 1" in folded or "n-1" in text.lower() or "compar" in folded:
        return "montant_n_1"
    if "net" in folded or "total" in folded or "montant" in folded or folded == "n":
        return "montant_n"
    raw = str(value or "").strip()
    if raw in {"brut", "amortissement", "montant_n", "montant_n_1"}:
        return raw
    return raw


def normalize_layout(
    parsed: Any,
    *,
    pdf_path: Path,
    source: str,
    override_page: int | None = None,
) -> dict[str, Any]:
    rects = _page_rects(pdf_path)
    entries: list[dict[str, Any]] = []
    for item in _as_entries(parsed):
        label = item.get("libelle") or item.get("label") or item.get("name")
        if _is_reference_note_label(label):
            continue
        display_column = item.get("display_column") or item.get("column") or item.get("colonne")
        field = _field_key(item.get("field"), display_column)
        page = override_page if override_page is not None else item.get("page")
        if not label or not field or page is None:
            continue
        try:
            page_number = int(page)
        except (TypeError, ValueError):
            continue
        width, height = rects.get(page_number, (None, None))
        x = _coordinate(item.get("x"))
        y = _coordinate(item.get("y"))
        x_norm = _coordinate(item.get("x_norm"))
        y_norm = _coordinate(item.get("y_norm"))
        bbox = _amount_bbox(item)
        if bbox is not None and width and height:
            x1, y1, x2, y2, is_normalized = bbox
            if is_normalized:
                x1 *= width
                x2 *= width
                y1 *= height
                y2 *= height
            # Tickmarks are placed just to the right of the visible amount and
            # vertically centered on the amount glyphs.
            x = min(float(width) - 10.0, x2 + 6.0)
            y = (y1 + y2) / 2.0
        if x is not None and 0 <= x <= 1 and width:
            x_norm = x
            x = None
        if y is not None and 0 <= y <= 1 and height:
            y_norm = y
            y = None
        if x is None and x_norm is not None and width:
            x = x_norm * width
        if y is None and y_norm is not None and height:
            y = y_norm * height
        if x is None or y is None:
            continue
        entries.append(
            {
                "statement": item.get("statement") or item.get("etat"),
                "scope": item.get("scope"),
                "statement_set_id": item.get("statement_set_id")
                or item.get("set_id"),
                "page": page_number,
                "libelle": str(label).strip(),
                "field": str(field).strip(),
                "display_column": display_column,
                "amount": _number(item.get("amount")),
                "x": round(float(x), 2),
                "y": round(float(y), 2),
                "notes": item.get("notes") or item.get("evidence") or "",
            }
        )
    return {
        "coordinate_system": "PDF points, origin at top-left",
        "source": source,
        "entries": entries,
        "quality_notes": parsed.get("quality_notes", []) if isinstance(parsed, dict) else [],
    }


def merge_layouts(layouts: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    notes: list[Any] = []
    seen: set[tuple[Any, Any, Any, Any]] = set()
    for layout in layouts:
        for item in layout.get("entries", []):
            key = (
                item.get("page"),
                item.get("libelle"),
                item.get("field"),
                item.get("display_column"),
            )
            if key in seen:
                continue
            seen.add(key)
            entries.append(item)
        notes.extend(layout.get("quality_notes", []))
    return {
        "coordinate_system": "PDF points, origin at top-left",
        "source": source,
        "entries": entries,
        "quality_notes": notes,
    }


def normalize_document_index(
    parsed: Any,
    *,
    pdf_path: Path,
    source: str,
) -> dict[str, Any]:
    """Normalize Copilot's visual page index and make missing pages explicit."""

    page_count = len(_page_rects(pdf_path))
    raw_pages = parsed.get("pages", []) if isinstance(parsed, dict) else []
    by_page: dict[int, dict[str, Any]] = {}
    valid_scopes = {"annual", "consolidated", "auditor_report", "tax", "other"}
    valid_roles = {
        "cover",
        "contents",
        "primary_statement",
        "accounting_policies",
        "narrative_note",
        "annex_table",
        "auditor_report",
        "tax_form",
        "blank",
        "other",
    }
    for item in raw_pages:
        if not isinstance(item, dict):
            continue
        try:
            page_number = int(item.get("page"))
        except (TypeError, ValueError):
            continue
        if not 1 <= page_number <= page_count:
            continue
        scope = _fold_label(item.get("scope")).replace(" ", "_")
        role = _fold_label(item.get("page_role")).replace(" ", "_")
        headings = item.get("headings")
        by_page[page_number] = {
            "page": page_number,
            "scope": scope if scope in valid_scopes else "other",
            "page_role": role if role in valid_roles else "other",
            "primary_title": _repair_mojibake(item.get("primary_title")).strip()
            or None,
            "headings": [
                _repair_mojibake(value).strip()
                for value in headings
                if str(value).strip()
            ]
            if isinstance(headings, list)
            else [],
            "entity": _repair_mojibake(item.get("entity")).strip() or None,
            "period_end": item.get("period_end"),
            "statement_kind": item.get("statement_kind"),
            "reviewable": bool(item.get("reviewable", True)),
            "notes": _repair_mojibake(item.get("notes")).strip(),
        }

    pages = []
    missing = []
    for page_number in range(1, page_count + 1):
        if page_number in by_page:
            pages.append(by_page[page_number])
            continue
        missing.append(page_number)
        pages.append(
            {
                "page": page_number,
                "scope": "other",
                "page_role": "other",
                "primary_title": None,
                "headings": [],
                "entity": None,
                "period_end": None,
                "statement_kind": None,
                "reviewable": True,
                "notes": "Copilot did not return a descriptor for this physical page.",
            }
        )

    quality_notes = (
        list(parsed.get("quality_notes", [])) if isinstance(parsed, dict) else []
    )
    if missing:
        quality_notes.append(f"Missing Copilot descriptors for pages: {missing}")
    return {
        "document_type": "financial_report_index",
        "source": source,
        "entity": _repair_mojibake(parsed.get("entity"))
        if isinstance(parsed, dict)
        else None,
        "period_end": parsed.get("period_end") if isinstance(parsed, dict) else None,
        "pages": pages,
        "quality_notes": quality_notes,
    }
