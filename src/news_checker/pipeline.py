"""Collection: providers → URL normalization → filters → deduplication → storage."""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .filters import MentionFilter
from .models import Mention, SearchRequest
from .normalize import canonical_url, classify, host_of
from .providers import REGISTRY, Budget, BudgetExceeded, Provider, ProviderNotConfigured
from .providers.base import short_error
from .storage import Store, utcnow

log = logging.getLogger(__name__)


@dataclass
class ProviderStats:
    requests: int = 0
    results: int = 0
    new: int = 0
    filtered: int = 0
    errors: int = 0
    status: str = "ok"


@dataclass
class RunResult:
    kind: str
    stats: dict[str, ProviderStats] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    classify: object | None = None  # classify.ClassifyResult

    @property
    def new_total(self) -> int:
        return sum(s.new for s in self.stats.values())


def make_http(settings: Settings) -> httpx.Client:
    return httpx.Client(
        timeout=settings.http_timeout, follow_redirects=True, headers={"User-Agent": settings.user_agent}
    )


class Pipeline:
    def __init__(self, settings: Settings, store: Store, http: httpx.Client | None = None):
        self.settings, self.store = settings, store
        self.http = http or make_http(settings)
        self.filter = MentionFilter(settings.filters)

    def build_providers(self, only: list[str] | None = None) -> tuple[list[Provider], dict[str, str]]:
        providers: list[Provider] = []
        skipped: dict[str, str] = {}
        for name, cfg in self.settings.providers.items():
            if only and name not in only:
                continue
            if not cfg.enabled:
                skipped[name] = "disabled"
                continue
            cls = REGISTRY.get(cfg.type)
            if cls is None:
                skipped[name] = f"unknown type '{cfg.type}'"
                continue
            try:
                budget = Budget(self.store, name, cfg.run_limit, cfg.monthly_limit)
                providers.append(cls(name, cfg, self.settings, self.http, budget))
            except ProviderNotConfigured as exc:
                skipped[name] = str(exc)
        return providers, skipped

    def plan(self, provider: Provider, kind: str, now: datetime) -> list[SearchRequest]:
        if kind == "backfill":
            since = now - timedelta(days=self.settings.backfill.days)
            if provider.supports_date_range:
                step = timedelta(days=max(1, provider.cfg.backfill_window_days))
                windows, start = [], since
                while start < now:
                    end = min(start + step, now)
                    windows.append((start, end))
                    start = end
                windows.reverse()  # newest first: they matter more if the budget runs out
            else:
                windows = [(since, now)]
        else:
            windows = [(now - timedelta(days=self.settings.daily.lookback_days), now)]

        if not provider.uses_queries:
            return [SearchRequest(None, None, s, u) for s, u in windows]

        supported = provider.langs()
        requests = []
        for i, (since, until) in enumerate(windows):
            for q in self.settings.queries:
                if q.providers is not None and provider.name not in q.providers:
                    continue
                if not q.date_window and i > 0:
                    continue  # a query without a date window runs once
                for lang in q.langs:
                    if supported is None or lang in supported:
                        requests.append(SearchRequest(q, lang, since, until) if q.date_window
                                        else SearchRequest(q, lang, None, None))
        return requests

    def collect(self, kind: str = "daily", only: list[str] | None = None) -> RunResult:
        result = RunResult(kind=kind)
        run_id = self.store.start_run(kind)
        started = time.monotonic()
        providers, skipped = self.build_providers(only)
        log.info("RUN start id=%s kind=%s providers=%s", run_id, kind, ",".join(p.name for p in providers) or "-")
        for name, why in skipped.items():
            result.stats[name] = ProviderStats(status=f"skipped: {why}")
            log.info("PROVIDER skip name=%s reason=%r", name, why)

        now = utcnow()
        providers.sort(key=lambda p: p.runs_last)
        for provider in providers:
            stats = result.stats.setdefault(provider.name, ProviderStats())
            if kind == "backfill" and not provider.cfg.backfill:
                stats.status = "skipped: not part of backfill"
                log.info("PROVIDER skip name=%s reason='not part of backfill'", provider.name)
                continue
            requests = self.plan(provider, kind, now)
            log.info("PROVIDER start name=%s type=%s requests=%d", provider.name, provider.type, len(requests))
            p_started = time.monotonic()
            try:
                t0 = time.monotonic()
                for req, outcome in provider.run(requests):
                    elapsed = time.monotonic() - t0
                    stats.requests += 1
                    where = (f"provider={provider.name} query={req.query_id or '-'} lang={req.lang or '-'} "
                             f"window={_window(req)}")
                    if isinstance(outcome, Exception):
                        stats.errors += 1
                        result.errors.append(f"{provider.name}/{req.query_id}/{req.lang}: {outcome}")
                        log.warning("REQ  %s error=%r sec=%.1f", where, str(outcome), elapsed)
                    else:
                        counts = Counter(self._store(provider, req, m, kind, stats) for m in outcome)
                        stats.results += len(outcome)
                        log.info("REQ  %s results=%d new=%d dup=%d filtered=%d sec=%.1f", where, len(outcome),
                                 counts["new"], counts["dup"], counts["filtered"], elapsed)
                    t0 = time.monotonic()
            except BudgetExceeded as exc:
                stats.status = str(exc)
                result.errors.append(str(exc))
                log.warning("BUDGET %s", exc)
            except Exception as exc:  # noqa: BLE001 — one provider must not break the run
                exc = short_error(exc)
                stats.status = f"error: {exc}"
                stats.errors += 1
                result.errors.append(f"{provider.name}: {exc}")
                log.exception("PROVIDER fail name=%s error=%r", provider.name, str(exc))
            finally:
                provider.close()
            log.info("PROVIDER end name=%s requests=%d results=%d new=%d filtered=%d errors=%d sec=%.0f",
                     provider.name, stats.requests, stats.results, stats.new, stats.filtered, stats.errors,
                     time.monotonic() - p_started)

        self.store.finish_run(run_id, {k: vars(v) for k, v in result.stats.items()}, result.errors)
        log.info("RUN end id=%s kind=%s new=%d errors=%d sec=%.0f", run_id, kind, result.new_total,
                 len(result.errors), time.monotonic() - started)
        return result

    def _track_tg_channel(self, original_url: str) -> bool:
        """Remember the channel of a t.me link for telegram_web. Returns True if the link is a channel feed."""
        chan, is_feed = _tg_channel_of(original_url)
        if chan and not self.filter.is_ignored_chat(chan) and self.filter.reason(f"https://t.me/{chan}/1", "t.me") is None:
            if self.store.add_tg_channel(chan, original_url):
                log.info("TGCHAN new channel=%s source=%s", chan, original_url)
        return is_feed

    def _store(self, provider: Provider, req: SearchRequest, m: Mention, kind: str, stats: ProviderStats) -> str:
        """Store a finding. Returns new | dup | filtered."""
        src = f"provider={provider.name} query={m.query_id or req.query_id or '-'}"
        if not m.url or not m.url.startswith(("http://", "https://")):
            stats.filtered += 1
            log.info("SKIP %s reason=bad_url url=%r", src, m.url)
            return "filtered"
        url = canonical_url(m.url, self.settings.filters.tracking_params)
        host = host_of(url)
        if host == "t.me" and self._track_tg_channel(m.url):
            # a channel feed (t.me/s/<channel>?before=N) opens the wrong post; the channel goes to telegram_web
            stats.filtered += 1
            log.info("SKIP %s reason=tg_feed url=%s", src, m.url)
            return "filtered"
        reason = self.filter.reason(url, host) or self.filter.text_reason(
            f"{m.title or ''}\n{m.snippet or ''}", req.query.exclude if req.query else []
        )
        if reason:
            stats.filtered += 1
            log.info("SKIP %s reason=%s url=%s title=%r", src, reason, m.url, _short(m.title))
            return "filtered"
        by_domain = classify(host)
        source_type = by_domain if by_domain != "web" else (m.source_type or provider.source_type or "web")
        is_new = self.store.upsert_mention(
            url=url, original_url=m.url, domain=host, source_type=source_type,
            title=(m.title or "").strip(), snippet=(m.snippet or "").strip(), published_at=m.published_at,
            provider=m.provider or provider.name, query_id=m.query_id or req.query_id, run_kind=kind,
        )
        if is_new:
            stats.new += 1
            published = f"{m.published_at:%Y-%m-%d}" if m.published_at else "-"
            log.info("NEW  %s type=%s domain=%s published=%s url=%s title=%r",
                     src, source_type, host, published, m.url, _short(m.title))
            return "new"
        log.debug("DUP  %s url=%s", src, url)
        return "dup"


_TG_RESERVED = {"s", "c", "joinchat", "addstickers", "addemoji", "share", "iv", "proxy", "socks", "login", "setlanguage"}


def _tg_channel_of(original_url: str) -> tuple[str | None, bool]:
    """(channel username, is it a t.me/s/… feed) of a t.me link."""
    parts = [p for p in urlsplit(original_url).path.split("/") if p]
    in_preview = bool(parts) and parts[0] == "s"
    if in_preview:
        parts = parts[1:]
    # t.me/s/<channel>/<id> is a single post preview; t.me/s/<channel>[?before=N] is the channel feed
    is_feed = in_preview and not (len(parts) > 1 and parts[1].isdigit())
    if not parts or parts[0].lower() in _TG_RESERVED or parts[0].startswith("+"):
        return None, is_feed
    name = parts[0].lstrip("@").lower()
    return (name if re.fullmatch(r"[a-z0-9_]{4,64}", name) else None), is_feed


def _window(req: SearchRequest) -> str:
    return f"{req.since:%Y-%m-%d}..{req.until:%Y-%m-%d}" if req.since and req.until else "-"


def _short(text: str | None, n: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"
