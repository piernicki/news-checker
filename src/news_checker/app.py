"""One cycle: collect → classify → report new links → deliver."""

from __future__ import annotations

import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from .classify import ClassifyResult, classify_mentions
from .config import Settings
from .pipeline import Pipeline, RunResult
from .reporters import HtmlFileReporter, Report, Reporter
from .storage import Store, utcnow

log = logging.getLogger(__name__)


def make_reporters(settings: Settings) -> list[Reporter]:
    return [HtmlFileReporter(settings.reports_dir, settings.report_title, settings.schedule.timezone,
                             settings.reports.language)]


def make_deliveries(settings: Settings) -> list:
    """Delivery channels that are fully configured."""
    from .reporters.telegram_bot import TelegramBotReporter

    out = []
    tg = settings.reports.telegram
    if tg.enabled and tg.bot_token and tg.chat_id:
        out.append(TelegramBotReporter(tg, settings.report_title, settings.schedule.timezone,
                                       settings.reports.language))
    elif tg.enabled:
        log.warning("DELIVERY skip channel=telegram reason='bot_token or chat_id is not set'")
    return out


def deliver(settings: Settings, store: Store, report: Report, html_path: Path | None) -> list[str]:
    results = []
    for d in make_deliveries(settings):
        try:
            info = d.deliver(report, html_path)
            store.record_delivery(report.name, d.channel, status="sent", kind=report.kind,
                                  file_path=str(html_path) if html_path else None)
            log.info("DELIVERY ok channel=%s report=%s links=%d %s", d.channel, report.name, len(report.mentions), info)
            results.append(f"{d.channel}: {info}")
        except Exception as exc:  # noqa: BLE001 — the report stays undelivered and is retried next run
            store.record_delivery(report.name, d.channel, status="failed", kind=report.kind,
                                  file_path=str(html_path) if html_path else None, error=str(exc))
            log.warning("DELIVERY fail channel=%s report=%s error=%r", d.channel, report.name, str(exc))
            results.append(f"{d.channel}: error {exc}")
    return results


def retry_failed_deliveries(settings: Settings, store: Store) -> None:
    max_attempts = settings.reports.telegram.max_attempts
    for d in make_deliveries(settings):
        for row in store.failed_deliveries(d.channel, max_attempts):
            mentions = store.mentions_by_report(row["report_name"])
            report = Report(name=row["report_name"], kind=row["kind"], generated_at=utcnow(), mentions=mentions,
                            hide_no_match=settings.classifier.hide_no_match)
            path = Path(row["file_path"]) if row.get("file_path") else None
            log.info("DELIVERY retry channel=%s report=%s attempt=%d", d.channel, row["report_name"], row["attempts"] + 1)
            try:
                info = d.deliver(report, path)
                store.record_delivery(report.name, d.channel, status="sent", kind=report.kind)
                log.info("DELIVERY ok channel=%s report=%s (retry) %s", d.channel, report.name, info)
            except Exception as exc:  # noqa: BLE001
                store.record_delivery(report.name, d.channel, status="failed", kind=report.kind, error=str(exc))
                log.warning("DELIVERY fail channel=%s report=%s error=%r", d.channel, report.name, str(exc))


def report_unreported(settings: Settings, store: Store, kind: str) -> list[str]:
    mentions = store.unreported()
    if not mentions:
        log.info("REPORT none reason='no new links'")
        return []
    now = utcnow()
    local = now.astimezone(ZoneInfo(settings.schedule.timezone))
    stamp = f"{local:%Y-%m-%d_%H%M}"
    name = f"backfill-{stamp}" if kind == "backfill" else stamp
    report = Report(name=name, kind=kind, generated_at=now, mentions=mentions,
                    hide_no_match=settings.classifier.hide_no_match)
    delivered = [r.send(report) for r in make_reporters(settings)]
    store.mark_reported([m.id for m in mentions], name)
    for d in delivered:
        log.info("REPORT file=%s links=%d kind=%s", d, len(mentions), kind)
    delivered += deliver(settings, store, report, Path(delivered[0]) if delivered else None)
    return delivered


def run_cycle(settings: Settings, kind: str = "daily", only: list[str] | None = None,
              report: bool = True) -> tuple[RunResult, list[str]]:
    store = Store(settings.database_url)
    pipeline = Pipeline(settings, store)
    try:
        result = pipeline.collect(kind, only)
    finally:
        pipeline.http.close()
    result.classify = classify_pending(settings, store)
    if report:
        retry_failed_deliveries(settings, store)
    delivered = report_unreported(settings, store, kind) if report else []
    return result, delivered


def classify_pending(settings: Settings, store: Store) -> ClassifyResult:
    """Classify new (not yet reported) links. Skipped without a key or when the classifier is disabled."""
    return classify_mentions(settings, store, store.unclassified(only_unreported=True))
