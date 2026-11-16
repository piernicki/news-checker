"""Rendering a provider-agnostic QueryConfig into a search engine's syntax."""

from __future__ import annotations

from .config import QueryConfig


def _quote(text: str) -> str:
    return '"' + text.replace('"', "") + '"'


def render(query: QueryConfig, *, or_op: str = "OR", site_op: str = "site:", exclude_sites: list[str] = ()) -> str:
    """Google/Brave: `"A" "B" (x OR y) site:z -site:w`. Yandex: or_op="|".

    exclude_sites are added as -site:domain unless the query has its own site: restriction.
    """
    phrases = [_quote(p) for p in query.phrases]
    if query.match == "any" and len(phrases) > 1:
        parts = ["(" + f" {or_op} ".join(phrases) + ")"]
    else:
        parts = phrases
    if query.any:
        words = [_quote(w) if " " in w else w for w in query.any]
        parts.append("(" + f" {or_op} ".join(words) + ")" if len(words) > 1 else words[0])
    parts.extend("-" + _quote(x) for x in query.exclude)
    if query.site:
        parts.append(f"{site_op}{query.site}")
    else:
        parts.extend(f"-{site_op}{d}" for d in exclude_sites)
    return " ".join(parts)


def render_google(query: QueryConfig, exclude_sites: list[str] = ()) -> str:
    return render(query, exclude_sites=exclude_sites)


def render_yandex(query: QueryConfig) -> str:
    return render(query, or_op="|")
