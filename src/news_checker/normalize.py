"""URL normalization for deduplication and source type detection."""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from .config import DEFAULT_TRACKING_PARAMS

SOCIAL_DOMAINS = {
    "linkedin.com", "threads.com", "threads.net", "facebook.com", "fb.com", "instagram.com",
    "x.com", "twitter.com", "vk.com", "youtube.com", "tiktok.com", "reddit.com", "ok.ru",
}
TELEGRAM_DOMAINS = {"t.me", "telegram.me", "telegram.dog"}
_REDIRECTORS = {("google.com", "/url"), ("www.google.com", "/url")}


def unwrap_redirect(url: str) -> str:
    """Unwrap redirect links like google.com/url?url=..."""
    parts = urlsplit(url)
    if (parts.netloc.lower(), parts.path) in _REDIRECTORS:
        params = dict(parse_qsl(parts.query))
        target = params.get("url") or params.get("q")
        if target and target.startswith("http"):
            return unquote(target)
    return url


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def canonical_url(url: str, tracking_params: list[str] | None = None) -> str:
    """Canonical form: https, lowercase host without www, no tracking parameters or fragment, remaining
    parameters sorted, no trailing slash."""
    url = unwrap_redirect(url.strip())
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"

    path = parts.path or "/"
    if host in TELEGRAM_DOMAINS:
        netloc = "t.me"
        if path.startswith("/s/"):  # web preview t.me/s/<name>/<id>
            path = path[2:]
        segments = path.split("/")
        if len(segments) > 1:  # Telegram usernames are case-insensitive
            segments[1] = segments[1].lower()
        path = "/".join(segments)
    if len(path) > 1:
        path = path.rstrip("/")

    drop = {p.lower() for p in (tracking_params if tracking_params is not None else DEFAULT_TRACKING_PARAMS)}
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in drop
    ]
    query.sort()
    return urlunsplit(("https", netloc, path, urlencode(query, doseq=True), ""))


def strip_scheme(url: str) -> str:
    """'https://a.b/c?d' -> 'a.b/c?d', for prefix comparison."""
    return url.split("://", 1)[-1]


def _matches_domain(host: str, domains: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify(host: str) -> str:
    if host in TELEGRAM_DOMAINS:
        return "telegram"
    if _matches_domain(host, SOCIAL_DOMAINS):
        return "social"
    return "web"
