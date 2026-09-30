"""Password hashing (Argon2id) and password policy (LAC-24)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

PolicyViolation = Literal["too_short", "common"]

MIN_PASSWORD_LENGTH = 8

_COMMON_PASSWORDS_FILE = Path(__file__).with_name("common_passwords.txt")

# argon2-cffi defaults to Argon2id with RFC 9106 low-memory parameters.
_hasher = PasswordHasher()


def hash_password(raw: str) -> str:
    """Return an Argon2id encoded hash of ``raw`` with a random salt."""
    return _hasher.hash(raw)


def verify_password(stored_hash: str, raw: str) -> bool:
    """Check ``raw`` against ``stored_hash``; malformed hashes never match."""
    try:
        return _hasher.verify(stored_hash, raw)
    except (VerificationError, InvalidHashError):
        return False


def password_policy_violations(raw: str) -> list[PolicyViolation]:
    """Return the policy rules violated by ``raw`` (empty when acceptable)."""
    violations: list[PolicyViolation] = []
    if len(raw) < MIN_PASSWORD_LENGTH:
        violations.append("too_short")
    if raw.casefold() in _common_passwords():
        violations.append("common")
    return violations


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    with _COMMON_PASSWORDS_FILE.open(encoding="utf-8") as handle:
        return frozenset(line.strip().casefold() for line in handle if line.strip())
