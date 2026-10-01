"""Synchronous SMTP e-mail sender (CT-12, DA-12, AUTH-92).

E-mails are sent inline with ``smtplib`` (no queue), so the one-time link never has to be
stored anywhere. Any SMTP or network failure is swallowed and reported as ``False`` so the
caller can keep the account or request and offer a resend. Failure logs carry only the
template name and the error type: never the recipient, the link or the exception message.
"""

import re
import smtplib
import ssl
from email.message import EmailMessage

from app.config import get_settings
from app.email.templates import (
    EmailContent,
    google_only_account_email,
    password_reset_email,
    verification_email,
)
from app.observability import log_event

__all__ = [
    "SMTP_TIMEOUT_SECONDS",
    "EmailContent",
    "google_only_account_email",
    "password_reset_email",
    "send_email",
    "verification_email",
]

SMTP_TIMEOUT_SECONDS = 10

# Deliberately strict: a single address, no whitespace or control characters (blocks header
# injection through CR/LF), one "@" and a dotted domain.
_ADDRESS = re.compile(r"^[^\s@<>,;\"]+@[^\s@<>,;\"]+\.[^\s@<>,;\"]+$")
_MAX_ADDRESS_LENGTH = 254


def _is_valid_address(address: str) -> bool:
    return len(address) <= _MAX_ADDRESS_LENGTH and _ADDRESS.fullmatch(address) is not None


def _build_message(sender: str, to: str, message: EmailContent) -> EmailMessage:
    email = EmailMessage()
    email["From"] = sender
    email["To"] = to
    email["Subject"] = message.subject
    email.set_content(message.body)
    return email


def send_email(to: str, message: EmailContent) -> bool:
    """Send ``message`` to ``to`` synchronously. Return False if it could not be sent."""
    if not _is_valid_address(to):
        log_event("email.send_failed", template=message.template, reason="invalid_recipient")
        return False

    settings = get_settings()
    try:
        email = _build_message(settings.smtp_from, to, message)
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
        ) as client:
            if settings.smtp_starttls:
                client.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                client.login(settings.smtp_user, settings.smtp_password.get_secret_value())
            client.send_message(email)
    except (smtplib.SMTPException, OSError, ValueError) as error:
        # Only the error type is logged: SMTP replies and messages may echo the address.
        log_event(
            "email.send_failed",
            template=message.template,
            reason="smtp_error",
            error_type=type(error).__name__,
        )
        return False

    log_event("email.sent", template=message.template)
    return True
