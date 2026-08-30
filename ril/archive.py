from __future__ import annotations

import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

from ril.models import Index
from ril.storage import load_index

INDEX_NAME = "index.json"
ARTICLES_DIR = "articles"

# Guards against a hostile or corrupt archive. All are far above any real
# library, so a legitimate backup never meets them.
_MAX_ENTRIES = 200_000
_MAX_TOTAL_UNCOMPRESSED = 4 * 1024 * 1024 * 1024
_MAX_INDEX_BYTES = 256 * 1024 * 1024
_COPY_CHUNK = 64 * 1024


class ArchiveError(RuntimeError):
    """An archive is missing, malformed, or unsafe to extract."""


@dataclass
class ArchiveSummary:
    """What a backup zip contains, checked before anything is written to disk."""

    article_count: int = 0
    file_count: int = 0
    missing_files: list[str] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    skipped_entries: list[str] = field(default_factory=list)


@dataclass
class RestoreResult:
    summary: ArchiveSummary
    replaced_articles: int
    snapshot_path: Optional[Path] = None
    dry_run: bool = False


def export_filename(prefix: str = "ril-export", now: Optional[datetime] = None) -> str:
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    return f"{prefix}-{timestamp}.zip"


def create_archive(data_folder: Path, zip_path: Path) -> Path:
    """Compress the whole data folder into `zip_path`, paths relative to the folder."""
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    # A destination inside the data folder would otherwise try to archive itself.
    target = zip_path.resolve()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(data_folder.rglob("*")):
            if not file.is_file() or file.is_symlink():
                continue
            if file.resolve() == target:
                continue
            zf.write(file, file.relative_to(data_folder))
    return zip_path


def _is_safe_article_filename(name: str) -> bool:
    """True if `name` is a bare filename that stays inside the articles folder.

    The index is attacker-controlled once we accept an arbitrary archive, and
    `filename` is later joined onto the data folder to read and delete files.
    """
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return PurePosixPath(name).name == name


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """Read one member, stopping if it expands past `limit`.

    Sizes in the zip header are attacker-declared, so the real byte count is
    what gets enforced here.
    """
    out = bytearray()
    with zf.open(info) as fh:
        while True:
            chunk = fh.read(_COPY_CHUNK)
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > limit:
                raise ArchiveError(
                    f"Entry {info.filename} is larger than the {limit} byte limit."
                )
    return bytes(out)


def _copy_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path, budget: int) -> int:
    """Stream one member to `dest`, failing if it eats more than `budget` bytes."""
    written = 0
    with zf.open(info) as src, dest.open("wb") as dst:
        while True:
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > budget:
                raise ArchiveError(
                    "Archive expands to more than "
                    f"{_MAX_TOTAL_UNCOMPRESSED} bytes; refusing to extract it."
                )
            dst.write(chunk)
    return written


