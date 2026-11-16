"""Yandex Search API v2 (Yandex Cloud / AI Studio).

mode: deferred (default, cheaper: all requests are submitted first, then polled) or sync.
The date window uses the query-language operator date:YYYYMMDD..YYYYMMDD.
Yandex has no Polish search type, so pl/en go through SEARCH_TYPE_COM.
New Yandex Cloud folders may have no quota for deferred requests (HTTP 429); use mode: sync then.
"""

from __future__ import annotations

import base64
import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator

from ..models import Mention, SearchRequest
from ..queries import render_yandex
from . import register
from ._util import parse_date
from .base import BudgetExceeded, Provider, short_error

log = logging.getLogger(__name__)

DEFAULT_LOCALES = {
    "ru": {"searchType": "SEARCH_TYPE_RU", "l10n": "LOCALIZATION_RU"},
    "uk": {"searchType": "SEARCH_TYPE_RU", "l10n": "LOCALIZATION_UK"},
    "pl": {"searchType": "SEARCH_TYPE_COM", "l10n": "LOCALIZATION_EN"},
    "en": {"searchType": "SEARCH_TYPE_COM", "l10n": "LOCALIZATION_EN"},
}
NO_RESULTS_CODE = "15"


@register("yandex")
class YandexProvider(Provider):
    supports_date_range = True
    SYNC_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
    ASYNC_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
    OPERATION_URL = "https://operation.api.cloud.yandex.net/operations/{id}"

    def setup(self) -> None:
        self.api_key = self.require("api_key")
        self.folder_id = self.require("folder_id")
        self.locales = {**DEFAULT_LOCALES, **(self.opt("locales") or {})}
        self.mode = self.opt("mode", "deferred")
        self.per_page = int(self.opt("per_page", 50))
        self.poll_interval = float(self.opt("poll_interval_seconds", 15))
        self.poll_timeout = float(self.opt("poll_timeout_seconds", 1800))

    def langs(self) -> list[str]:
        return list(self.locales)

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Api-Key {self.api_key}"}

    def body(self, req: SearchRequest) -> dict:
        text = render_yandex(req.query)
        if req.since and req.until:
            text += f" date:{req.since:%Y%m%d}..{req.until:%Y%m%d}"
        loc = self.locales[req.lang]
        return {
            "query": {"searchType": loc["searchType"], "queryText": text[:400], "familyMode": "FAMILY_MODE_NONE",
                      "page": "0", "fixTypoMode": "FIX_TYPO_MODE_OFF"},
            "sortSpec": {"sortMode": "SORT_MODE_BY_TIME", "sortOrder": "SORT_ORDER_DESC"},
            "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": str(self.per_page), "docsInGroup": "1"},
            "maxPassages": "3",
            "l10n": loc["l10n"],
            "folderId": self.folder_id,
            "responseFormat": "FORMAT_XML",
            **({"region": str(loc["region"])} if loc.get("region") else {}),
        }

    def search(self, req: SearchRequest) -> list[Mention]:
        self.charge()
        resp = self.fetch("POST", self.SYNC_URL, json=self.body(req), headers=self.headers)
        resp.raise_for_status()
        return parse_xml(base64.b64decode(resp.json()["rawData"]))

    def run(self, requests: list[SearchRequest]) -> Iterator[tuple[SearchRequest, list[Mention] | Exception]]:
        if self.mode != "deferred":
            yield from super().run(requests)
            return
        pending: dict[str, SearchRequest] = {}
        for req in requests:
            try:
                self.charge()
                resp = self.fetch("POST", self.ASYNC_URL, json=self.body(req), headers=self.headers)
                resp.raise_for_status()
                pending[resp.json()["id"]] = req
            except BudgetExceeded:
                break  # requests already submitted are still collected
            except Exception as exc:  # noqa: BLE001
                yield req, short_error(exc)
        deadline = time.monotonic() + self.poll_timeout
        while pending and time.monotonic() < deadline:
            time.sleep(self.poll_interval)
            for op_id, req in list(pending.items()):
                try:
                    resp = self.fetch("GET", self.OPERATION_URL.format(id=op_id), headers=self.headers)
                    resp.raise_for_status()
                    op = resp.json()
                    if not op.get("done"):
                        continue
                    del pending[op_id]
                    if op.get("error"):
                        yield req, RuntimeError(f"Yandex: {op['error']}")
                    else:
                        yield req, parse_xml(base64.b64decode(op["response"]["rawData"]))
                except Exception as exc:  # noqa: BLE001
                    del pending[op_id]
                    yield req, short_error(exc)
        for req in pending.values():
            yield req, TimeoutError("Yandex: deferred request did not finish within poll_timeout_seconds")


def parse_xml(raw: bytes) -> list[Mention]:
    root = ET.fromstring(raw)
    error = root.find(".//response/error")
    if error is not None:
        if error.get("code") == NO_RESULTS_CODE:
            return []
        raise RuntimeError(f"Yandex error {error.get('code')}: {''.join(error.itertext()).strip()}")
    out = []
    for doc in root.iter("doc"):
        url = (doc.findtext("url") or "").strip()
        if not url:
            continue
        title_el = doc.find("title")
        passages = [" ".join("".join(p.itertext()).split()) for p in doc.iter("passage")]
        headline = doc.findtext("headline") or ""
        out.append(Mention(
            url=url,
            title=" ".join("".join(title_el.itertext()).split()) if title_el is not None else "",
            snippet=" … ".join(p for p in passages if p) or headline,
            published_at=parse_date(doc.findtext("modtime")),
        ))
    return out
