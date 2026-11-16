from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from news_checker.config import FiltersConfig, ProviderConfig, QueryConfig, Settings
from news_checker.models import Mention, SearchRequest
from news_checker.providers import REGISTRY, Budget, Provider, register
from news_checker.storage import Store, StoredMention

OWN_DOMAIN = "example-law.pl"


@register("fake")
class FakeProvider(Provider):
    """Returns the same four links for every request: one regular, one excluded, one Telegram, one invalid."""

    supports_date_range = True
    calls: list = []

    def langs(self):
        return ["pl", "ru"]

    def search(self, req: SearchRequest):
        self.charge()
        FakeProvider.calls.append(req)
        return [
            Mention(url=f"https://www.site.pl/{req.query_id}?utm_medium=x", title="T", snippet="s"),
            Mention(url=f"https://{OWN_DOMAIN}/own"),
            Mention(url="https://t.me/s/Other/1", title="tg"),
            Mention(url="not-a-url"),
        ]


@pytest.fixture
def settings(tmp_path):
    FakeProvider.calls = []
    s = Settings(
        queries=[QueryConfig(id="q1", phrases=["X"], langs=["pl", "ru", "en"]),
                 QueryConfig(id="q2", phrases=["Y"], langs=["pl"], providers=["other"])],
        providers={"fake": ProviderConfig(type="fake", backfill_window_days=100)},
        filters=FiltersConfig(exclude_domains=[OWN_DOMAIN]),
    )
    s.base_dir = tmp_path
    s.storage.url = "sqlite:///data/test.db"
    return s


@pytest.fixture
def store(settings):
    return Store(settings.database_url)


def run_provider(type_name, settings, handler, **opts):
    """Run one request of a registered HTTP provider against a mocked transport."""
    cfg = ProviderConfig(type=type_name, **opts)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    p = REGISTRY[type_name](type_name, cfg, settings, http, Budget(Store(settings.database_url), type_name, None, None))
    q = QueryConfig(id="q", phrases=["Jan Kowalski"])
    since, until = datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 3, 1, tzinfo=UTC)
    return list(p.run([SearchRequest(q, "pl", since, until)]))


def stored(i, verdict, title="Post", url=None):
    d = datetime(2026, 11, 14 - i % 5, tzinfo=UTC)
    url = url or f"https://a.pl/{i}"
    return StoredMention(id=i, url=url, original_url=url, domain="a.pl", source_type="web", title=title, snippet="",
                         published_at=d, first_seen_at=d, providers=["p"], queries=["q"], verdict=verdict)
