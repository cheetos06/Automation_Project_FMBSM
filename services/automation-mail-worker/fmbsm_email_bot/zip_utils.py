from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from .files import safe_filename, unique_path

ARCHIVE_SUFFIXES = {".zip", ".7z"}
PDF_SUFFIX = ".pdf"


@dataclass
class _ArchiveState:
    max_extracted_bytes: int
    max_files: int
    max_depth: int
    allowed_suffixes: frozenset[str]
    total_extracted_bytes: int = 0
    total_files_seen: int = 0

    def count_file(self, *, size: int, filename: str) -> None:
        self.total_files_seen += 1
        if self.total_files_seen > self.max_files:
            raise ValueError(f"Archive has too many files: {self.total_files_seen} > {self.max_files}")

        self.total_extracted_bytes += max(size, 0)
        if self.total_extracted_bytes > self.max_extracted_bytes:
            raise ValueError(
                f"Archive extracted size exceeds limit at {filename}: "
                f"{self.total_extracted_bytes} > {self.max_extracted_bytes}"
            )


def safe_extract_pdfs(
    archive_path: Path,
    destination: Path,
    *,
    max_extracted_bytes: int,
    max_files: int,
    max_depth: int = 5,
) -> list[Path]:
    """Extract PDFs from ZIP/7Z archives, including nested archives, without path traversal."""
    return safe_extract_files(
        archive_path,
        destination,
        allowed_suffixes={PDF_SUFFIX},
        max_extracted_bytes=max_extracted_bytes,
        max_files=max_files,
        max_depth=max_depth,
    )


def safe_extract_files(
    archive_path: Path,
    destination: Path,
    *,
    allowed_suffixes: set[str] | frozenset[str],
    max_extracted_bytes: int,
    max_files: int,
    max_depth: int = 5,
) -> list[Path]:
    """Safely extract only explicitly allowed file types, including nested archives."""

    normalized_suffixes = frozenset(
        suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
        for suffix in allowed_suffixes
    )
    if not normalized_suffixes:
        raise ValueError("At least one allowed archive suffix is required")
    destination.mkdir(parents=True, exist_ok=True)
    state = _ArchiveState(
        max_extracted_bytes=max_extracted_bytes,
        max_files=max_files,
        max_depth=max_depth,
        allowed_suffixes=normalized_suffixes,
    )
    return _extract_archive(archive_path, destination, state, depth=0)


def _extract_archive(archive_path: Path, destination: Path, state: _ArchiveState, *, depth: int) -> list[Path]:
    if depth > state.max_depth:
        raise ValueError(f"Nested archive depth exceeds limit: {depth} > {state.max_depth}")

    suffix = archive_path.suffix.lower()
    if suffix == ".zip":
        return _extract_zip(archive_path, destination, state, depth=depth)
    if suffix == ".7z":
        return _extract_7z(archive_path, destination, state, depth=depth)
    raise ValueError(f"Unsupported archive type: {archive_path.name}")


def _extract_zip(zip_path: Path, destination: Path, state: _ArchiveState, *, depth: int) -> list[Path]:
    extracted: list[Path] = []

    try:
        with ZipFile(zip_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.flag_bits & 0x1:
                    raise ValueError(f"Encrypted ZIP entries are not supported: {info.filename}")

                safe_relative = _safe_member_path(info.filename, archive_label="ZIP")
                state.count_file(size=info.file_size, filename=info.filename)
                suffix = safe_relative.suffix.lower()
                if suffix not in {*state.allowed_suffixes, *ARCHIVE_SUFFIXES}:
                    continue

                if suffix in state.allowed_suffixes:
                    output_path = _safe_output_path(destination, safe_relative)
                    with archive.open(info) as source, output_path.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    extracted.append(output_path)
                    continue

                nested_archive = _safe_output_path(destination / "_nested_archives", safe_relative)
                with archive.open(info) as source, nested_archive.open("wb") as target:
                    shutil.copyfileobj(source, target)
                nested_destination = destination / safe_relative.with_suffix("")
                extracted.extend(_extract_archive(nested_archive, nested_destination, state, depth=depth + 1))
    except BadZipFile as exc:
        raise ValueError(f"Invalid ZIP file: {zip_path.name}") from exc

    return extracted


def _extract_7z(seven_z_path: Path, destination: Path, state: _ArchiveState, *, depth: int) -> list[Path]:
    try:
        import py7zr
    except ImportError as exc:
        raise RuntimeError("7Z support requires the py7zr package. Install requirements.txt again.") from exc

    extracted: list[Path] = []

    try:
        with py7zr.SevenZipFile(seven_z_path, mode="r") as archive:
            needs_password = getattr(archive, "needs_password", None)
            if callable(needs_password) and needs_password():
                raise ValueError(f"Encrypted 7Z archives are not supported: {seven_z_path.name}")

            selected: list[tuple[str, Path, int]] = []
            for info in archive.list():
                filename = getattr(info, "filename", "")
                if not filename or bool(getattr(info, "is_directory", False)):
                    continue

                safe_relative = _safe_member_path(filename, archive_label="7Z")
                size = int(getattr(info, "uncompressed", 0) or 0)
                state.count_file(size=size, filename=filename)
                if safe_relative.suffix.lower() in {*state.allowed_suffixes, *ARCHIVE_SUFFIXES}:
                    selected.append((filename, safe_relative, size))

            if not selected:
                return []

            with TemporaryDirectory() as tmp:
                staging = Path(tmp)
                archive.extract(path=staging, targets=[filename for filename, _, _ in selected])

                for filename, safe_relative, _size in selected:
                    source_path = _staged_member_path(staging, filename)
                    suffix = safe_relative.suffix.lower()
                    if suffix in state.allowed_suffixes:
                        output_path = _safe_output_path(destination, safe_relative)
                        shutil.copyfile(source_path, output_path)
                        extracted.append(output_path)
                        continue

                    nested_archive = _safe_output_path(destination / "_nested_archives", safe_relative)
                    shutil.copyfile(source_path, nested_archive)
                    nested_destination = destination / safe_relative.with_suffix("")
                    extracted.extend(_extract_archive(nested_archive, nested_destination, state, depth=depth + 1))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Invalid 7Z file: {seven_z_path.name}") from exc

    return extracted


def _safe_member_path(member_name: str, *, archive_label: str) -> Path:
    normalized = member_name.replace("\\", "/")
    member = PurePosixPath(normalized)

    if member.is_absolute():
        raise ValueError(f"Unsafe absolute {archive_label} path: {member_name}")
    if any(part in {"", ".", ".."} for part in member.parts):
        raise ValueError(f"Unsafe {archive_label} path traversal: {member_name}")

    safe_parts = [safe_filename(part, "part") for part in member.parts]
    return Path(*safe_parts)


def _staged_member_path(staging: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    member = PurePosixPath(normalized)
    source_path = staging.joinpath(*member.parts)
    _assert_inside_directory(source_path, staging)
    if not source_path.is_file():
        raise ValueError(f"Archive member was not extracted: {member_name}")
    return source_path


def _safe_output_path(destination: Path, safe_relative: Path) -> Path:
    output_path = destination / safe_relative
    _assert_inside_directory(output_path, destination)
    return unique_path(output_path.parent, output_path.name)


def _assert_inside_directory(path: Path, directory: Path) -> None:
    resolved_directory = directory.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_directory)
    except ValueError as exc:
        raise ValueError(f"Archive entry escapes extraction directory: {path}") from exc
