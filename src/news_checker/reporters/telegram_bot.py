"""Sending reports to a Telegram chat with a bot (Bot API).

The message holds a title and a summary, then "match" links and a separate "unclear" block (linked title, domain,
date). Long lists are split into messages of up to 4096 characters; links beyond max_links stay in the file only.
The full HTML report (with "no match" links collapsed) is attached as a document.
"""

from __future__ import annotations

import html
import logging
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from ..config import TelegramDeliveryConfig
from ..i18n import t
from ..storage import StoredMention
from .base import Report

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4096


class TelegramError(RuntimeError):
    pass


class TelegramBot:
    def __init__(self, token: str, http: httpx.Client):
        self.token, self.http = token, http

    def call(self, method: str, *, data: dict | None = None, files: dict | None = None) -> dict:
        resp = self.http.post(API.format(token=self.token, method=method), data=data, files=files)
        try:
            payload = resp.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {resp.status_code}") from None
        if not payload.get("ok"):
            raise TelegramError(f"{method}: {payload.get('error_code')} {payload.get('description')}")
        return payload["result"]


class TelegramBotReporter:
    channel = "telegram"

    def __init__(self, cfg: TelegramDeliveryConfig, title: str, timezone: str, language: str = "en",
                 http: httpx.Client | None = None):
        self.cfg, self.title, self.tz = cfg, title, ZoneInfo(timezone)
        self.t = partial(t, language)
        self.bot = TelegramBot(cfg.bot_token, http or httpx.Client(timeout=60))

    def _line(self, i: int, m: StoredMention) -> str:
        title = html.escape((m.title or m.domain).strip()[:150])
        date = (m.published_at or m.first_seen_at).astimezone(self.tz).strftime("%d.%m.%Y")
        return f'{i}. <a href="{html.escape(m.original_url, quote=True)}">{title}</a> — {html.escape(m.domain)}, {date}'

    def build_messages(self, report: Report) -> list[str]:
        matches = [m for m in report.mentions if m.verdict in ("match", None)]
        unclear = [m for m in report.mentions if m.verdict == "unclear"] if self.cfg.include_unclear else []
        rejected = sum(1 for m in report.mentions if m.verdict == "no_match")
        key = lambda m: (m.published_at or m.first_seen_at).timestamp()  # noqa: E731
        matches.sort(key=key, reverse=True)
        unclear.sort(key=key, reverse=True)

        day = report.generated_at.astimezone(self.tz).strftime("%d.%m.%Y")
        head = f"<b>{html.escape(self.title)}</b> — {day}"
        if report.kind == "backfill":
            head += self.t("backfill_suffix")
        stats = [self.t("tg.match", n=len(matches))]
        if self.cfg.include_unclear:
            stats.append(self.t("tg.unclear", n=len(unclear)))
        stats.append(self.t("tg.rejected", n=rejected))
        lines = [head, " · ".join(stats)]

        budget = self.cfg.max_links
        shown_matches, shown_unclear = matches[:budget], unclear[: max(0, budget - len(matches))]
        if shown_matches:
            lines += [""] + [self._line(i, m) for i, m in enumerate(shown_matches, 1)]
        if shown_unclear:
            lines += ["", f"<b>{self.t('tg.unclear_header')}</b>"]
            lines += [self._line(i, m) for i, m in enumerate(shown_unclear, 1)]
        hidden = len(matches) + len(unclear) - len(shown_matches) - len(shown_unclear)
        if hidden > 0:
            lines += ["", self.t("tg.more", n=hidden)]
        return _chunk(lines)

    def has_content(self, report: Report) -> bool:
        return any(m.verdict in ("match", None) or (self.cfg.include_unclear and m.verdict == "unclear")
                   for m in report.mentions)

    def deliver(self, report: Report, html_path: Path | None) -> str:
        if not self.has_content(report):
            return "nothing to send: only 'no match' links"
        messages = self.build_messages(report)
        for text in messages:
            self.bot.call("sendMessage", data={
                "chat_id": self.cfg.chat_id, "text": text, "parse_mode": "HTML",
                "link_preview_options": '{"is_disabled": true}',
            })
        attached = bool(self.cfg.attach_html and html_path and html_path.exists())
        if attached:
            with html_path.open("rb") as f:
                self.bot.call("sendDocument", data={"chat_id": self.cfg.chat_id, "caption": self.t("tg.caption")},
                              files={"document": (html_path.name, f, "text/html")})
        return f"{len(messages)} message(s)" + (" + file" if attached else "")


def _chunk(lines: list[str]) -> list[str]:
    """Join lines into messages within Telegram's length limit without splitting a line."""
    out, cur = [], ""
    for line in lines:
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > LIMIT and cur:
            out.append(cur)
            cur = line
        else:
            cur = candidate
    if cur.strip():
        out.append(cur)
    return out
