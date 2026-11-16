from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def parse_date(value) -> datetime | None:
    """ISO 8601, RFC 822, YYYYMMDDTHHMMSS(Z), search-result dates in en/pl/ru/uk, relative ('3 days ago')."""
    if value is None or value == "":
        return None
    s = str(value).strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return parse_human_date(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Month prefixes (en, pl, ru, uk), matched against the start of a casefolded word.
_MONTHS = {
    1: ("jan", "sty", "янв", "січ"), 2: ("feb", "lut", "фев", "лют"), 3: ("mar", "мар", "бер"),
    4: ("apr", "kwi", "апр", "кві"), 5: ("may", "maj", "мая", "май", "тра"), 6: ("jun", "cze", "июн", "чер"),
    7: ("jul", "lip", "июл", "лип"), 8: ("aug", "sie", "авг", "сер"), 9: ("sep", "wrz", "сен", "вер"),
    10: ("oct", "paź", "paz", "окт", "жов"), 11: ("nov", "lis", "ноя", "лис"), 12: ("dec", "gru", "дек", "гру"),
}
# Relative dates: '3 days ago', '3 dni temu', '3 дня назад', '3 дні тому', 'yesterday'.
_UNITS = (
    (("min", "мин", "хв"), "minutes"), (("hour", "godz", "час", "ч", "годин"), "hours"),
    (("day", "dzie", "dni", "дн", "ден", "дня", "доб"), "days"), (("week", "tydz", "tyg", "недел", "тиж"), "weeks"),
    (("month", "miesi", "мес", "місяц"), "months"), (("year", "rok", "lat", "лет", "год", "рік", "рок"), "years"),
)
_YESTERDAY = ("yesterday", "wczoraj", "вчера", "вчора")
_REL_RE = re.compile(r"(\d+)\s*([^\W\d_]+)")
_WORD_RE = re.compile(r"[^\W\d_]+")


def parse_human_date(s: str, now: datetime | None = None) -> datetime | None:
    """Search-result dates in en/pl/ru/uk: '5 мар. 2025 г.', 'Mar 5, 2025', '12 lis 2025', '3 дня назад'."""
    now = now or datetime.now(UTC)
    text = s.replace(" ", " ").replace("\xa0", " ").casefold().strip()
    if any(text.startswith(w) for w in _YESTERDAY):
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if any(m in text for m in ("ago", "temu", "назад", "тому")):
        match = _REL_RE.search(text)
        if match:
            n, word = int(match.group(1)), match.group(2)
            for prefixes, unit in _UNITS:
                if word.startswith(prefixes):
                    if unit == "months":
                        return now - timedelta(days=30 * n)
                    if unit == "years":
                        return now - timedelta(days=365 * n)
                    return now - timedelta(**{unit: n})
        return None
    year = re.search(r"\b(19|20)\d{2}\b", text)
    month = next((num for w in _WORD_RE.findall(text) for num, pref in _MONTHS.items() if w.startswith(pref)), None)
    day = re.search(r"\b(\d{1,2})\b", text[: year.start()] + " " + text[year.end():] if year else text)
    if not (year and month and day):
        return None
    try:
        return datetime(int(year.group(0)), month, int(day.group(1)), tzinfo=UTC)
    except ValueError:
        return None


def snippet_around(text: str, needle: str, width: int = 300) -> str:
    """Text around the first case-insensitive occurrence of needle."""
    text = _WS_RE.sub(" ", text or "").strip()
    if len(text) <= width:
        return text
    pos = text.lower().find(needle.lower()) if needle else -1
    if pos < 0:
        return text[:width].rstrip() + "…"
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
