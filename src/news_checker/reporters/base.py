from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from ..storage import StoredMention

GROUPS = ("web", "news", "telegram", "social")


@dataclass
class Report:
    name: str  # e.g. 2026-11-16_0700 or backfill-2026-11-15_2259
    kind: str  # daily | backfill | manual
    generated_at: datetime
    mentions: list[StoredMention]
    hide_no_match: bool = True

    @property
    def visible(self) -> list[StoredMention]:
        if not self.hide_no_match:
            return self.mentions
        return [m for m in self.mentions if m.verdict != "no_match"]

    @property
    def rejected(self) -> list[StoredMention]:
        return [m for m in self.mentions if m.verdict == "no_match"] if self.hide_no_match else []

    def groups(self) -> list[dict]:
        """Visible mentions grouped by source type, newest first."""
        def sort_key(m: StoredMention):
            return (m.published_at or m.first_seen_at).timestamp()

        out = []
        for key in GROUPS:
            items = sorted((m for m in self.visible if m.source_type == key), key=sort_key, reverse=True)
            if items:
                out.append({"key": key, "mentions": items})
        return out


class Reporter(ABC):
    @abstractmethod
    def send(self, report: Report) -> str:
        """Deliver the report and return a description of the result (path, message id, ...)."""
