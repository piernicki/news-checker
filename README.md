# news-checker

Monitoring of online mentions of a person and their projects. news-checker searches the web and public Telegram
channels on a schedule, drops namesakes and noise with rules and an LLM filter, and sends the new mentions to a
Telegram group, with a full HTML report attached.

It was built to answer one question every morning: *who mentioned or quoted this person since yesterday, and where?*

## Features

- **Several sources**: Google (via [Serper](https://serper.dev)), [Brave Search](https://brave.com/search/api/),
  [Yandex Search API](https://yandex.cloud/en/services/search-api), and posts inside public Telegram channels
  (via the `t.me/s/<channel>` web preview, no account needed).
- **Deduplication** by canonical URL (tracking parameters, `www`, trailing slashes and fragments stripped).
- **Filters**: excluded domains, URL prefixes and regexes, a text stop-list of namesakes, ignored Telegram chats.
  Excluded domains are also sent to search engines as `-site:` so they do not take result slots.
- **LLM filter** (Claude Haiku by default): every new link gets a verdict *match / no match / unclear* with a short
  reason. The prompt and the description of the person are configurable.
- **Reports**: an HTML file per run and a Telegram message (English or Russian), with automatic retries of failed
  deliveries.
- **Initial collection** (`backfill`) over a configurable period, then daily runs on a cron schedule.
- **Spending limits** per provider and month; usage is tracked in the database.
- **Structured log** (`RUN`, `REQ`, `NEW`, `SKIP`, `CLASSIFY`, `DELIVERY`, ...) that is easy to grep.

## How it works

```
schedule ─▶ providers (Serper, Brave, Yandex) ─▶ normalize URL ─▶ filters ─▶ dedup ─▶ SQLite / Postgres
                 │                                                               ▲
                 └── t.me links ─▶ tracked channels ─▶ telegram_web ─────────────┘
                                                                                 │
                         HTML report + Telegram message ◀── LLM filter ◀── new links
```

Each run searches the last `daily.lookback_days` days (with overlap; duplicates are dropped), classifies the new
links, writes `reports/<YYYY-MM-DD_HHMM>.html` and sends the "match" and "unclear" links to Telegram. If nothing new
was found, no report is created and nothing is sent.

## Quick start (Docker)

Create a working directory with three files:

```
news-checker/
├── config.yaml          # from config.example.yaml
├── .env                 # from .env.example: API keys and the bot token
└── docker-compose.yml   # from this repository
```

```bash
cp config.example.yaml config.yaml   # describe the person, queries, filters
cp .env.example .env                 # add API keys
# in docker-compose.yml set `image:` to <dockerhub-user>/news-checker:<version>

docker compose run --rm news-checker check      # validate the config, see active providers
docker compose run --rm news-checker backfill   # initial collection (backfill.days)
docker compose up -d                            # daily runs on schedule.cron
```

`data/` (SQLite database), `reports/` and `logs/` are created in the working directory. On Linux the container
runs as uid 1000, so these directories must be writable by it: `sudo chown -R 1000:1000 data reports logs`.

## Local run (uv)

```bash
uv sync
cp config.example.yaml config.yaml && cp .env.example .env
uv run news-checker check
uv run news-checker run
```

The config is read from `--config` or `NEWS_CHECKER_CONFIG` (default `./config.yaml`); `.env` next to it is loaded
automatically.

## Configuration

See [config.example.yaml](config.example.yaml); every option is commented.

| Section | What it controls |
|---|---|
| `queries` | provider-agnostic queries: exact phrases, `match: any` for spelling variants, context words, negative phrases, languages, `site:`, which providers use them, `date_window` |
| `providers` | providers by name; `type`, keys, `monthly_limit`, `run_limit`, provider options |
| `filters` | `exclude_domains`, `exclude_url_prefixes`, `exclude_url_regex`, `exclude_text`, `ignore_telegram_chats` |
| `classifier` | the LLM filter: `subject` (who the person is, their resources, known namesakes), model, batch size, optional `prompt` |
| `schedule` | cron expressions and time zone for `serve` |
| `daily`, `backfill` | look-back window of a regular run; length of the initial collection |
| `reports` | output directory, `language` (`en`/`ru`), title, Telegram delivery |
| `storage`, `logging` | database URL, log directory, level and retention |

Secrets are referenced as `${VAR}` and read from the environment or `.env`:

| Variable | Used by |
|---|---|
| `SERPER_API_KEY` | `serper` provider |
| `BRAVE_API_KEY` | `brave` provider |
| `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` | `yandex` provider |
| `ANTHROPIC_API_KEY` | LLM filter |
| `TG_BOT_TOKEN`, `TG_CHAT_ID` | Telegram delivery |

A provider without its key is skipped, so you can start with any subset.

## Commands

| Command | What it does |
|---|---|
| `check` | validate the config; show providers, calls per run and this month's usage, delivery and classifier status |
| `run [--providers a,b] [--no-report]` | one regular run: collect, classify, report, deliver |
| `backfill [--days N]` | initial collection over `backfill.days`, split into date windows |
| `serve [--cron EXPR] [--providers a,b]` | run on `schedule.cron` (the default command of the image) |
| `classify [--force] [--limit N]` | classify links without a verdict; `--force` re-applies filter rules and the model to all links |
| `report [--since YYYY-MM-DD \| --all]` | rebuild an HTML report without marking anything |
| `send [--report NAME]` | send a report (default: the latest) to Telegram |
| `tg-bot-chats` | show the bot and the `chat_id` of the groups it was added to |
| `--version` | print the version |

## Providers

| Type | Key | Notes |
|---|---|---|
| `serper` | `SERPER_API_KEY` | Google results; date window via `tbs`; 1 credit per page of 10 results (2,500 free credits on sign-up) |
| `brave` | `BRAVE_API_KEY` | Brave's own index; date window via `freshness`; monthly free credit. It ignores exact phrases for some queries, so restrict it with `queries[].providers` |
| `yandex` | `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` | Yandex Search API v2; `mode: deferred` is cheaper, but new folders often have no quota for it, `mode: sync` works right away |
| `telegram_web` | — | searches inside public channels via `t.me/s/<channel>?q=<term>` and stores exact post links. Channels come from `channels` and from any `t.me` link found by other providers; a new channel is searched through its full history |

Search engines have no dates for `t.me` pages, so a query with `site: t.me` needs `date_window: false`.

To add a provider, create a module in `src/news_checker/providers/`, subclass `Provider`, decorate it with
`@register("type")` and import it in `providers/__init__.py`.

## LLM filter

`classifier.subject` describes the person: who they are, what they do, their own resources and known namesakes.
The model answers with strict JSON (structured outputs) and one verdict per link:

- `match`: material by another author that mentions or quotes the person or their projects;
- `no_match`: namesakes, accidental matches, the person's own posts, comments and profiles, the name only in lists
  of likes or participants, profile pages;
- `unclear`: shown in the report with a tag for manual review.

"No match" links are collapsed at the bottom of the HTML report and are not sent to Telegram. Reasons are written in
`reports.language`. The built-in prompt can be replaced with `classifier.prompt` (placeholders `{subject}` and
`{reason_language}`). A batch of 20 links costs a fraction of a cent with Claude Haiku.

Without `ANTHROPIC_API_KEY` or with `classifier.enabled: false` the filter is skipped and all new links are reported.

## Telegram delivery

1. Create a bot with [@BotFather](https://t.me/BotFather) and put the token into `TG_BOT_TOKEN`.
2. Add the bot to a group and post any message there.
3. Run `news-checker tg-bot-chats` and put the group's `chat_id` (a negative number) into `TG_CHAT_ID`.
4. Test with `news-checker send` (sends the latest report).

Messages list up to `max_links` links and are split at Telegram's 4,096-character limit; the full HTML report is
attached. A failed delivery is retried on the next runs (`max_attempts`).

## Logs

`logs/news-checker.log`, rotated at midnight and kept for `logging.retention_days` days:

```
… RUN start id=5 kind=daily providers=serper,brave,telegram_web
… REQ  provider=serper query=name-ru lang=ru window=2026-11-13..2026-11-16 results=10 new=2 dup=8 filtered=0 sec=1.6
… NEW  provider=serper query=name-ru type=web domain=example.com published=2026-11-16 url=https://… title='…'
… SKIP provider=serper query=surname-context-ru reason=exclude_text:… url=… title='…'
… CLASSIFY end match=2 no_match=10 errors=0 tokens_in=4676 tokens_out=637 cost_usd=0.0008
… DELIVERY ok channel=telegram report=2026-11-16_0700 links=12 1 message(s) + file
```

## Versioning and releases

Versions follow [SemVer](https://semver.org) and come from git tags; there is no version string to edit by hand.

```bash
git tag v0.2.0
git push origin v0.2.0
```

The [CI workflow](.github/workflows/ci.yml) runs the tests on every push and pull request. On a `vX.Y.Z` tag it
builds a multi-arch image (`linux/amd64`, `linux/arm64`) and pushes it to Docker Hub as `X.Y.Z`, `X.Y`, `X`
(from 1.0 on) and `latest`; every push to `main` is published as `edge`. Pin a minor version (`0.2`) on servers to
get fixes without surprises.

Repository settings needed for publishing: secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access
token), and optionally the variable `DOCKERHUB_IMAGE` (default `<DOCKERHUB_USERNAME>/news-checker`).

`news-checker --version` and the image label `org.opencontainers.image.version` show the version. Builds between
tags get a development version such as `0.2.1.dev3+g1a2b3c4`.

## Development

```bash
uv sync
uv run pytest -q
docker build --build-arg VERSION=0.0.0+local -t news-checker .
```

Tests do not touch the network: HTTP providers, the Telegram Bot API and the Anthropic client are mocked.

## Limitations

- Search engines show only part of what exists; social networks are covered only as far as they are indexed.
- Instagram snippets in search results often mix text from neighbouring posts, so Instagram is better excluded.
- Public Telegram channels are searched only once they are known (from the config or from search results);
  private chats are not covered.
- The database schema is created automatically; there are no Alembic migrations yet.

## License

[MIT](LICENSE)
