"""Reader-facing texts (HTML report, Telegram message, classifier reasons) in the configured language."""

from __future__ import annotations

LANGUAGES = ("en", "ru")

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "language_name": "English",
        "title": "New mentions",
        "backfill_suffix": " (initial collection)",
        "group.web": "Web",
        "group.news": "News",
        "group.telegram": "Telegram",
        "group.social": "Social media",
        "meta.generated": "Generated {at}",
        "meta.published": "published {a} — {b}",
        "meta.undated": " (undated: {n})",
        "meta.found": "found {a} — {b}",
        "meta.links": "links: {n}",
        "meta.rejected": "filtered out: {n}",
        "meta.unclassified": "not classified: {n}",
        "empty": "No new links.",
        "rejected_block": "Probably not a match ({n})",
        "tag.unclear": "unclear",
        "tag.published": "published {d}",
        "tag.found": "found {d}",
        "tg.match": "Matches: {n}",
        "tg.unclear": "unclear: {n}",
        "tg.rejected": "filtered out: {n}",
        "tg.unclear_header": "Unclear, check manually:",
        "tg.more": "…and {n} more in the attached file.",
        "tg.caption": "Full report",
    },
    "ru": {
        "language_name": "Russian",
        "title": "Новые упоминания",
        "backfill_suffix": " (первый сбор за период)",
        "group.web": "Веб",
        "group.news": "Новости",
        "group.telegram": "Telegram",
        "group.social": "Соцсети",
        "meta.generated": "Сформирован {at}",
        "meta.published": "публикации {a} — {b}",
        "meta.undated": " (без даты: {n})",
        "meta.found": "найдены {a} — {b}",
        "meta.links": "ссылок: {n}",
        "meta.rejected": "отсеяно фильтром: {n}",
        "meta.unclassified": "без оценки: {n}",
        "empty": "Новых ссылок нет.",
        "rejected_block": "Скорее всего не тот человек ({n})",
        "tag.unclear": "неясно",
        "tag.published": "опубл. {d}",
        "tag.found": "найдено {d}",
        "tg.match": "Упоминаний: {n}",
        "tg.unclear": "неясно: {n}",
        "tg.rejected": "отсеяно фильтром: {n}",
        "tg.unclear_header": "Неясно, проверь вручную:",
        "tg.more": "…и ещё {n} — в приложенном файле.",
        "tg.caption": "Полный отчёт",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    text = TEXTS.get(lang, TEXTS["en"]).get(key) or TEXTS["en"][key]
    return text.format(**kwargs) if kwargs else text
