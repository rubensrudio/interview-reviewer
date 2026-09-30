from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import get_settings
from app.knowledge.sources import (
    ApprovedSource,
    load_approved_sources,
    load_configured_sources,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]
VERSIONED_REGISTRY = BACKEND_DIR / "config" / "approved_sources.yaml"

REQUIRED_ENV = {
    "IR_THROTTLE_SECRET": "test-throttle-secret",
    "IR_OIDC_STATE_SECRET": "test-oidc-state-secret",
}


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_know_03_versioned_registry_loads_without_error() -> None:
    sources = load_approved_sources(VERSIONED_REGISTRY)

    domains = {source.domain for source in sources}
    assert {"docs.python.org", "developer.mozilla.org", "kubernetes.io"} <= domains
    for source in sources:
        assert source.urls
        assert source.skill_terms
        assert all(url.startswith("https://") for url in source.urls)


def test_know_03_configured_registry_uses_settings_path() -> None:
    assert load_configured_sources() == load_approved_sources(VERSIONED_REGISTRY)


def test_know_03_valid_registry_returns_approved_sources(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
sources:
  - id: python
    domain: docs.python.org
    urls:
      - https://docs.python.org/3/tutorial/
    skill_terms: [python, asyncio]
""",
    )

    assert load_approved_sources(path) == [
        ApprovedSource(
            id="python",
            domain="docs.python.org",
            urls=["https://docs.python.org/3/tutorial/"],
            skill_terms=["python", "asyncio"],
        )
    ]


def test_know_03_subdomain_of_declared_domain_is_accepted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
sources:
  - id: k8s
    domain: kubernetes.io
    urls: [https://www.kubernetes.io/docs/]
    skill_terms: [kubernetes]
""",
    )

    assert load_approved_sources(path)[0].urls == ["https://www.kubernetes.io/docs/"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/docs/",
        "https://docs.python.org.evil.com/",
        "https://notdocs.python.org/",
        "https://user:pass@docs.python.org/",
    ],
)
def test_know_03_url_outside_declared_domain_raises(tmp_path: Path, url: str) -> None:
    path = _write(
        tmp_path,
        f"""
sources:
  - id: python
    domain: docs.python.org
    urls: ["{url}"]
    skill_terms: [python]
""",
    )

    with pytest.raises(ValueError):
        load_approved_sources(path)


def test_know_03_http_url_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
sources:
  - id: python
    domain: docs.python.org
    urls: ["http://docs.python.org/3/"]
    skill_terms: [python]
""",
    )

    with pytest.raises(ValueError):
        load_approved_sources(path)


@pytest.mark.parametrize(
    "content",
    [
        "",
        "sources: {}",
        "other: []",
        "sources:\n  - id: python\n    domain: docs.python.org\n    urls: []\n"
        "    skill_terms: [python]\n",
        "sources:\n  - id: python\n    domain: docs.python.org\n"
        "    urls: [https://docs.python.org/]\n    skill_terms: []\n",
        "sources:\n  - id: ''\n    domain: docs.python.org\n"
        "    urls: [https://docs.python.org/]\n    skill_terms: [python]\n",
        "sources:\n  - id: python\n    domain: docs.python.org\n"
        "    urls: [https://docs.python.org/]\n    skill_terms: [python]\n"
        "    extra: nope\n",
        "sources:\n  - id: a\n    domain: docs.python.org\n"
        "    urls: [https://docs.python.org/]\n    skill_terms: [python]\n"
        "  - id: a\n    domain: kubernetes.io\n"
        "    urls: [https://kubernetes.io/]\n    skill_terms: [kubernetes]\n",
    ],
)
def test_know_03_malformed_registry_raises(tmp_path: Path, content: str) -> None:
    with pytest.raises(ValueError):
        load_approved_sources(_write(tmp_path, content))
