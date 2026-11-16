"""Storage on SQLAlchemy Core. Switching to Postgres only needs a different storage.url."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import (
    JSON, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text,
    UniqueConstraint, create_engine, func, insert, inspect, select, update,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

mentions = Table(
    "mentions", metadata,
    Column("id", Integer, primary_key=True),
    Column("url", String(2048), nullable=False, unique=True),  # canonical URL
    Column("original_url", String(4096), nullable=False),
    Column("domain", String(255), nullable=False, index=True),
    Column("source_type", String(16), nullable=False, index=True),
    Column("title", Text, nullable=False, default=""),
    Column("snippet", Text, nullable=False, default=""),
    Column("published_at", DateTime(timezone=True)),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, index=True),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("first_run_kind", String(16), nullable=False, default="daily"),
    Column("reported_at", DateTime(timezone=True), index=True),
    Column("report_name", String(255)),
    # classifier verdict: match | no_match | unclear; NULL means not classified yet
    Column("verdict", String(16), index=True),
    Column("verdict_reason", Text),
    Column("classifier", String(64)),
    Column("classified_at", DateTime(timezone=True)),
)

tg_channels = Table(
    "tg_channels", metadata,
    Column("username", String(128), primary_key=True),  # lowercase, without @
    Column("source_url", String(4096)),  # where the channel was discovered
    Column("discovered_at", DateTime(timezone=True), nullable=False),
    Column("last_searched_at", DateTime(timezone=True)),  # NULL: never searched, the first search covers full history
)

deliveries = Table(
    "deliveries", metadata,
    Column("id", Integer, primary_key=True),
    Column("report_name", String(255), nullable=False, index=True),
    Column("channel", String(32), nullable=False),  # e.g. telegram
    Column("kind", String(16), nullable=False, default="daily"),
    Column("file_path", String(1024)),
    Column("status", String(16), nullable=False),  # sent | failed | skipped
    Column("attempts", Integer, nullable=False, default=0),
    Column("last_error", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("report_name", "channel", name="uq_delivery"),
)

# Columns added after the first schema version; created in existing databases (SQLite and Postgres).
_ADDED_COLUMNS = {"mentions": ["verdict", "verdict_reason", "classifier", "classified_at"]}

hits = Table(
    "hits", metadata,
    Column("id", Integer, primary_key=True),
    Column("mention_id", ForeignKey("mentions.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("provider", String(64), nullable=False),
    Column("query_id", String(128), nullable=False, default=""),
    Column("seen_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("mention_id", "provider", "query_id", name="uq_hit"),
)

runs = Table(
    "runs", metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("stats", JSON),
    Column("errors", JSON),
)

usage = Table(
    "usage", metadata,
    Column("id", Integer, primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("period", String(7), nullable=False),  # YYYY-MM
    Column("requests", Integer, nullable=False, default=0),
    UniqueConstraint("provider", "period", name="uq_usage"),
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class StoredMention:
    id: int
    url: str
    original_url: str
    domain: str
    source_type: str
    title: str
    snippet: str
    published_at: datetime | None
    first_seen_at: datetime
    providers: list[str]
    queries: list[str]
    verdict: str | None = None
    verdict_reason: str | None = None


class Store:
    def __init__(self, url: str):
        self.engine: Engine = create_engine(url, future=True)
        metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Minimal migration: ALTER TABLE ADD COLUMN for new nullable columns (to be replaced by Alembic)."""
        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table_name, names in _ADDED_COLUMNS.items():
                existing = {c["name"] for c in insp.get_columns(table_name)}
                table = metadata.tables[table_name]
                for name in names:
                    if name not in existing:
                        col_type = table.c[name].type.compile(dialect=self.engine.dialect)
                        conn.exec_driver_sql(f'ALTER TABLE {table_name} ADD COLUMN {name} {col_type}')

    # --- mentions ------------------------------------------------------------

    def upsert_mention(
        self, *, url: str, original_url: str, domain: str, source_type: str, title: str,
        snippet: str, published_at: datetime | None, provider: str, query_id: str, run_kind: str,
    ) -> bool:
        """Store a mention and the hit that found it. Returns True if the URL is new."""
        now = utcnow()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(mentions.c.id, mentions.c.title, mentions.c.snippet, mentions.c.published_at)
                .where(mentions.c.url == url)
            ).first()
            is_new = row is None
            if is_new:
                mention_id = conn.execute(
                    insert(mentions).values(
                        url=url, original_url=original_url, domain=domain, source_type=source_type,
                        title=title or "", snippet=snippet or "", published_at=published_at,
                        first_seen_at=now, last_seen_at=now, first_run_kind=run_kind,
                    )
                ).inserted_primary_key[0]
            else:
                mention_id = row.id
                values: dict = {"last_seen_at": now}
                # fill empty fields with what another provider found
                if not row.title and title:
                    values["title"] = title
                if len(snippet or "") > len(row.snippet or ""):
                    values["snippet"] = snippet
                if row.published_at is None and published_at is not None:
                    values["published_at"] = published_at
                conn.execute(update(mentions).where(mentions.c.id == mention_id).values(**values))
            exists = conn.execute(
                select(hits.c.id).where(
                    hits.c.mention_id == mention_id, hits.c.provider == provider, hits.c.query_id == query_id
                )
            ).first()
            if not exists:
                conn.execute(
                    insert(hits).values(mention_id=mention_id, provider=provider, query_id=query_id, seen_at=now)
                )
        return is_new

    def _load(self, where) -> list[StoredMention]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(mentions).where(where).order_by(mentions.c.first_seen_at)).all()
            if not rows:
                return []
            ids = [r.id for r in rows]
            hit_rows = conn.execute(
                select(hits.c.mention_id, hits.c.provider, hits.c.query_id).where(hits.c.mention_id.in_(ids))
            ).all()
        by_id: dict[int, tuple[set, set]] = {i: (set(), set()) for i in ids}
        for h in hit_rows:
            by_id[h.mention_id][0].add(h.provider)
            if h.query_id:
                by_id[h.mention_id][1].add(h.query_id)
        return [
            StoredMention(
                id=r.id, url=r.url, original_url=r.original_url, domain=r.domain, source_type=r.source_type,
                title=r.title, snippet=r.snippet, published_at=_aware(r.published_at),
                first_seen_at=_aware(r.first_seen_at), providers=sorted(by_id[r.id][0]),
                queries=sorted(by_id[r.id][1]), verdict=r.verdict, verdict_reason=r.verdict_reason,
            )
            for r in rows
        ]

    def unreported(self) -> list[StoredMention]:
        return self._load(mentions.c.reported_at.is_(None))

    def seen_since(self, since: datetime) -> list[StoredMention]:
        return self._load(mentions.c.first_seen_at >= since)

    def unclassified(self, only_unreported: bool = True) -> list[StoredMention]:
        cond = mentions.c.verdict.is_(None)
        if only_unreported:
            cond = cond & mentions.c.reported_at.is_(None)
        return self._load(cond)

    def all_mentions(self) -> list[StoredMention]:
        return self._load(mentions.c.id.isnot(None))

    def set_verdict(self, mention_id: int, *, verdict: str, reason: str, classifier: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(mentions).where(mentions.c.id == mention_id).values(
                verdict=verdict, verdict_reason=reason, classifier=classifier, classified_at=utcnow(),
            ))

    def mark_reported(self, ids: list[int], report_name: str) -> None:
        if not ids:
            return
        with self.engine.begin() as conn:
            conn.execute(
                update(mentions).where(mentions.c.id.in_(ids)).values(reported_at=utcnow(), report_name=report_name)
            )

    def count_mentions(self) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(mentions)).scalar_one()

    # --- tracked Telegram channels -----------------------------------------

    def add_tg_channel(self, username: str, source_url: str | None = None) -> bool:
        """Track a channel. Returns True if it is new."""
        username = username.lower().lstrip("@")
        with self.engine.begin() as conn:
            if conn.execute(select(tg_channels.c.username).where(tg_channels.c.username == username)).first():
                return False
            conn.execute(insert(tg_channels).values(username=username, source_url=source_url, discovered_at=utcnow()))
        return True

    def list_tg_channels(self) -> dict[str, datetime | None]:
        """username → last_searched_at."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(tg_channels.c.username, tg_channels.c.last_searched_at)).all()
        return {r.username: _aware(r.last_searched_at) for r in rows}

    def mark_tg_searched(self, username: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(tg_channels).where(tg_channels.c.username == username.lower())
                         .values(last_searched_at=utcnow()))

    # --- reports and delivery ------------------------------------------------

    def mentions_by_report(self, report_name: str) -> list[StoredMention]:
        return self._load(mentions.c.report_name == report_name)

    def last_report_name(self) -> str | None:
        with self.engine.connect() as conn:
            return conn.execute(
                select(mentions.c.report_name).where(mentions.c.report_name.isnot(None))
                .order_by(mentions.c.reported_at.desc()).limit(1)
            ).scalar()

    def record_delivery(self, report_name: str, channel: str, *, status: str, kind: str = "daily",
                        file_path: str | None = None, error: str | None = None) -> None:
        with self.engine.begin() as conn:
            row = conn.execute(select(deliveries.c.id, deliveries.c.attempts).where(
                deliveries.c.report_name == report_name, deliveries.c.channel == channel)).first()
            values = dict(status=status, last_error=error, updated_at=utcnow())
            if file_path:
                values["file_path"] = file_path
            if row is None:
                conn.execute(insert(deliveries).values(report_name=report_name, channel=channel, kind=kind,
                                                       attempts=1, **values))
            else:
                conn.execute(update(deliveries).where(deliveries.c.id == row.id)
                             .values(attempts=row.attempts + 1, **values))

    def failed_deliveries(self, channel: str, max_attempts: int) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(deliveries).where(
                deliveries.c.channel == channel, deliveries.c.status == "failed",
                deliveries.c.attempts < max_attempts).order_by(deliveries.c.id)).all()
        return [dict(r._mapping) for r in rows]

    # --- runs ----------------------------------------------------------------

    def start_run(self, kind: str) -> int:
        with self.engine.begin() as conn:
            return conn.execute(insert(runs).values(kind=kind, started_at=utcnow())).inserted_primary_key[0]

    def finish_run(self, run_id: int, stats: dict, errors: list) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(runs).where(runs.c.id == run_id).values(
                    finished_at=utcnow(), stats=json.loads(json.dumps(stats, default=str)), errors=errors
                )
            )

    # --- request usage -------------------------------------------------------

    def usage_get(self, provider: str, period: str) -> int:
        with self.engine.connect() as conn:
            value = conn.execute(
                select(usage.c.requests).where(usage.c.provider == provider, usage.c.period == period)
            ).scalar()
        return value or 0

    def usage_add(self, provider: str, period: str, n: int = 1) -> None:
        with self.engine.begin() as conn:
            updated = conn.execute(
                update(usage).where(usage.c.provider == provider, usage.c.period == period)
                .values(requests=usage.c.requests + n)
            ).rowcount
            if not updated:
                conn.execute(insert(usage).values(provider=provider, period=period, requests=n))
