"""Registry of approved knowledge sources (KNOW-03, KNOW-04).

The registry is a versioned YAML file maintained by the project. URLs are never
accepted from users: only the entries declared here may be fetched.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from app.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[2]

_ALLOWED_KEYS = frozenset({"id", "domain", "urls", "skill_terms"})


@dataclass(frozen=True)
class ApprovedSource:
    id: str
    domain: str
    urls: list[str]
    skill_terms: list[str]


def load_approved_sources(path: Path) -> list[ApprovedSource]:
    """Load and validate the approved sources registry.

    Raises `ValueError` when the registry is malformed, when a URL is not https
    or when a URL does not belong to the domain declared by its source.
    """
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
        raise ValueError("approved sources registry must define a 'sources' list")

    sources: list[ApprovedSource] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(document["sources"]):
        source = _parse_source(raw, index)
        if source.id in seen_ids:
            raise ValueError(f"duplicate approved source id: {source.id}")
        seen_ids.add(source.id)
        sources.append(source)
    return sources


def load_configured_sources() -> list[ApprovedSource]:
    """Load the registry at `Settings.knowledge_sources_path` (relative to the backend dir)."""
    path = Path(get_settings().knowledge_sources_path)
    if not path.is_absolute():
        path = BACKEND_DIR / path
    return load_approved_sources(path)


def _parse_source(raw: Any, index: int) -> ApprovedSource:
    if not isinstance(raw, dict):
        raise ValueError(f"source #{index} must be a mapping")
    unknown = set(raw) - _ALLOWED_KEYS
    missing = _ALLOWED_KEYS - set(raw)
    if unknown or missing:
        raise ValueError(
            f"source #{index} has invalid keys "
            f"(missing: {sorted(missing)}, unknown: {sorted(map(str, unknown))})"
        )

    source_id = _non_empty_str(raw["id"], f"source #{index} id")
    domain = _non_empty_str(raw["domain"], f"source {source_id} domain").lower()
    urls = _non_empty_str_list(raw["urls"], f"source {source_id} urls")
    skill_terms = _non_empty_str_list(raw["skill_terms"], f"source {source_id} skill_terms")
    for url in urls:
        _validate_url(url, domain, source_id)
    return ApprovedSource(id=source_id, domain=domain, urls=urls, skill_terms=skill_terms)


def _validate_url(url: str, domain: str, source_id: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"source {source_id}: URL must use https: {url}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"source {source_id}: URL must not carry credentials: {url}")
    host = (parts.hostname or "").lower()
    if host != domain and not host.endswith(f".{domain}"):
        raise ValueError(f"source {source_id}: URL outside domain {domain}: {url}")


def _non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _non_empty_str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    return [_non_empty_str(item, field) for item in value]
