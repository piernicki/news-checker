"""Exclusion filters: own resources, domains, regexes, ignored Telegram chats."""

from __future__ import annotations

import re

from .config import FiltersConfig
from .normalize import canonical_url, is_google_redirect, strip_scheme


class MentionFilter:
    def __init__(self, cfg: FiltersConfig):
        self.domains = {d.lower().removeprefix("www.").strip(".") for d in cfg.exclude_domains}
        self.prefixes = [  # case-insensitive: youtube.com/@Name == youtube.com/@name
            strip_scheme(canonical_url(p, cfg.tracking_params)).lower() for p in cfg.exclude_url_prefixes
        ]
        self.regexes = [re.compile(r, re.IGNORECASE) for r in cfg.exclude_url_regex]
        self.text_regexes = [re.compile(r, re.IGNORECASE) for r in cfg.exclude_text]
        self.tg_chats = {self.normalize_chat(c) for c in cfg.ignore_telegram_chats}

    @staticmethod
    def normalize_chat(chat: str | int) -> str:
        """'@Name' / 'https://t.me/Name' / '-100123' / 123 → 'name' / '123'."""
        value = str(chat).strip()
        value = value.split("t.me/", 1)[-1].lstrip("@").strip("/").lower()
        if value.startswith("-100") and value[4:].isdigit():
            value = value[4:]
        return value

    def is_ignored_chat(self, *identifiers: str | int | None) -> bool:
        return any(i is not None and self.normalize_chat(i) in self.tg_chats for i in identifiers)

    def text_reason(self, text: str, query_excludes: list[str] = ()) -> str | None:
        """Reason to drop by text (title + snippet), or None."""
        folded = (text or "").casefold()
        for phrase in query_excludes:
            if phrase.casefold() in folded:
                return f"query_exclude:{phrase}"
        for r in self.text_regexes:
            if r.search(text or ""):
                return f"exclude_text:{r.pattern}"
        return None

    def reason(self, url: str, host: str) -> str | None:
        """Reason to drop by URL (canonical), or None."""
        if is_google_redirect(url):
            return "google_redirect"
        if any(host == d or host.endswith("." + d) for d in self.domains):
            return "domain"
        bare = strip_scheme(url).lower()
        for prefix in self.prefixes:
            p = prefix.rstrip("/")
            if bare.rstrip("/") == p or bare.startswith(p + "/") or bare.startswith(p + "?"):
                return "prefix"
        if any(r.search(url) for r in self.regexes):
            return "regex"
        if host == "t.me" and self.tg_chats:
            segments = bare.split("/")
            chat = segments[2] if len(segments) > 2 and segments[1] == "c" else (segments[1] if len(segments) > 1 else "")
            if chat and self.normalize_chat(chat) in self.tg_chats:
                return "telegram_chat"
        return None
