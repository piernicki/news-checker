"""Logging: console + logs/news-checker.log rotated at midnight.

Event lines start with a tag that is easy to grep:
  RUN start|end      the whole run
  PROVIDER start|end|skip|fail
  REQ                one provider request: query, language, date window, results, new, duplicates, dropped, time
  NEW                a new link: provider, query, type, domain, publication date, URL, title
  SKIP               a dropped finding and the reason (domain, prefix, negative phrase, ...)
  DUP                a duplicate (only with level: DEBUG)
  CLASSIFY           filter verdicts and cost
  BUDGET             a spending limit was reached
  REPORT / DELIVERY  report written / sent
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .config import Settings

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S%z"
LOG_FILE = "news-checker.log"


def setup_logging(settings: Settings | None, verbose: bool = False) -> Path | None:
    level = logging.DEBUG if verbose else getattr(logging, (settings.logging.level if settings else "INFO").upper())
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    formatter = logging.Formatter(FORMAT, DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    path = None
    if settings and settings.logging.dir:
        directory = settings.resolve(settings.logging.dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / LOG_FILE
        file_handler = TimedRotatingFileHandler(
            path, when="midnight", backupCount=settings.logging.retention_days, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "anthropic", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path
