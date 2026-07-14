from __future__ import annotations

import re
from pathlib import Path

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._() -]+")


def safe_filename(name: str | None, fallback: str) -> str:
    raw = (name or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = _UNSAFE_CHARS.sub("_", raw).strip(" .")
    if not cleaned:
        cleaned = fallback
    return cleaned[:180]


def unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    index = 2
    while True:
        next_candidate = directory / f"{stem}_{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        index += 1
