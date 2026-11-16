"""Brave Search API: Brave's own index. Date window via freshness=YYYY-MM-DDtoYYYY-MM-DD."""

from __future__ import annotations

from ..models import Mention, SearchRequest
from ..queries import render_google
from . import register
from ._util import parse_date, strip_html
from .base import Provider

DEFAULT_LOCALES = {
    "pl": {"country": "PL", "search_lang": "pl"},
    "ru": {"country": "ALL", "search_lang": "ru"},
    "en": {"country": "ALL", "search_lang": "en"},
    "uk": {"country": "ALL", "search_lang": "uk"},
}


@register("brave")
class BraveProvider(Provider):
    supports_date_range = True
    URL = "https://api.search.brave.com/res/v1/web/search"

    def setup(self) -> None:
        self.api_key = self.require("api_key")
        self.locales = {**DEFAULT_LOCALES, **(self.opt("locales") or {})}
        self.count = min(int(self.opt("count", 20)), 20)
        self.pages = min(int(self.opt("pages", 1)), 10)

    def langs(self) -> list[str]:
        return list(self.locales)

    def search(self, req: SearchRequest) -> list[Mention]:
        params = {"q": render_google(req.query, self.excluded_sites()), "count": self.count, **self.locales[req.lang]}
        if req.since and req.until:
            params["freshness"] = f"{req.since:%Y-%m-%d}to{req.until:%Y-%m-%d}"
        out: list[Mention] = []
        for offset in range(self.pages):
            self.charge()
            resp = self.fetch("GET", 
                self.URL, params={**params, "offset": offset},
                headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            items = (data.get("web") or {}).get("results", [])
            out.extend(
                Mention(url=i["url"], title=strip_html(i.get("title")), snippet=strip_html(i.get("description")),
                        published_at=parse_date(i.get("page_age")))
                for i in items if i.get("url")
            )
            if not (data.get("query") or {}).get("more_results_available"):
                break
        return out
