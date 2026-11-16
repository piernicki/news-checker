from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .config import QueryConfig

SourceType = Literal["web", "news", "telegram", "social"]


@dataclass(frozen=True)
class SearchRequest:
    """One provider call: query × language × date window. query and lang are None for query-less providers."""

    query: QueryConfig | None
    lang: str | None
    since: datetime | None
    until: datetime | None

    @property
    def query_id(self) -> str:
        return self.query.id if self.query else ""


@dataclass
class Mention:
    url: str
    title: str = ""
    snippet: str = ""
    published_at: datetime | None = None
    source_type: SourceType | None = None  # None: derive from the domain
    provider: str = ""
    query_id: str = ""
