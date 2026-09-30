"""Plain-text e-mail templates (CT-12, AUTH-09).

Templates are pure functions: they never log, persist or send anything. The one-time link
is only embedded in the returned body, which is handed straight to ``send_email`` (DA-12).
"""

from dataclasses import dataclass

GOOGLE_ONLY_ACCOUNT_MESSAGE = (
    "Your account uses Google sign-in. Please sign in with Google or recover access "
    "through Google."
)


@dataclass(frozen=True, slots=True)
class EmailContent:
    """A ready-to-send e-mail. ``template`` is a safe identifier used in logs."""

    template: str
    subject: str
    body: str


def verification_email(link: str) -> EmailContent:
    body = (
        "Hello,\n\n"
        "Please confirm your e-mail address to activate your Interview Reviewer account "
        "by opening the link below:\n\n"
        f"{link}\n\n"
        "The link can be used only once and expires after a while. If you did not create "
        "an account, you can ignore this message.\n"
    )
    return EmailContent(
        template="verification",
        subject="Verify your e-mail address",
        body=body,
    )


def password_reset_email(link: str) -> EmailContent:
    body = (
        "Hello,\n\n"
        "We received a request to reset the password of your Interview Reviewer account. "
        "To choose a new password, open the link below:\n\n"
        f"{link}\n\n"
        "The link can be used only once and expires after a while. If you did not request "
        "a password reset, you can ignore this message; your password stays unchanged.\n"
    )
    return EmailContent(
        template="password_reset",
        subject="Reset your password",
        body=body,
    )


def google_only_account_email() -> EmailContent:
    body = (
        "Hello,\n\n"
        "We received a request to reset the password of your Interview Reviewer account.\n\n"
        f"{GOOGLE_ONLY_ACCOUNT_MESSAGE}\n\n"
        "No password was created or changed. If you did not make this request, you can "
        "ignore this message.\n"
    )
    return EmailContent(
        template="google_only_account",
        subject="How to access your account",
        body=body,
    )
