from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from conftest import OWN_DOMAIN, FakeProvider

from news_checker.app import report_unreported
from news_checker.config import ProviderConfig
from news_checker.logsetup import setup_logging
from news_checker.models import Mention
from news_checker.pipeline import Pipeline
from news_checker.providers import Budget, BudgetExceeded, register
from news_checker.storage import Store


def test_collect_dedup_and_report(settings, store):
    pipe = Pipeline(settings, store)
    st = pipe.collect("daily").stats["fake"]
    # q1 × (pl, ru): the provider does not support en; q2 is only for the 'other' provider
    assert st.requests == 2 and len(FakeProvider.calls) == 2
    assert st.new == 2  # site.pl/q1 + t.me/other/1 (the second request finds duplicates)
    assert st.filtered == 4  # own domain ×2 + invalid URL ×2
    assert pipe.collect("daily").new_total == 0

    mentions = {m.url: m for m in store.unreported()}
    assert set(mentions) == {"https://site.pl/q1", "https://t.me/other/1"}
    assert mentions["https://t.me/other/1"].source_type == "telegram"
    assert mentions["https://site.pl/q1"].providers == ["fake"]

    [path] = report_unreported(settings, store, "daily")
    html = open(path, encoding="utf-8").read()
    assert "https://www.site.pl/q1?utm_medium=x" in html and "Telegram (1)" in html
    assert "found " in html  # no publication dates: the header shows discovery dates
    assert store.unreported() == []
    assert report_unreported(settings, store, "daily") == []  # nothing new, no file


def test_backfill_windows(settings, store):
    pipe = Pipeline(settings, store)
    provider = pipe.build_providers()[0][0]
    now = datetime(2026, 1, 1, tzinfo=UTC)
    windows = sorted({(r.since, r.until) for r in pipe.plan(provider, "backfill", now)})
    assert len(windows) == 8  # 730 / 100 days
    assert windows[0][0] == now - timedelta(days=730) and windows[-1][1] == now
    assert all(a[1] == b[0] for a, b in zip(windows, windows[1:]))


def test_query_without_date_window(settings, store):
    from news_checker.config import QueryConfig

    settings.queries.append(QueryConfig(id="tg-web", phrases=["X"], site="t.me", langs=["pl"], date_window=False))
    pipe = Pipeline(settings, store)
    provider = pipe.build_providers()[0][0]
    now = datetime(2026, 1, 1, tzinfo=UTC)
    back = [r for r in pipe.plan(provider, "backfill", now) if r.query_id == "tg-web"]
    daily = [r for r in pipe.plan(provider, "daily", now) if r.query_id == "tg-web"]
    assert len(back) == 1 and back[0].since is None and back[0].until is None
    assert len(daily) == 1 and daily[0].since is None


def test_budget(store):
    b = Budget(store, "p", run_limit=2, monthly_limit=3)
    b.charge()
    b.charge()
    with pytest.raises(BudgetExceeded):
        b.charge()
    b2 = Budget(store, "p", run_limit=None, monthly_limit=3)
    b2.charge()
    with pytest.raises(BudgetExceeded):
        b2.charge()


def test_budget_stops_provider(settings, store):
    settings.providers["fake"].run_limit = 1
    result = Pipeline(settings, store).collect("daily")
    assert "limit" in result.stats["fake"].status and result.stats["fake"].requests == 1


def test_unconfigured_provider_is_skipped(settings, store):
    settings.providers["serper"] = ProviderConfig(type="serper", api_key="")
    providers, skipped = Pipeline(settings, store).build_providers()
    assert "serper" in skipped and [p.name for p in providers] == ["fake"]


