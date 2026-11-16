from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from conftest import stored

from news_checker import app as app_mod
from news_checker.classifiers.anthropic_llm import INSTRUCTIONS, AnthropicClassifier
from news_checker.classify import classify_mentions
from news_checker.config import TelegramDeliveryConfig
from news_checker.pipeline import Pipeline
from news_checker.reporters import Report
from news_checker.reporters.telegram_bot import LIMIT, TelegramBotReporter, _chunk


class FakeMessages:
    """Stand-in for anthropic.Anthropic().messages returning prepared (stop_reason, results) replies."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        stop, results = self.replies.pop(0)
        return SimpleNamespace(
            stop_reason=stop, stop_details=None,
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=json.dumps({"results": results}))],
        )


def make_llm(settings, replies, monkeypatch):
    fake = FakeMessages(replies)
    monkeypatch.setattr(AnthropicClassifier, "make_client", lambda self: SimpleNamespace(messages=fake))
    settings.classifier = settings.classifier.model_copy(update={
        "enabled": True, "subject": "Jan X, a lawyer", "batch_size": 2})
    settings.classifier.__pydantic_extra__ = {"api_key": "k"}
    return fake


@pytest.mark.parametrize("language, rejected_label", [("en", "Probably not a match (1)"), ("ru", "Скорее всего не тот человек (1)")])
def test_classifier_flow_and_report(settings, monkeypatch, language, rejected_label):
    settings.reports.language = language
    # FakeProvider yields two new links: site.pl/q1 and t.me/other/1 (ids 1 and 2)
    fake = make_llm(settings, [("end_turn", [
        {"id": 1, "verdict": "match", "reason": "quoted as an expert"},
        {"id": 2, "verdict": "no_match", "reason": "namesake"},
    ])], monkeypatch)
    result, paths = app_mod.run_cycle(settings, "daily")
    assert result.classify.counts == {"match": 1, "no_match": 1}
    call = fake.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema" and "Jan X" in call["system"]
    assert {"en": "in English", "ru": "in Russian"}[language] in call["system"]
    schema = call["output_config"]["format"]["schema"]["properties"]["results"]["items"]
    assert schema["required"] == ["id", "verdict", "reason"]
    html = open(paths[0], encoding="utf-8").read()
    assert f'lang="{language}"' in html and "site.pl" in html
    assert rejected_label in html and "namesake" in html
    assert "Telegram (" not in html  # "no match" links are hidden from the main groups


def test_classifier_errors_leave_links_unclassified(settings, store, monkeypatch):
    Pipeline(settings, store).collect("daily")
    make_llm(settings, [("refusal", []), ("max_tokens", [])], monkeypatch)
    settings.classifier.batch_size = 1
    res = classify_mentions(settings, store, store.unclassified())
    assert len(res.errors) == 2 and len(store.unclassified()) == 2


def test_classifier_skipped_without_key(settings, store, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings.classifier = settings.classifier.model_copy(update={"enabled": True, "subject": "x"})
    Pipeline(settings, store).collect("daily")
    res = classify_mentions(settings, store, store.unclassified())
    assert res.status.startswith("skipped") and "API key" in res.status


def test_prompt_treats_own_content_as_no_match():
    assert "ANOTHER author" in INSTRUCTIONS and "OWN content" in INSTRUCTIONS and "Photo by" in INSTRUCTIONS


def bot(settings, language="en", max_links=3):
    settings.reports.telegram = TelegramDeliveryConfig(enabled=True, bot_token="T", chat_id="-100", max_links=max_links)
    settings.reports.language = language
    return settings


@pytest.mark.parametrize("language, stats, more", [
    ("en", "Matches: 3 · unclear: 1 · filtered out: 1", "…and 1 more in the attached file."),
    ("ru", "Упоминаний: 3 · неясно: 1 · отсеяно фильтром: 1", "…и ещё 1 — в приложенном файле."),
])
def test_bot_message_format(settings, language, stats, more):
    cfg = bot(settings, language).reports.telegram
    r = TelegramBotReporter(cfg, "Mentions", "Europe/Warsaw", language, httpx.Client())
    report = Report(name="x", kind="daily", generated_at=datetime(2026, 11, 15, tzinfo=UTC), mentions=[
        stored(1, "match", "Jan <b>Kowalski</b> & court"), stored(2, "match"), stored(3, "unclear"),
        stored(4, "no_match"), stored(5, "match")])
    [text] = r.build_messages(report)
    assert stats in text and more in text  # max_links=3 is used up by "match" links
    assert "&lt;b&gt;Kowalski&lt;/b&gt; &amp; court" in text and 'href="https://a.pl/1"' in text
    assert "a.pl/4" not in text  # "no match" links only in the attached file


def test_chunking_respects_telegram_limit():
    chunks = _chunk([f"line {i} " + "x" * 300 for i in range(40)])
    assert len(chunks) > 1 and all(len(c) <= LIMIT for c in chunks)


def test_delivery_failure_is_retried(settings, store, monkeypatch):
    bot(settings)
    calls, fail = [], {"on": True}

    def handler(request):
        calls.append(request.url.path.rsplit("/", 1)[1])
        if fail["on"]:
            return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests"})
        return httpx.Response(200, json={"ok": True, "result": {}})

    real = httpx.Client
    monkeypatch.setattr("news_checker.reporters.telegram_bot.httpx.Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler)))
    Pipeline(settings, store).collect("daily")
    out = app_mod.report_unreported(settings, store, "daily")
    assert any("error" in o for o in out)
    [row] = store.failed_deliveries("telegram", 5)
    assert row["attempts"] == 1 and "Too Many Requests" in row["last_error"]

    fail["on"] = False
    calls.clear()
    app_mod.retry_failed_deliveries(settings, store)
    assert calls == ["sendMessage", "sendDocument"] and store.failed_deliveries("telegram", 5) == []


def test_custom_prompt_from_config(settings, monkeypatch):
    from news_checker.classifiers import ClassifierNotConfigured
    from news_checker.providers import Budget
    from news_checker.storage import Store

    make_llm(settings, [], monkeypatch)
    settings.classifier.prompt = "Find {subject}. Reasons in {reason_language}."
    clf = AnthropicClassifier(settings.classifier, settings, Budget(Store(settings.database_url), "c", None, None))
    assert clf.system == "Find Jan X, a lawyer. Reasons in English."
    settings.classifier.prompt = "Broken {unknown}"
    with pytest.raises(ClassifierNotConfigured, match="placeholder"):
        AnthropicClassifier(settings.classifier, settings, Budget(Store(settings.database_url), "c", None, None))
