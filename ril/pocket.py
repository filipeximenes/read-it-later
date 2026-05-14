"""Pocket CSV export parsing (matches `part_*.csv` from Pocket)."""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ril.extractor import ExtractedArticle


def pocket_rows(csv_path: Path) -> Iterator[dict[str, str]]:
    """Yield each data row from a Pocket-export CSV."""
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row:
                continue
            out: dict[str, str] = {}
            for k, v in row.items():
                if not k:
                    continue
                out[k.strip()] = (v or "").strip()
            yield out


def parse_time_added(raw: str | None) -> tuple[datetime, bool]:
    """Return Pocket `time_added` as UTC, or UTC now when missing/invalid.

    Second tuple value is True when the fallback Instant was used.
    """
    if raw is None or not str(raw).strip():
        return datetime.now(timezone.utc), True
    try:
        ts = int(str(raw).strip())
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt, False
    except (ValueError, OSError, OverflowError):
        return datetime.now(timezone.utc), True


def status_to_read(status: str | None) -> bool:
    """Pocket uses `archive` for saved/archived items; treat as already read."""
    return (status or "").strip().casefold() == "archive"


def apply_pocket_title_fallback(
    pocket_title: str | None,
    url: str,
    extracted: ExtractedArticle,
) -> ExtractedArticle:
    """Use CSV title when fetch failed or the extractor only kept the raw URL."""
    csv_title = (pocket_title or "").strip()
    url_s = url.strip()
    title_s = (extracted.title or "").strip()
    use_csv_title = extracted.fetch_failed or title_s == url_s
    if use_csv_title and csv_title:
        return replace(extracted, title=csv_title)
    return extracted
