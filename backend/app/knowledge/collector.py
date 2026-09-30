"""Knowledge base collector (KNOW-03, KNOW-04, KNOW-07).

Run with ``python -m app.knowledge.collector``. It takes no arguments: the only URLs it fetches
are the ones declared in the approved sources registry (``app.knowledge.sources``), requested
verbatim (no query string added), with a fixed User-Agent, without cookies, proxies from the
environment or redirects. Each page is reduced to its title and visible text, and stored (or
updated) in ``knowledge_items`` keyed by URL.

Collected content is untrusted public text: it is stored as plain text only. Nothing in it is
interpreted, and links it contains are never followed.
"""

import logging
import re
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import session_scope
from app.knowledge.sources import ApprovedSource, load_configured_sources
from app.logging_setup import configure_logging, log_event, timed
from app.models.knowledge import EXCERPT_MAX_LENGTH, KnowledgeItem

USER_AGENT = "InterviewReviewerKnowledgeCollector/1.0"
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TITLE_MAX_LENGTH = 300
_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_NON_TEXT_TAGS = ("script", "style", "noscript", "template", "iframe", "svg", "object", "embed")
_WHITESPACE_RE = re.compile(r"\s+")
# NUL is dropped (PostgreSQL text cannot hold it; UTF-16 read as UTF-8 is full of it); other
# C0/C1 control characters become spaces. Tab, newline and carriage return are whitespace.
_NUL_RE = re.compile("\x00")
_CONTROL_RE = re.compile("[\x01-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class CollectionResult:
    stored: int
    skipped: int


class _SkipUrl(Exception):
    """The URL cannot be collected; ``reason`` is a fixed code safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """HTTP client for collection: no redirects, no env proxies, no cookies, fixed User-Agent."""
    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        # A policy with an empty domain allowlist neither stores nor sends any cookie.
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
    )


def extract_page(html: str, fallback_title: str) -> tuple[str, str]:
    """Return ``(title, visible_text)`` of an HTML document, whitespace-normalized."""
    # The stdlib parser does not resolve external entities or fetch anything.
    soup = BeautifulSoup(_NUL_RE.sub("", html), "html.parser")
    title_tag = soup.find("title")
    title = _normalize(title_tag.get_text(" ")) if title_tag is not None else ""
    for tag in soup(["title", *_NON_TEXT_TAGS]):
        tag.decompose()
    text = _normalize(soup.get_text(" "))
    return (title or fallback_title)[:TITLE_MAX_LENGTH], text


def collect(
    db: Session,
    sources: Sequence[ApprovedSource],
    client: httpx.Client,
    clock: Clock = _utc_now,
) -> CollectionResult:
    """Fetch every registry URL and upsert it into ``knowledge_items``. Does not commit."""
    stored = skipped = 0
    for source in sources:
        source_stored = 0
        for url in source.urls:
            try:
                title, text = _fetch(client, source, url)
            except _SkipUrl as skip:
                skipped += 1
                log_event(
                    "knowledge.collect_skipped",
                    source_id=source.id,
                    host=_host(url),
                    reason=skip.reason,
                )
                continue
            try:
                # A savepoint per URL: a storage error discards only this item, not the run.
                with db.begin_nested():
                    _upsert(db, source, url, title, text[:EXCERPT_MAX_LENGTH], clock())
            except SQLAlchemyError:
                skipped += 1
                # The exception carries page content in its parameters: never log it.
                log_event(
                    "knowledge.collect_skipped",
                    source_id=source.id,
                    host=_host(url),
                    reason="store_error",
                )
                continue
            source_stored += 1
        stored += source_stored
        log_event("knowledge.collected", source_id=source.id, items=source_stored)
    db.flush()
    return CollectionResult(stored=stored, skipped=skipped)


def _fetch(client: httpx.Client, source: ApprovedSource, url: str) -> tuple[str, str]:
    request_url = _allowed_url(source, url)
    try:
        with timed("knowledge.fetch", source_id=source.id):
            with client.stream("GET", request_url) as response:
                if response.is_redirect:
                    raise _SkipUrl("redirect")
                if response.status_code != 200:
                    raise _SkipUrl("http_status")
                content_type = response.headers.get("content-type", "")
                if content_type.split(";", 1)[0].strip().lower() not in _HTML_CONTENT_TYPES:
                    raise _SkipUrl("content_type")
                body = _read_limited(response)
                encoding = response.charset_encoding or "utf-8"
    except httpx.HTTPError as error:
        raise _SkipUrl("network") from error

    try:
        html = body.decode(encoding, errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")
    title, text = extract_page(html, fallback_title=url)
    if not text:
        raise _SkipUrl("empty")
    return title, text


def _allowed_url(source: ApprovedSource, url: str) -> httpx.URL:
    """Re-check egress rules right before the request (defense in depth over the registry)."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as error:
        raise _SkipUrl("invalid_url") from error
    host = (parsed.host or "").lower().rstrip(".")
    domain = source.domain.lower().rstrip(".")
    in_domain = host == domain or host.endswith(f".{domain}")
    if parsed.scheme != "https" or parsed.userinfo or not in_domain:
        raise _SkipUrl("not_allowed")
    return parsed


def _read_limited(response: httpx.Response) -> bytes:
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise _SkipUrl("too_large")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise _SkipUrl("too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _upsert(
    db: Session,
    source: ApprovedSource,
    url: str,
    title: str,
    excerpt: str,
    collected_at: datetime,
) -> None:
    values = {
        "source_id": source.id,
        "title": title,
        "collected_at": collected_at,
        "excerpt": excerpt,
        "skill_terms": list(source.skill_terms),
    }
    statement = insert(KnowledgeItem).values(id=uuid.uuid4(), url=url, **values)
    db.execute(
        statement.on_conflict_do_update(index_elements=[KnowledgeItem.url], set_=values)
    )


def _normalize(text: str) -> str:
    text = _CONTROL_RE.sub(" ", _NUL_RE.sub("", text))
    return _WHITESPACE_RE.sub(" ", text).strip()


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host
    except httpx.InvalidURL:
        return "invalid"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Accepts no arguments, so no user data can reach a request (KNOW-04)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        print("usage: python -m app.knowledge.collector (takes no arguments)", file=sys.stderr)
        return 2
    configure_logging()
    # httpx logs every request URL at INFO; collection events are logged by this module only.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    sources = load_configured_sources()
    with build_client() as client, session_scope() as db:
        result = collect(db, sources, client)
    log_event("knowledge.collect_finished", stored=result.stored, skipped=result.skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
