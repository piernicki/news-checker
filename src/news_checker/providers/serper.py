"""Serper.dev: Google results via API. Date window via tbs=cdr; 1 credit per page of up to 10 results."""

from __future__ import annotations

from ..models import Mention, SearchRequest
from ..queries import render_google
from . import register
from ._util import parse_date
from .base import Provider

DEFAULT_LOCALES = {
    "pl": {"gl": "pl", "hl": "pl"},
    "ru": {"gl": "pl", "hl": "ru"},
    "en": {"gl": "us", "hl": "en"},
    "uk": {"gl": "ua", "hl": "uk"},
}


@register("serper")
class SerperProvider(Provider):
    supports_date_range = True
    BASE = "https://google.serper.dev"

    def setup(self) -> None:
        self.api_key = self.require("api_key")
        self.locales = {**DEFAULT_LOCALES, **(self.opt("locales") or {})}
        self.endpoints = self.opt("endpoints", ["search"])  # search and/or news
        self.num = int(self.opt("num", 10))
        self.pages = int(self.opt("pages", 1))

    def langs(self) -> list[str]:
        return list(self.locales)

    def search(self, req: SearchRequest) -> list[Mention]:
        body = {"q": render_google(req.query, self.excluded_sites()), "num": self.num, **self.locales[req.lang]}
        if req.since and req.until:
            body["tbs"] = f"cdr:1,cd_min:{req.since:%m/%d/%Y},cd_max:{req.until:%m/%d/%Y}"
        out: list[Mention] = []
        for endpoint in self.endpoints:
            key = "news" if endpoint == "news" else "organic"
            for page in range(1, self.pages + 1):
                self.charge()
                resp = self.fetch("POST", 
                    f"{self.BASE}/{endpoint}", json={**body, "page": page},
                    headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                items = resp.json().get(key, [])
                out.extend(
                    Mention(url=i["link"], title=i.get("title", ""), snippet=i.get("snippet", ""),
                            published_at=parse_date(i.get("date")),
                            source_type="news" if endpoint == "news" else None)
                    for i in items if i.get("link")
                )
                if len(items) < self.num:
                    break
        return out
