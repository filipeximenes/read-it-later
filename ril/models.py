from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


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

    def mark_read(self) -> None:
        self.read = True
        self.read_at = datetime.utcnow()

    def mark_unread(self) -> None:
        self.read = False
        self.read_at = None


class Index(BaseModel):
    version: int = 1
    articles: list[Article] = Field(default_factory=list)

    def find_by_id(self, article_id: str) -> Optional[Article]:
        for article in self.articles:
            if article.id == article_id:
                return article
        return None

    def find_by_url(self, url: str) -> Optional[Article]:
        for article in self.articles:
            if article.url == url:
                return article
        return None
