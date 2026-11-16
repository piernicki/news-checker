"""Loading and validation of config.yaml. Values like ${VAR} or ${VAR:-default} are taken from the environment."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_TRACKING_PARAMS = [
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "ysclid",
    "mc_cid", "mc_eid", "igshid", "igsh", "_ga", "_gl", "ref_src", "spm",
]


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


class QueryConfig(BaseModel):
    """A provider-agnostic search query; each provider renders it in its own syntax."""

    model_config = ConfigDict(extra="forbid")

    id: str
    phrases: list[str] = Field(min_length=1)  # exact phrases
    match: Literal["all", "any"] = "all"  # all phrases required, or any of them (spelling variants)
    any: list[str] = []  # plus at least one of these words (context)
    exclude: list[str] = []  # negative phrases: sent as -"…" and also checked in title/snippet
    langs: list[str] = ["pl"]
    site: str | None = None
    providers: list[str] | None = None  # None: every provider that works with queries
    date_window: bool = True  # False: no date filter (search engines have no dates for e.g. t.me pages)


class ProviderConfig(BaseModel):
    """Common provider settings; provider-specific options are extra keys."""

    model_config = ConfigDict(extra="allow")

    type: str
    enabled: bool = True
    run_limit: int | None = None  # max requests per run
    monthly_limit: int | None = None  # max requests per calendar month
    backfill: bool = True  # take part in the initial collection
    backfill_window_days: int = 90  # date window size when slicing history

    def opt(self, key: str, default: Any = None) -> Any:
        return (self.model_extra or {}).get(key, default)


class FiltersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclude_domains: list[str] = []
    exclude_url_prefixes: list[str] = []
    exclude_url_regex: list[str] = []
    exclude_text: list[str] = []  # case-insensitive regexes over title + snippet
    ignore_telegram_chats: list[str] = []  # username without @ or numeric id
    tracking_params: list[str] = DEFAULT_TRACKING_PARAMS


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron: list[str] = ["0 7 * * *"]
    timezone: str = "Europe/Warsaw"
    run_on_start: bool = False


class DailyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_days: int = 3  # search window of a regular run; overlaps are deduplicated


class BackfillConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = 730


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: str = "logs"  # empty: console only
    level: str = "INFO"  # DEBUG also logs duplicates
    retention_days: int = 90  # rotated at midnight


class ClassifierConfig(BaseModel):
    """LLM filter for new links (match / no match / unclear). Classifier-specific options are extra keys."""

    model_config = ConfigDict(extra="allow")

    type: str = "anthropic"
    enabled: bool = False
    batch_size: int = 20  # links per model request
    monthly_limit: int | None = None  # max model requests per month
    subject: str = ""  # who we are looking for: who they are, what they do, their resources, known namesakes
    # Full system prompt override; placeholders {subject} and {reason_language}. Empty: the built-in prompt.
    prompt: str = ""
    hide_no_match: bool = True  # move "no match" links into a collapsed block of the report

    def opt(self, key: str, default: Any = None) -> Any:
        return (self.model_extra or {}).get(key, default)


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = "sqlite:///data/news.db"


class TelegramDeliveryConfig(BaseModel):
    """Sending reports to a Telegram chat via a bot (Bot API)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    bot_token: str = ""  # from @BotFather
    chat_id: str = ""  # group id (negative number); see `news-checker tg-bot-chats`
    max_links: int = 50  # links in the message text; the rest only in the attached file
    include_unclear: bool = True  # list "unclear" links too (separate block)
    attach_html: bool = True  # attach the full HTML report
    max_attempts: int = 5  # how many runs in a row to retry a failed delivery


class ReportsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: str = "reports"
    language: Literal["en", "ru"] = "en"  # language of the HTML report, Telegram message and filter reasons
    title: str | None = None  # default: "New mentions" in the report language
    telegram: TelegramDeliveryConfig = TelegramDeliveryConfig()


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[QueryConfig] = []
    providers: dict[str, ProviderConfig] = {}
    filters: FiltersConfig = FiltersConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    daily: DailyConfig = DailyConfig()
    backfill: BackfillConfig = BackfillConfig()
    storage: StorageConfig = StorageConfig()
    reports: ReportsConfig = ReportsConfig()
    logging: LoggingConfig = LoggingConfig()
    classifier: ClassifierConfig = ClassifierConfig()
    http_timeout: float = 30.0
    user_agent: str = "news-checker"

    base_dir: Path = Path(".")

    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def database_url(self) -> str:
        url = self.storage.url
        prefix = "sqlite:///"
        if url.startswith(prefix) and not url.startswith(prefix + "/"):
            path = self.resolve(url[len(prefix):])
            path.parent.mkdir(parents=True, exist_ok=True)
            return prefix + str(path)
        return url

    @property
    def reports_dir(self) -> Path:
        return self.resolve(self.reports.dir)

    @property
    def report_title(self) -> str:
        from .i18n import t

        return self.reports.title or t(self.reports.language, "title")


def load_settings(path: str | Path) -> Settings:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings = Settings.model_validate(_expand_env(raw))
    settings.base_dir = path.parent
    ids = [q.id for q in settings.queries]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Duplicate query ids: {sorted(dupes)}")
    return settings
