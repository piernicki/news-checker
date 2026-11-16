from __future__ import annotations

from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, select_autoescape

from ..i18n import t
from .base import Report, Reporter

_env = Environment(loader=PackageLoader("news_checker", "templates"), autoescape=select_autoescape(["html", "j2"]))


class HtmlFileReporter(Reporter):
    def __init__(self, directory: Path, title: str, timezone: str = "UTC", language: str = "en"):
        self.directory, self.title, self.timezone, self.language = directory, title, timezone, language

    def render(self, report: Report) -> str:
        tz = ZoneInfo(self.timezone)
        tr = partial(t, self.language)
        published = [m.published_at for m in report.visible if m.published_at]
        found = [m.first_seen_at for m in report.mentions]
        if published:  # publication range matters more than discovery dates (e.g. after an initial collection)
            period = tr("meta.published", a=f"{min(published).astimezone(tz):%d.%m.%Y}",
                        b=f"{max(published).astimezone(tz):%d.%m.%Y}")
            undated = len(report.visible) - len(published)
            period += tr("meta.undated", n=undated) if undated else ""
        elif found:
            period = tr("meta.found", a=f"{min(found).astimezone(tz):%d.%m.%Y}", b=f"{max(found).astimezone(tz):%d.%m.%Y}")
        else:
            period = ""
        title = self.title + (tr("backfill_suffix") if report.kind == "backfill" else "")
        return _env.get_template("report.html.j2").render(
            t=tr, lang=self.language, title=title, generated_at=f"{report.generated_at.astimezone(tz):%Y-%m-%d %H:%M}",
            period=period, total=len(report.visible), groups=report.groups(),
            rejected=sorted(report.rejected, key=lambda m: m.domain),
            unclassified=sum(1 for m in report.mentions if m.verdict is None),
        )

    def send(self, report: Report) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{report.name}.html"
        n = 2
        while path.exists():  # never overwrite an earlier report
            path = self.directory / f"{report.name}-{n}.html"
            n += 1
        path.write_text(self.render(report), encoding="utf-8")
        return str(path)
