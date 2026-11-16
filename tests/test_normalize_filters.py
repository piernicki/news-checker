from __future__ import annotations

from datetime import UTC, datetime

import pytest

from news_checker.config import FiltersConfig, QueryConfig, load_settings
from news_checker.filters import MentionFilter
from news_checker.normalize import canonical_url, classify, host_of, unwrap_redirect
from news_checker.providers._util import parse_date, parse_human_date
from news_checker.queries import render, render_yandex


@pytest.mark.parametrize("raw, expected", [
    ("http://www.Example.com/a/?utm_source=x&b=2&a=1#frag", "https://example.com/a?a=1&b=2"),
    ("https://example.com/", "https://example.com/"),
    ("example.com/path/", "https://example.com/path"),
    ("https://t.me/s/Some_Channel/123", "https://t.me/some_channel/123"),
    ("https://telegram.me/Chan/5", "https://t.me/chan/5"),
    ("https://example.com/x?fbclid=1&gclid=2", "https://example.com/x"),
    ("https://www.google.com/url?rct=j&url=https://news.pl/art%3Fid%3D1&ct=ga", "https://news.pl/art?id=1"),
])
def test_canonical_url(raw, expected):
    assert canonical_url(raw) == expected


def test_unwrap_and_classify():
    assert unwrap_redirect("https://www.google.com/url?q=https://a.pl/x") == "https://a.pl/x"
    assert host_of("https://www.pl.linkedin.com/in/x") == "pl.linkedin.com"
    assert classify("pl.linkedin.com") == "social"
    assert classify("t.me") == "telegram"
    assert classify("rp.pl") == "web"


def make_filter(**kw):
    return MentionFilter(FiltersConfig(**kw))


def test_url_filters():
    f = make_filter(
        exclude_domains=["example-law.pl"],
        exclude_url_prefixes=["t.me/my_channel", "threads.com/@me"],
        exclude_url_regex=[r"^https://([a-z]{2}\.)?linkedin\.com/in/me"],
        ignore_telegram_chats=["@LawChat", "-1001234"],
    )

    def reason(u):
        c = canonical_url(u)
        return f.reason(c, host_of(c))

    assert reason("https://blog.example-law.pl/post") == "domain"
    assert reason("https://t.me/my_channel/15") == "prefix"
    assert reason("https://t.me/my_channel") == "prefix"
    assert reason("https://t.me/my_channel_fans/1") is None
    assert reason("https://www.threads.com/@me/post/abc") == "prefix"
    assert reason("https://www.threads.com/@Me/post/abc") == "prefix"  # case-insensitive
    assert reason("https://pl.linkedin.com/in/me") == "regex"
    assert reason("https://linkedin.com/posts/other") is None
    assert reason("https://t.me/lawchat/99") == "telegram_chat"
    assert reason("https://t.me/c/1234/5") == "telegram_chat"
    assert f.is_ignored_chat("lawchat") and f.is_ignored_chat(None, 1234)


def test_text_filters_and_negative_phrases():
    q = QueryConfig(id="x", phrases=["Kowalski"], any=["adwokat"], exclude=["Piotr Kowalski"])
    assert render(q) == '"Kowalski" adwokat -"Piotr Kowalski"'
    f = make_filter(exclude_text=[r"Piotr(a|em|owi)?\s+Kowalsk"])
    assert f.text_reason("Spotkanie z Piotrem Kowalskim").startswith("exclude_text")
    assert f.text_reason("PIOTR KOWALSKI", ["piotr kowalski"]).startswith("query_exclude")
    assert f.text_reason("Jan Kowalski, adwokat") is None


def test_render():
    q = QueryConfig(id="a", phrases=["Jan Kowalski", "Jana Kowalskiego"], match="any", any=["adwokat", "radca prawny"],
                    site="linkedin.com")
    assert render(q) == '("Jan Kowalski" OR "Jana Kowalskiego") (adwokat OR "radca prawny") site:linkedin.com'
    assert render_yandex(QueryConfig(id="b", phrases=["A B"], any=["x", "y"])) == '"A B" (x | y)'
    assert render(QueryConfig(id="c", phrases=["A", "B"])) == '"A" "B"'


def test_render_exclude_sites():
    q = QueryConfig(id="a", phrases=["Jan Kowalski"])
    assert render(q, exclude_sites=["instagram.com", "threads.com"]) == '"Jan Kowalski" -site:instagram.com -site:threads.com'
    tq = QueryConfig(id="b", phrases=["Jan Kowalski"], site="t.me")  # a query with its own site: is left as is
    assert render(tq, exclude_sites=["instagram.com"]) == '"Jan Kowalski" site:t.me'


def test_example_config_profile_and_own_content_rules():
    f = MentionFilter(load_settings("config.example.yaml").filters)

    def why(url, text=""):
        c = canonical_url(url)
        return f.reason(c, host_of(c)) or f.text_reason(text)

    for url in ("https://www.facebook.com/p/Jan-Kowalski-100045736877799/", "https://www.facebook.com/jan.kowalski.1/",
                "https://www.facebook.com/100064763317196/reels/", "https://www.facebook.com/1/videos/liker/2/",
                "https://www.youtube.com/@SomeChannel/playlists"):
        assert why(url) == "regex", url
    for url in ("https://www.facebook.com/media/posts/abc/123", "https://www.youtube.com/watch?v=1",
                "https://www.facebook.com/groups/848926408469923/posts/1"):
        assert why(url) is None, url
    assert why("https://www.instagram.com/p/X/") == "domain"
    assert why("https://a.pl/x", "Photo by Jan Kowalski on May 1").startswith("exclude_text")
    assert why("https://www.facebook.com/groups/1/posts/2", "write to jan.kowalski@example.pl").startswith("exclude_text")


@pytest.mark.parametrize("raw, expected", [
    ("5 мар. 2025 г.", "2025-03-05"),
    ("24 сент. 2025 г.", "2025-09-24"),
    ("17 мая 2024 г.", "2024-05-17"),
    ("Mar 5, 2025", "2025-03-05"),
    ("12 lis 2025", "2025-11-12"),
    ("3 paź 2024", "2024-10-03"),
    ("7 лют. 2025 р.", "2025-02-07"),
])
def test_parse_absolute_dates(raw, expected):
    got = parse_date(raw)
    assert got is not None and f"{got:%Y-%m-%d}" == expected


@pytest.mark.parametrize("raw, expected", [
    ("3 days ago", "2026-11-12"),
    ("3 dni temu", "2026-11-12"),
    ("2 дня назад", "2026-11-13"),
    ("5 годин тому", "2026-11-15"),
    ("2 года назад", "2024-11-15"),
    ("1 tydzień temu", "2026-11-08"),
    ("вчера", "2026-11-14"),
])
def test_parse_relative_dates(raw, expected):
    got = parse_human_date(raw, datetime(2026, 11, 15, 12, tzinfo=UTC))
    assert got is not None and f"{got:%Y-%m-%d}" == expected


def test_parse_date_garbage():
    assert parse_date("soon") is None and parse_date("Status Quo") is None
