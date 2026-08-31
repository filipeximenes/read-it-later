from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Bumped when the shape of index.json changes. An older file is migrated as it
# is parsed, so the version on disk only moves forward on the next write.
INDEX_VERSION = 2


def utcnow() -> datetime:
    """The current time, always timezone-aware.

    Every timestamp in the index is compared against timestamps written by
    another machine, so a naive one is not merely untidy: it cannot be
    compared with an aware one at all.
    """
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Read any timestamp as UTC, treating a naive one as UTC already.

    Older indexes were written with `datetime.utcnow()`, which produced naive
    values that were UTC in fact but not in type.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Article(BaseModel):
    id: str
    url: str
    title: str
    author: Optional[str] = None
    description: Optional[str] = None
    published_date: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    video_urls: list[str] = Field(default_factory=list)
    saved_at: datetime
    read: bool = False
    read_at: Optional[datetime] = None
    filename: str
    fetch_failed: bool = False

    # --- sync bookkeeping ---------------------------------------------------
    # Two clocks, because the two halves of a record change independently and
    # merge separately: the body and its metadata on one side, whether it has
    # been read on the other. Marking an article unread sets `read_at` back to
    # None, which is why that field cannot carry the ordering itself.
    content_updated_at: datetime
    state_updated_at: datetime
    # A tombstone. The row stays so that a delete can travel to the other side;
    # without one, a delete here looks exactly like an article the other side
    # has not sent yet, and comes back on the next sync.
    deleted: bool = False
    # When this side last decided the article exists or does not. Deleting and
    # saving again both move it, and it doubles as the time of the deletion.
    #
    # It cannot be folded into the content clock: a merge can take content from
    # one side and existence from the other, and a shared clock would then no
    # longer describe either. It is also the only timestamp for this, on
    # purpose — a separate `deleted_at` could arrive disagreeing with it, and
    # then two sides could merge the same pair and get different answers.
    existence_updated_at: datetime
    # Digest of the markdown body, so an unchanged body is never re-sent.
    body_sha256: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _backfill_sync_fields(cls, data: object) -> object:
        """Give a record written before sync existed the timestamps it lacks."""
        if not isinstance(data, dict):
            return data
        saved_at = data.get("saved_at")
        if saved_at is not None:
            if data.get("content_updated_at") is None:
                data["content_updated_at"] = saved_at
            if data.get("state_updated_at") is None:
                data["state_updated_at"] = data.get("read_at") or saved_at
            # `deleted_at` was the first shape of this field, before it was
            # clear that one clock had to serve for both.
            legacy_deleted_at = data.pop("deleted_at", None)
            if legacy_deleted_at is not None:
                data.setdefault("deleted", True)
            if data.get("existence_updated_at") is None:
                data["existence_updated_at"] = legacy_deleted_at or saved_at
        return data

    @field_validator(
        "saved_at",
        "read_at",
        "content_updated_at",
        "state_updated_at",
        "existence_updated_at",
    )
    @classmethod
    def _force_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        return None if value is None else as_utc(value)

    def mark_read(self, now: Optional[datetime] = None) -> None:
        moment = now or utcnow()
        self.read = True
        self.read_at = moment
        self.state_updated_at = moment

    def mark_unread(self, now: Optional[datetime] = None) -> None:
        self.read = False
        self.read_at = None
        self.state_updated_at = now or utcnow()

    def mark_deleted(self, now: Optional[datetime] = None) -> None:
        """Turn the record into a tombstone, keeping the row so it can travel."""
        self.deleted = True
        self.existence_updated_at = now or utcnow()

    def mark_present(self, now: Optional[datetime] = None) -> None:
        """Clear a tombstone, because the article was saved again."""
        self.deleted = False
        self.existence_updated_at = now or utcnow()

    @property
    def deleted_at(self) -> Optional[datetime]:
        """When this was deleted, or None if it still exists."""
        return self.existence_updated_at if self.deleted else None

    def touch_content(self, now: Optional[datetime] = None) -> None:
        """Record that the body or its metadata changed just now."""
        self.content_updated_at = now or utcnow()


class Index(BaseModel):
    version: int = INDEX_VERSION
    articles: list[Article] = Field(default_factory=list)

    @property
    def live(self) -> list[Article]:
        """Articles that still exist, with tombstones left out.

        Everything a person sees goes through here. Tombstones are for sync
        alone, and must never reach a list, a count, a statistic — or a
        lookup, which is why the two finders below start from this list.
        """
        return [a for a in self.articles if not a.deleted]

    def find_by_id(self, article_id: str) -> Optional[Article]:
        """A live article. A deleted one is not found, so it cannot be opened."""
        for article in self.live:
            if article.id == article_id:
                return article
        return None

    def find_by_url(self, url: str) -> Optional[Article]:
        """A live article. A deleted URL is not found, so it can be added again."""
        for article in self.live:
            if article.url == url:
                return article
        return None

    def by_id(self) -> dict[str, Article]:
        """Every record including tombstones, keyed by id. For sync only."""
        return {article.id: article for article in self.articles}
