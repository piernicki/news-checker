"""Mentions inside public Telegram channels via the web preview t.me/s/<channel>?q=<term>.

No account or API key: the same page a browser sees. Returns exact post links https://t.me/<channel>/<id>
with text and date.

Channels searched:
- `channels` from the config;
- channels discovered by other providers (any t.me link in search results); the pipeline stores them in the
  tg_channels table, which is why this provider runs last.
A channel that has never been searched is searched through its full history (up to max_pages pages per term),
later only within the run window.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from ..filters import MentionFilter
from ..models import Mention, SearchRequest
from . import register
from ._util import parse_date, snippet_around
from .base import Provider, ProviderNotConfigured

log = logging.getLogger(__name__)

BASE = "https://t.me/s/"
_PREV_RE = re.compile(r'<link rel="prev" href="([^"]+)"')


@register("telegram_web")
class TelegramWebProvider(Provider):
    source_type = "telegram"
    uses_queries = False
    supports_date_range = False  # one window per run: history is covered by the first search of a channel
    runs_last = True

    def setup(self) -> None:
        self.terms: list[str] = [t for t in (self.opt("terms") or []) if str(t).strip()]
        if not self.terms:
            raise ProviderNotConfigured(f"{self.name}: 'terms' is empty")
        self.channels = [MentionFilter.normalize_chat(c) for c in (self.opt("channels") or [])]
        self.max_pages = int(self.opt("max_pages", 10))
        self.store = self.budget.store
        self.filter = MentionFilter(self.settings.filters)
        if self.cfg.model_extra is not None:
            self.cfg.model_extra.setdefault("min_interval_seconds", 1.0)

    def _excluded(self, chan: str) -> bool:
        return self.filter.is_ignored_chat(chan) or self.filter.reason(f"https://t.me/{chan}/1", "t.me") is not None

    def search(self, req: SearchRequest) -> list[Mention]:
        known = self.store.list_tg_channels()
        for chan in self.channels:
            if chan not in known:
                self.store.add_tg_channel(chan, "config")
                known[chan] = None
        out: dict[str, Mention] = {}
        for chan, last_searched in sorted(known.items()):
            if self._excluded(chan):
                continue
            full_history = last_searched is None
            ok = True
            for term in self.terms:
                try:
                    for m in self._search_channel(chan, term, None if full_history else req.since, req.until):
                        out.setdefault(m.url, m)
                except Exception as exc:  # noqa: BLE001 — one channel must not break the others
                    ok = False
                    log.warning("TGWEB error channel=%s term=%r error=%r", chan, term, str(exc))
            if ok:
                self.store.mark_tg_searched(chan)
            log.info("TGWEB channel=%s full_history=%s found_total=%d", chan, full_history, len(out))
        return list(out.values())

    def _snippet(self, node, text: str, term: str) -> str:
        """Text around the term plus post links containing a search term: a mention is often only a hyperlink
        whose address is not part of the visible text."""
        snippet = snippet_around(text, term)
        hrefs = [a.attributes.get("href") or "" for a in
                 node.css("div.tgme_widget_message_text a, a.tgme_widget_message_link_preview")]
        hits = []
        for href in hrefs:
            if href and href not in hits and any(t.lower() in href.lower() for t in self.terms):
                hits.append(href)
        return snippet + (" [links in post: " + ", ".join(hits[:3]) + "]" if hits else "")

    def _search_channel(self, chan: str, term: str, since: datetime | None, until: datetime | None) -> list[Mention]:
        from selectolax.lexbor import LexborHTMLParser

        url: str | None = BASE + chan
        params: dict | None = {"q": term}
        out: list[Mention] = []
        for _ in range(self.max_pages):
            self.charge()
            resp = self.fetch("GET", url, params=params)
            if not urlsplit(str(resp.url)).path.startswith("/s/"):
                log.info("TGWEB skip channel=%s reason='no public preview'", chan)
                return out
            html = resp.text
            tree = LexborHTMLParser(html)
            title_node = tree.css_first(".tgme_channel_info_header_title")
            title = title_node.text(strip=True) if title_node else chan
            older = False
            for node in tree.css("div.tgme_widget_message[data-post]"):
                post = node.attributes.get("data-post") or ""
                if "/" not in post:
                    continue
                time_node = node.css_first("time[datetime]")
                date = parse_date(time_node.attributes.get("datetime")) if time_node else None
                if date and since and date < since:
                    older = True
                    continue
                if date and until and date > until:
                    continue
                text_node = node.css_first("div.tgme_widget_message_text")
                text = text_node.text(separator=" ") if text_node else ""
                out.append(Mention(
                    url=f"https://t.me/{post}", title=title, snippet=self._snippet(node, text, term),
                    published_at=date, source_type="telegram", query_id=f"tg-web:{term}",
                ))
            prev = _PREV_RE.search(html)
            if older or not prev:
                break
            url, params = urljoin("https://t.me", prev.group(1).replace("&amp;", "&")), None
        return out
