from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
from conftest import run_provider

from news_checker.config import ProviderConfig
from news_checker.models import SearchRequest
from news_checker.providers import Budget
from news_checker.providers.telegram_web import TelegramWebProvider
from news_checker.providers.yandex import parse_xml


def test_serper(settings):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        assert request.headers["X-API-KEY"] == "k"
        return httpx.Response(200, json={"organic": [{"title": "A", "link": "https://a.pl/1", "snippet": "s",
                                                       "date": "Jan 5, 2025"}]})

    settings.filters.exclude_domains = ["instagram.com"]
    [(_, mentions)] = run_provider("serper", settings, handler, api_key="k")
    assert seen["q"] == '"Jan Kowalski" -site:instagram.com' and seen["gl"] == "pl"
    assert seen["tbs"].startswith("cdr:1,cd_min:01/01/2025")
    assert mentions[0].url == "https://a.pl/1" and mentions[0].published_at.year == 2025


def test_brave(settings):
    def handler(request):
        assert request.url.params["freshness"] == "2025-01-01to2025-03-01"
        return httpx.Response(200, json={"web": {"results": [
            {"url": "https://b.pl", "title": "<strong>B</strong>", "description": "d"}]},
            "query": {"more_results_available": False}})

    [(_, mentions)] = run_provider("brave", settings, handler, api_key="k")
    assert mentions[0].title == "B"


YANDEX_XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0"><response><results><grouping>
<group><doc><url>https://ya.ru/x</url><title>Ян <hlword>Ковальский</hlword></title>
<modtime>20250105T101500</modtime><passages><passage>текст <hlword>Ковальский</hlword> тут</passage></passages></doc></group>
</grouping></results></response></yandexsearch>"""


def test_yandex_xml_and_sync(settings):
    [m] = parse_xml(YANDEX_XML.encode())
    assert m.title == "Ян Ковальский" and m.snippet == "текст Ковальский тут" and m.published_at.day == 5
    assert parse_xml(b'<yandexsearch><response><error code="15">nothing</error></response></yandexsearch>') == []

    def handler(request):
        body = json.loads(request.content)
        assert body["query"]["queryText"].endswith("date:20250101..20250301")
        assert request.headers["Authorization"] == "Api-Key k"
        return httpx.Response(200, json={"rawData": base64.b64encode(YANDEX_XML.encode()).decode()})

    [(_, mentions)] = run_provider("yandex", settings, handler, api_key="k", folder_id="f", mode="sync")
    assert mentions[0].url == "https://ya.ru/x"


def test_yandex_deferred(settings):
    calls = {"poll": 0}

    def handler(request):
        if request.url.path.endswith("searchAsync"):
            return httpx.Response(200, json={"id": "op1", "done": False})
        calls["poll"] += 1
        done = calls["poll"] > 1
        return httpx.Response(200, json={"id": "op1", "done": done, **(
            {"response": {"rawData": base64.b64encode(YANDEX_XML.encode()).decode()}} if done else {})})

    [(_, mentions)] = run_provider("yandex", settings, handler, api_key="k", folder_id="f", poll_interval_seconds=0)
    assert len(mentions) == 1 and calls["poll"] == 2


def tme_page(chan, posts, prev=None):
    items = "".join(
        f'<div class="tgme_widget_message js-widget_message" data-post="{chan}/{pid}">'
        f'<div class="tgme_widget_message_text js-message_text">{text}</div>'
        f'<a class="tgme_widget_message_date"><time datetime="{date}" class="time"></time></a></div>'
        for pid, text, date in posts)
    link = f'<link rel="prev" href="/s/{chan}?q=x&amp;before={prev}">' if prev else ""
    return (f'<html><head>{link}</head><body><div class="tgme_channel_info_header_title">Law News</div>'
            f'{items}</body></html>')


def make_tg(settings, store, handler, terms):
    cfg = ProviderConfig(type="telegram_web", terms=terms, min_interval_seconds=0)
    return TelegramWebProvider("tgw", cfg, settings, httpx.Client(transport=httpx.MockTransport(handler)),
                               Budget(store, "tgw", None, None))


def test_telegram_web_full_history_then_window(settings, store):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "before=" in str(request.url):
            return httpx.Response(200, text=tme_page("lawnews", [(8000, "Ян Ковальский давно", "2025-01-01T10:00:00+00:00")]))
        return httpx.Response(200, text=tme_page("lawnews", [
            (11406, "юрист Ян Ковальский разобрал вопрос", "2026-11-14T10:00:00+00:00"),
            (9824, "как поясняет Ян Ковальский", "2026-05-22T10:00:00+00:00")], prev=9824))

    store.add_tg_channel("lawnews")
    p = make_tg(settings, store, handler, ["Ковальский"])
    req = SearchRequest(None, None, datetime(2026, 11, 12, tzinfo=UTC), datetime(2026, 11, 15, tzinfo=UTC))
    first = p.search(req)  # first search of a channel: full history, including the second page
    assert [m.url for m in first] == ["https://t.me/lawnews/11406", "https://t.me/lawnews/9824", "https://t.me/lawnews/8000"]
    assert first[0].title == "Law News" and "Ковальский" in first[0].snippet and first[0].published_at.day == 14
    assert "before=9824" in seen[1]
    seen.clear()
    again = p.search(req)  # afterwards only the run window: older posts are dropped, no second page
    assert [m.url for m in again] == ["https://t.me/lawnews/11406"] and len(seen) == 1


def test_telegram_web_snippet_includes_post_links(settings, store):
    page = ('<html><body><div class="tgme_widget_message" data-post="lawnews/9934">'
            '<div class="tgme_widget_message_text">A column. More in '
            '<a href="https://t.me/my_channel/120">the lawyer\'s channel</a></div>'
            '<time datetime="2026-06-03T10:00:00+00:00"></time></div></body></html>')
    store.add_tg_channel("lawnews")
    p = make_tg(settings, store, lambda r: httpx.Response(200, text=page), ["my_channel"])
    [m] = p.search(SearchRequest(None, None, None, None))
    assert m.snippet.endswith("[links in post: https://t.me/my_channel/120]")
