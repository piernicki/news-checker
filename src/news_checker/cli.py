"""Command line: news-checker [--config PATH] <command>."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .app import make_reporters, run_cycle
from .config import Settings, load_settings
from .logsetup import setup_logging
from .pipeline import Pipeline, RunResult
from .reporters import Report
from .storage import Store, utcnow

log = logging.getLogger("news_checker")


def _csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _print_result(result: RunResult, delivered: list[str]) -> None:
    print(f"\nRun {result.kind}: {result.new_total} new link(s)")
    for name, s in result.stats.items():
        line = (f"  {name:<14} requests {s.requests:<4} results {s.results:<5} new {s.new:<4} "
                f"filtered {s.filtered:<4} errors {s.errors}")
        print(line if s.status == "ok" else f"  {name:<14} {s.status}" + ("" if not s.requests else f" | {line.strip()}"))
    c = getattr(result, "classify", None)
    if c is not None:
        counts = ", ".join(f"{k} {v}" for k, v in sorted(c.counts.items())) or "—"
        print(f"Classifier: {c.status}; {counts}; ${c.cost_usd:.4f}")
    for path in delivered:
        print(f"Report: {path}")
    if result.errors:
        print(f"Errors: {len(result.errors)} (first 5):")
        for e in result.errors[:5]:
            print(f"  - {e}")


def cmd_run(settings: Settings, args) -> int:
    result, delivered = run_cycle(settings, "daily", args.providers, report=not args.no_report)
    _print_result(result, delivered)
    return 0


def cmd_backfill(settings: Settings, args) -> int:
    if args.days:
        settings.backfill.days = args.days
    result, delivered = run_cycle(settings, "backfill", args.providers, report=not args.no_report)
    _print_result(result, delivered)
    return 0


def cmd_report(settings: Settings, args) -> int:
    """Rebuild an HTML report of links found since a date (or all links) without marking anything."""
    store = Store(settings.database_url)
    tz = ZoneInfo(settings.schedule.timezone)
    if args.all:
        mentions, name = store.all_mentions(), "manual-all"
    else:
        since = datetime.fromisoformat(args.since).replace(tzinfo=tz) if args.since else utcnow() - timedelta(days=1)
        mentions, name = store.seen_since(since), f"manual-{since:%Y-%m-%d}"
    report = Report(name=name, kind="manual", generated_at=utcnow(), mentions=mentions,
                    hide_no_match=settings.classifier.hide_no_match)
    for r in make_reporters(settings):
        print(r.send(report))
    return 0


def cmd_classify(settings: Settings, args) -> int:
    """Classify links without a verdict (including reported ones); --force re-classifies everything."""
    from .classify import classify_mentions
    from .filters import MentionFilter

    store = Store(settings.database_url)
    mentions = store.all_mentions() if args.force else store.unclassified(only_unreported=False)
    if args.limit:
        mentions = mentions[: args.limit]
    # Current filter rules first, without the model, so config changes also apply to already collected links.
    flt, by_rule, rest = MentionFilter(settings.filters), 0, []
    for m in mentions:
        reason = flt.reason(m.url, m.domain) or flt.text_reason(f"{m.title}\n{m.snippet}")
        if reason:
            store.set_verdict(m.id, verdict="no_match", reason=f"filter rule: {reason}", classifier="rules")
            by_rule += 1
        else:
            rest.append(m)
    print(f"Dropped by filter rules: {by_rule}")
    res = classify_mentions(settings, store, rest)
    counts = ", ".join(f"{k} {v}" for k, v in sorted(res.counts.items())) or "—"
    print(f"Classifier: {res.status}; links {len(rest)}; {counts}; errors {len(res.errors)}; ${res.cost_usd:.4f}")
    return 0 if not res.errors else 1


def cmd_send(settings: Settings, args) -> int:
    """Send a report (the latest by default) to every configured delivery channel."""
    from .app import deliver, make_deliveries

    store = Store(settings.database_url)
    if not make_deliveries(settings):
        print("No delivery channel configured (reports.telegram: enabled, bot_token, chat_id)", file=sys.stderr)
        return 2
    name = args.report or store.last_report_name()
    mentions = store.mentions_by_report(name) if name else []
    if not mentions:
        print(f"Report {name!r} not found", file=sys.stderr)
        return 1
    report = Report(name=name, kind="backfill" if name.startswith("backfill") else "daily", generated_at=utcnow(),
                    mentions=mentions, hide_no_match=settings.classifier.hide_no_match)
    html_path = Path(make_reporters(settings)[0].send(report))  # fresh HTML with exactly this report's links
    for line in deliver(settings, store, report, html_path):
        print(line)
    return 0


def cmd_tg_bot_chats(settings: Settings, args) -> int:
    """Show the bot and the chats it has seen updates from (added to a group, messages), with their chat_id."""
    import httpx

    from .reporters.telegram_bot import TelegramBot, TelegramError

    token = settings.reports.telegram.bot_token
    if not token:
        print("TG_BOT_TOKEN is not set (reports.telegram.bot_token)", file=sys.stderr)
        return 2
    bot = TelegramBot(token, httpx.Client(timeout=30))
    try:
        me = bot.call("getMe")
        updates = bot.call("getUpdates", data={"allowed_updates": '["message","my_chat_member","channel_post"]'})
    except TelegramError as exc:
        print(f"Bot API error: {exc}", file=sys.stderr)
        return 1
    print(f"Bot: @{me.get('username')} ({me.get('first_name')})")
    chats = {}
    for u in updates:
        for key in ("message", "my_chat_member", "channel_post"):
            chat = (u.get(key) or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat
    if not chats:
        print("No chats yet: add the bot to a group (or post something there) and run the command again.")
    for cid, c in chats.items():
        print(f"  chat_id={cid}  {c.get('type')}  {c.get('title') or c.get('username') or c.get('first_name')}")
    return 0


def cmd_check(settings: Settings, args) -> int:
    from .app import make_deliveries
    from .classifiers import ClassifierNotConfigured
    from .classify import make_classifier

    store = Store(settings.database_url)
    pipeline = Pipeline(settings, store)
    providers, skipped = pipeline.build_providers()
    now = utcnow()
    print(f"news-checker {__version__}")
    print(f"Config: {settings.base_dir}  DB: {settings.database_url}  links stored: {store.count_mentions()}")
    print(f"Queries: {len(settings.queries)}")
    for p in providers:
        daily = len(pipeline.plan(p, "daily", now))
        backfill = len(pipeline.plan(p, "backfill", now)) if p.cfg.backfill else 0
        month = store.usage_get(p.name, now.strftime("%Y-%m"))
        print(f"  ✓ {p.name:<14} ({p.type}) calls: run {daily}, backfill {backfill}; "
              f"this month {month}/{p.cfg.monthly_limit or '∞'}")
    for name, why in skipped.items():
        print(f"  – {name:<14} {why}")
    channels = [d.channel for d in make_deliveries(settings)]
    print(f"Delivery: {', '.join(channels) if channels else '– not configured (reports.telegram)'}")
    try:
        c = make_classifier(settings, store)
        print(f"Classifier: ✓ {c.label}, batch {settings.classifier.batch_size}, "
              f"unclassified {len(store.unclassified(False))}")
    except ClassifierNotConfigured as exc:
        print(f"Classifier: – {exc}")
    pipeline.http.close()
    return 0


def cmd_serve(settings: Settings, args) -> int:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    tz = settings.schedule.timezone
    crons = args.cron or settings.schedule.cron

    def job():
        try:
            result, delivered = run_cycle(settings, "daily", args.providers)
            _print_result(result, delivered)
        except Exception:  # noqa: BLE001 — the scheduler must keep running
            log.exception("Run failed")

    scheduler = BlockingScheduler(timezone=tz)
    for expr in crons:
        scheduler.add_job(job, CronTrigger.from_crontab(expr, timezone=tz), max_instances=1, coalesce=True,
                          misfire_grace_time=3600)
        log.info("SCHEDULE cron=%r tz=%s", expr, tz)
    if settings.schedule.run_on_start:
        job()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="news-checker", description="Monitoring of online mentions")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default=os.environ.get("NEWS_CHECKER_CONFIG", "config.yaml"),
                        help="path to config.yaml (env NEWS_CHECKER_CONFIG)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def providers_arg(p):
        p.add_argument("--providers", type=_csv, help="only these providers, comma-separated")

    p = sub.add_parser("run", help="one regular run + report of new links")
    providers_arg(p)
    p.add_argument("--no-report", action="store_true", help="do not build or send a report")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("backfill", help="initial collection over backfill.days days")
    providers_arg(p)
    p.add_argument("--no-report", action="store_true", help="do not build or send a report")
    p.add_argument("--days", type=int, help="override backfill.days")
    p.set_defaults(func=cmd_backfill)
    p = sub.add_parser("report", help="rebuild HTML for links found since a date (marks nothing)")
    p.add_argument("--since", help="YYYY-MM-DD, default: last 24 hours")
    p.add_argument("--all", action="store_true", help="all stored links")
    p.set_defaults(func=cmd_report)
    p = sub.add_parser("classify", help="classify links without a verdict")
    p.add_argument("--force", action="store_true", help="re-classify all links (filter rules first, then the model)")
    p.add_argument("--limit", type=int, help="at most N links")
    p.set_defaults(func=cmd_classify)
    p = sub.add_parser("send", help="send a report (default: the latest) to Telegram")
    p.add_argument("--report", help="report name, e.g. 2026-11-16_0700")
    p.set_defaults(func=cmd_send)
    p = sub.add_parser("tg-bot-chats", help="show chat_id of the groups the bot was added to")
    p.set_defaults(func=cmd_tg_bot_chats)
    p = sub.add_parser("check", help="validate the config and show providers")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("serve", help="run on schedule.cron")
    p.add_argument("--cron", action="append", help="cron expression instead of schedule.cron (repeatable), for testing")
    providers_arg(p)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    setup_logging(None, args.verbose)  # console only until the config is loaded
    _load_dotenv(Path(args.config).resolve().parent / ".env")
    settings = load_settings(args.config)
    setup_logging(settings, args.verbose)
    log.info("CMD %s version=%s config=%s", args.command, __version__, Path(args.config).resolve())
    return args.func(settings, args)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (next to the config); existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    sys.exit(main())