def test_feed_links_become_tracked_channels(settings, store):
    @register("fake_tg")
    class TgLinks(FakeProvider):
        def search(self, req):
            return [Mention(url="https://t.me/s/LawNews?before=9823", title="feed"),
                    Mention(url="https://t.me/warsawdaily/21700", title="post"),
                    Mention(url="https://t.me/s/itnews/15040", title="post preview"),
                    Mention(url="https://t.me/s/my_channel?before=5")]

    settings.providers = {"p": ProviderConfig(type="fake_tg")}
    settings.filters.exclude_url_prefixes = ["t.me/my_channel"]
    Pipeline(settings, store).collect("daily")
    assert set(store.list_tg_channels()) == {"lawnews", "warsawdaily", "itnews"}  # own channel is not tracked
    # feeds are not stored; a post preview t.me/s/<channel>/<id> is stored as a post
    assert sorted(m.url for m in store.unreported()) == ["https://t.me/itnews/15040", "https://t.me/warsawdaily/21700"]


def test_pipeline_log_events(settings, store, caplog):
    settings.filters.exclude_text = [r"^tg$"]  # title of FakeProvider's Telegram link
    with caplog.at_level("INFO", logger="news_checker"):
        result = Pipeline(settings, store).collect("daily")
    assert result.stats["fake"].new == 1
    text = caplog.text
    assert "RUN start" in text and "RUN end" in text
    assert "REQ  provider=fake query=q1 lang=pl window=" in text and "new=1" in text
    assert "NEW  provider=fake query=q1 type=web domain=site.pl" in text
    assert "SKIP provider=fake query=q1 reason=exclude_text:^tg$ url=https://t.me/s/Other/1" in text
    assert f"reason=domain url=https://{OWN_DOMAIN}/own" in text


def test_log_file(settings, tmp_path):
    path = setup_logging(settings)
    logging.getLogger("news_checker.test").info("NEW hello")
    for h in logging.getLogger().handlers:
        h.flush()
    assert path == tmp_path / "logs" / "news-checker.log" and "NEW hello" in path.read_text()
    setup_logging(None)


def test_migration_adds_columns(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("create table mentions (id integer primary key, url varchar(2048) not null unique, "
                "original_url varchar(4096) not null, domain varchar(255) not null, source_type varchar(16) not null, "
                "title text not null, snippet text not null, published_at datetime, first_seen_at datetime not null, "
                "last_seen_at datetime not null, first_run_kind varchar(16) not null, reported_at datetime, "
                "report_name varchar(255))")
    con.commit()
    con.close()
    Store(f"sqlite:///{path}")
    cols = {r[1] for r in sqlite3.connect(path).execute("pragma table_info(mentions)")}
    assert {"verdict", "verdict_reason", "classifier", "classified_at"} <= cols


def test_google_redirects_are_dropped_but_reveal_channels(settings, store):
    from news_checker.filters import MentionFilter
    from news_checker.normalize import canonical_url, host_of

    goto = "https://www.google.com/goto?url=CAESYwHEIkXX_opaque&gsessionid=abc"

    @register("fake_goto")
    class GotoLinks(FakeProvider):
        def search(self, req):
            return [Mention(url=goto, title="Law News", display_url="https://t.me › lawnews"),
                    Mention(url=goto + "2", title="Cut", display_url="https://t.me › very_long_chan..."),
                    Mention(url=goto + "3", title="Feed", display_url="https://t.me › s › itnews")]

    settings.providers = {"p": ProviderConfig(type="fake_goto")}
    st = Pipeline(settings, store).collect("daily").stats["p"]
    assert st.new == 0 and st.filtered == 6  # 3 links × 2 languages of q1
    assert set(store.list_tg_channels()) == {"lawnews", "itnews"}  # a cut-off name is not guessed
    f = MentionFilter(settings.filters)
    c = canonical_url(goto)
    assert f.reason(c, host_of(c)) == "google_redirect"
    c = canonical_url("https://www.google.com/url?q=https://a.pl/x")  # decodable redirects are unwrapped instead
    assert f.reason(c, host_of(c)) is None and c == "https://a.pl/x"
