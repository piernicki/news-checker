# syntax=docker/dockerfile:1

# ---- build: resolve dependencies and install the project into /opt/app/.venv ----
FROM python:3.12-slim AS build

ARG VERSION=0.0.0
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

WORKDIR /opt/app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime: only the virtualenv, non-root user ----
FROM python:3.12-slim

ARG VERSION=0.0.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="news-checker" \
      org.opencontainers.image.description="Monitoring of online mentions: search engines, Telegram channels, LLM filtering, Telegram reports" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT"

RUN useradd --create-home --uid 1000 app
COPY --from=build /opt/app/.venv /opt/app/.venv

ENV PATH="/opt/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    NEWS_CHECKER_CONFIG=/work/config.yaml
WORKDIR /work
USER app
ENTRYPOINT ["news-checker"]
CMD ["serve"]
