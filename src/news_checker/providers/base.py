"""Search provider interface.

To add a source: create a module in providers/, subclass Provider, decorate it with @register("type"), import it
in providers/__init__.py and use that type in config.yaml.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import ClassVar

import httpx

from ..config import ProviderConfig, Settings
from ..models import Mention, SearchRequest
from ..storage import Store, utcnow

log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    pass


class ProviderNotConfigured(Exception):
    """A key or required option is missing: the provider is skipped without an error."""


class Budget:
    def __init__(self, store: Store, provider: str, run_limit: int | None, monthly_limit: int | None):
        self.store, self.provider = store, provider
        self.run_limit, self.monthly_limit = run_limit, monthly_limit
        self.used_this_run = 0

    def charge(self, n: int = 1) -> None:
        period = utcnow().strftime("%Y-%m")
        if self.run_limit is not None and self.used_this_run + n > self.run_limit:
            raise BudgetExceeded(f"{self.provider}: run limit reached ({self.run_limit})")
        if self.monthly_limit is not None and self.store.usage_get(self.provider, period) + n > self.monthly_limit:
            raise BudgetExceeded(f"{self.provider}: monthly limit reached ({self.monthly_limit})")
        self.used_this_run += n
        self.store.usage_add(self.provider, period, n)


class Provider(ABC):
    type: ClassVar[str]
    source_type: ClassVar[str | None] = None  # None: derived from the link domain
    uses_queries: ClassVar[bool] = True  # False: the provider does not use `queries` from the config
    supports_date_range: ClassVar[bool] = False  # can search within [since, until]
    runs_last: ClassVar[bool] = False  # runs after the others (uses their findings)

    def __init__(self, name: str, cfg: ProviderConfig, settings: Settings, http: httpx.Client, budget: Budget):
        self.name, self.cfg, self.settings, self.http, self.budget = name, cfg, settings, http, budget
        self.setup()

    def setup(self) -> None:
        """Validate options; raise ProviderNotConfigured if something required is missing."""

    def opt(self, key: str, default=None):
        return self.cfg.opt(key, default)

    def require(self, key: str) -> str:
        value = self.opt(key)
        if value in (None, ""):
            raise ProviderNotConfigured(f"{self.name}: option '{key}' is not set")
        return str(value)

    def charge(self, n: int = 1) -> None:
        self.budget.charge(n)

    def fetch(self, method: str, url: str, *, retries: int | None = None, **kwargs) -> httpx.Response:
        """HTTP request with retries on 429/5xx (exponential backoff, honours Retry-After)."""
        retries = int(self.opt("retries", 3)) if retries is None else retries
        backoff = float(self.opt("retry_backoff_seconds", 10))
        min_interval = float(self.opt("min_interval_seconds", 0))
        for attempt in range(retries + 1):
            wait = min_interval - (time.monotonic() - getattr(self, "_last_fetch", 0.0))
            if wait > 0:
                time.sleep(wait)
            resp = self.http.request(method, url, **kwargs)
            self._last_fetch = time.monotonic()
            if resp.status_code != 429 and resp.status_code < 500 or attempt == retries:
                break
            retry_after = resp.headers.get("retry-after", "")
            wait = float(retry_after) if retry_after.isdigit() else backoff * 2 ** attempt
            log.info("RETRY provider=%s status=%s wait=%.0f host=%s", self.name, resp.status_code, wait,
                     resp.request.url.host)
            time.sleep(wait)
        resp.raise_for_status()
        return resp

    def excluded_sites(self) -> list[str]:
        """Excluded domains to add as -site: so they do not take result slots (exclude_sites_in_query: false)."""
        if not self.opt("exclude_sites_in_query", True):
            return []
        return list(self.settings.filters.exclude_domains)

    def langs(self) -> list[str] | None:
        """Supported languages; None means any."""
        return None

    @abstractmethod
    def search(self, req: SearchRequest) -> list[Mention]:
        ...

    def run(self, requests: list[SearchRequest]) -> Iterator[tuple[SearchRequest, list[Mention] | Exception]]:
        """Run a batch of requests. A failing request does not stop the others; an exceeded budget does."""
        for req in requests:
            try:
                yield req, self.search(req)
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                yield req, short_error(exc)

    def close(self) -> None:
        pass


def short_error(exc: Exception) -> Exception:
    """HTTPStatusError with a very long URL → 'HTTP 429 api.host'."""
    if isinstance(exc, httpx.HTTPStatusError):
        return RuntimeError(f"HTTP {exc.response.status_code} {exc.request.url.host}")
    return exc