def _iter_safe_members(zf: zipfile.ZipFile) -> Iterator[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Yield extractable members, rejecting path traversal and symlinks (zip slip)."""
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ArchiveError(f"Archive contains a symlink, which is not allowed: {name}")
        if path.is_absolute() or any(part in ("..", "") for part in path.parts):
            raise ArchiveError(f"Archive contains an unsafe path: {name}")
        yield info, path


def inspect_archive(zip_path: Path, label: Optional[str] = None) -> ArchiveSummary:
    """Validate a backup zip and describe its contents. Never touches the data folder.

    `label` names the archive in error messages; callers that work on a temporary
    copy pass the user-facing name instead of leaking the server-side path.
    """
    zip_path = Path(zip_path)
    name = label or str(zip_path)
    if not zip_path.is_file():
        raise ArchiveError(f"Not a file: {name}")
    if not zipfile.is_zipfile(zip_path):
        raise ArchiveError(f"Not a zip archive: {name}")

    summary = ArchiveSummary()
    try:
        _zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"Archive is corrupt: {exc}") from exc
    with _zf as zf:
        members = {str(path): info for info, path in _iter_safe_members(zf)}

        if len(members) > _MAX_ENTRIES:
            raise ArchiveError(f"Archive has more than {_MAX_ENTRIES} entries.")
        declared = sum(info.file_size for info in members.values())
        if declared > _MAX_TOTAL_UNCOMPRESSED:
            raise ArchiveError(
                f"Archive declares more than {_MAX_TOTAL_UNCOMPRESSED} bytes of content."
            )

        if INDEX_NAME not in members:
            raise ArchiveError(
                f"Archive has no {INDEX_NAME} — it is not a ril backup."
            )
        try:
            raw = _read_member(zf, members[INDEX_NAME], _MAX_INDEX_BYTES).decode("utf-8")
            index = Index.model_validate_json(raw)
        except ArchiveError:
            raise
        except Exception as exc:
            raise ArchiveError(f"{INDEX_NAME} in the archive is not readable: {exc}") from exc

    # `filename` is joined onto the data folder when an article is read or
    # deleted, so it must be a plain name — never a path out of the folder.
    unsafe = sorted(
        {a.filename for a in index.articles if not _is_safe_article_filename(a.filename)}
    )
    if unsafe:
        shown = ", ".join(unsafe[:3]) + ("…" if len(unsafe) > 3 else "")
        raise ArchiveError(
            f"{INDEX_NAME} lists {len(unsafe)} article filename(s) that are not plain "
            f"filenames and could escape the articles folder: {shown}"
        )

    article_files = {
        name for name in members if name.startswith(f"{ARTICLES_DIR}/") and name.endswith(".md")
    }
    summary.skipped_entries = sorted(
        name for name in members if name != INDEX_NAME and name not in article_files
    )
    summary.article_count = len(index.articles)
    summary.file_count = len(article_files)

    indexed = {f"{ARTICLES_DIR}/{a.filename}" for a in index.articles}
    summary.missing_files = sorted(
        a.filename for a in index.articles if f"{ARTICLES_DIR}/{a.filename}" not in article_files
    )
    summary.orphan_files = sorted(
        Path(name).name for name in article_files if name not in indexed
    )
    return summary


def restore_archive(
    data_folder: Path,
    zip_path: Path,
    dry_run: bool = False,
    make_snapshot: bool = True,
    label: Optional[str] = None,
) -> RestoreResult:
    """Replace the data folder with the archive contents.

    Destructive: every article not in the archive is removed. A snapshot zip of the
    current data is written next to the data folder first unless `make_snapshot` is
    False, so the operation can be undone by restoring that snapshot.
    """
    data_folder = Path(data_folder)
    summary = inspect_archive(zip_path, label)
    replaced = len(load_index(data_folder).articles)

    if dry_run:
        return RestoreResult(summary=summary, replaced_articles=replaced, dry_run=True)

    snapshot_path: Optional[Path] = None
    has_data = (data_folder / INDEX_NAME).exists() or (data_folder / ARTICLES_DIR).exists()
    if make_snapshot and has_data:
        snapshot_path = _write_snapshot(data_folder)

    staging = Path(tempfile.mkdtemp(prefix=".ril-restore-", dir=data_folder.parent))
    try:
        incoming = staging / "incoming"
        (incoming / ARTICLES_DIR).mkdir(parents=True)
        budget = _MAX_TOTAL_UNCOMPRESSED
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for info, path in _iter_safe_members(zf):
                    name = str(path)
                    if name != INDEX_NAME and not (
                        name.startswith(f"{ARTICLES_DIR}/") and name.endswith(".md")
                    ):
                        continue
                    destination = incoming / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    budget -= _copy_member(zf, info, destination, budget)
        except zipfile.BadZipFile as exc:
            # Damaged, truncated, or with headers that do not match the payload.
            # Nothing has been swapped in yet, so the library is untouched.
            raise ArchiveError(f"Archive is corrupt and was not restored: {exc}") from exc

        _swap_in(data_folder, incoming, staging / "previous")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return RestoreResult(
        summary=summary,
        replaced_articles=replaced,
        snapshot_path=snapshot_path,
    )


def _write_snapshot(data_folder: Path) -> Path:
    """Archive the current data next to the data folder, before it is overwritten."""
    name = export_filename("ril-pre-restore")
    try:
        return create_archive(data_folder, data_folder.parent / name)
    except OSError as exc:
        raise ArchiveError(
            f"Could not write a safety snapshot next to {data_folder} ({exc}). "
            "Free up space or pass --no-snapshot to skip it."
        ) from exc


def _swap_in(data_folder: Path, incoming: Path, previous: Path) -> None:
    """Move `incoming` into place, keeping the old data aside until the swap succeeds."""
    previous.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    try:
        for name in (ARTICLES_DIR, INDEX_NAME):
            current = data_folder / name
            if current.exists():
                shutil.move(str(current), str(previous / name))
                moved.append((previous / name, current))
        for name in (ARTICLES_DIR, INDEX_NAME):
            source = incoming / name
            if source.exists():
                shutil.move(str(source), str(data_folder / name))
    except Exception:
        # Put the original data back so a failed restore leaves the folder usable.
        for name in (ARTICLES_DIR, INDEX_NAME):
            target = data_folder / name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink()
        for source, target in moved:
            if source.exists():
                shutil.move(str(source), str(target))
        raise
