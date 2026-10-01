"""Unit tests for the SMTP e-mail sender and templates (CT-12, AUTH-09, AUTH-92)."""

import json
import logging
import smtplib
from collections.abc import Iterator
from email.message import EmailMessage
from typing import Any

import pytest

from app.config import get_settings
from app.email.sender import (
    EmailContent,
    google_only_account_email,
    password_reset_email,
    send_email,
    verification_email,
)
from app.logging_setup import JsonFormatter, RedactionFilter

RECIPIENT = "candidate@example.com"
VERIFY_LINK = "http://localhost:4200/verify-email?token=abc123SECRETtoken"
RESET_LINK = "http://localhost:4200/reset-password?token=zzz999SECRETtoken"


class FakeSMTP:
    """Stand-in for ``smtplib.SMTP`` that records what would be sent."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, message: EmailMessage) -> dict[str, Any]:
        self.sent.append(message)
        return {}


@pytest.fixture(autouse=True)
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    monkeypatch.setenv("IR_THROTTLE_SECRET", "test-throttle-secret")
    monkeypatch.setenv("IR_OIDC_STATE_SECRET", "test-oidc-state-secret")
    monkeypatch.setenv("IR_SMTP_HOST", "smtp.test")
    monkeypatch.setenv("IR_SMTP_PORT", "2525")
    monkeypatch.delenv("IR_SMTP_STARTTLS", raising=False)
    monkeypatch.delenv("IR_SMTP_USER", raising=False)
    monkeypatch.delenv("IR_SMTP_PASSWORD", raising=False)
    get_settings.cache_clear()
    FakeSMTP.instances = []
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def _body(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain",))
    assert part is not None
    content = part.get_content()
    assert isinstance(content, str)
    return content


def test_send_email_returns_true_and_body_contains_link(fake_smtp: type[FakeSMTP]) -> None:
    assert send_email(RECIPIENT, verification_email(VERIFY_LINK)) is True

    (client,) = fake_smtp.instances
    assert (client.host, client.port) == ("smtp.test", 2525)
    assert client.timeout == 10
    assert client.started_tls is False
    assert client.login_args is None
    (message,) = client.sent
    assert message["To"] == RECIPIENT
    assert message["From"] == get_settings().smtp_from
    assert message["Subject"] == verification_email(VERIFY_LINK).subject
    assert VERIFY_LINK in _body(message)


def test_send_email_uses_starttls_and_login_when_configured(
    fake_smtp: type[FakeSMTP], settings_env: pytest.MonkeyPatch
) -> None:
    settings_env.setenv("IR_SMTP_STARTTLS", "true")
    settings_env.setenv("IR_SMTP_USER", "mailer")
    settings_env.setenv("IR_SMTP_PASSWORD", "smtp-pass")
    get_settings.cache_clear()

    assert send_email(RECIPIENT, password_reset_email(RESET_LINK)) is True

    (client,) = fake_smtp.instances
    assert client.started_tls is True
    assert client.login_args == ("mailer", "smtp-pass")
    assert RESET_LINK in _body(client.sent[0])


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("refused"),
        TimeoutError("timed out"),
        smtplib.SMTPRecipientsRefused({RECIPIENT: (550, b"no such user")}),
        smtplib.SMTPServerDisconnected("gone"),
    ],
)
def test_send_email_returns_false_on_smtp_failure_without_leaking(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    def failing_smtp(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(smtplib, "SMTP", failing_smtp)
    caplog.set_level(logging.DEBUG)

    assert send_email(RECIPIENT, verification_email(VERIFY_LINK)) is False

    events = [getattr(r, "event_fields", {}) for r in caplog.records]
    assert any(
        fields.get("event") == "email.send_failed" and fields.get("template") == "verification"
        for fields in events
    )
    # Raw records, before any redaction: neither the link nor the address is ever passed.
    raw = caplog.text + json.dumps([vars(r) for r in caplog.records], default=str)
    assert VERIFY_LINK not in raw
    assert "abc123SECRETtoken" not in raw
    assert RECIPIENT not in raw


def test_send_email_failure_log_line_is_formatted_without_link_or_email(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refused(*args: object, **kwargs: object) -> None:
        raise ConnectionRefusedError(f"cannot reach {RECIPIENT} {RESET_LINK}")

    monkeypatch.setattr(smtplib, "SMTP", refused)
    caplog.set_level(logging.INFO)

    assert send_email(RECIPIENT, password_reset_email(RESET_LINK)) is False

    formatter = JsonFormatter()
    redaction = RedactionFilter()
    lines = []
    for record in caplog.records:
        redaction.filter(record)
        lines.append(formatter.format(record))
    output = "\n".join(lines)
    assert "email.send_failed" in output
    assert "password_reset" in output
    assert "zzz999SECRETtoken" not in output
    assert RECIPIENT not in output


@pytest.mark.parametrize("to", ["", "not-an-address", "a@b.com\r\nBcc: evil@x.com"])
def test_send_email_rejects_invalid_recipient_without_connecting(
    fake_smtp: type[FakeSMTP], to: str
) -> None:
    assert send_email(to, verification_email(VERIFY_LINK)) is False
    assert fake_smtp.instances == []


def test_templates_are_english_and_contain_links() -> None:
    verification = verification_email(VERIFY_LINK)
    reset = password_reset_email(RESET_LINK)

    assert isinstance(verification, EmailContent)
    assert verification.template == "verification"
    assert VERIFY_LINK in verification.body
    assert "verify" in verification.subject.lower()
    assert reset.template == "password_reset"
    assert RESET_LINK in reset.body
    assert "password" in reset.subject.lower()


def test_google_only_account_email_uses_spec_message() -> None:
    message = google_only_account_email()

    assert message.template == "google_only_account"
    assert "Your account uses Google sign-in" in message.body
    assert (
        "Your account uses Google sign-in. Please sign in with Google or recover access "
        "through Google." in message.body
    )
    assert "http" not in message.body
