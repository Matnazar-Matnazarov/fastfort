"""Password hashing and policy.

Argon2id, because it is the only widely available algorithm with a memory cost:
bcrypt's work factor is CPU-only, so a GPU farm attacks it far more cheaply than
it attacks Argon2 at the same wall-clock cost.

Verification is deliberately constant-cost in one respect: `verify` is also called
for identities that do not exist, against a fixed dummy hash, so a stranger cannot
tell registered addresses from unregistered ones by timing the response.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from fastfort.core.exceptions import ValidationError

if TYPE_CHECKING:
    from fastfort.core.settings import AuthSettings

__all__ = [
    "DUMMY_HASH",
    "hash_password",
    "needs_rehash",
    "validate_password",
    "verify_password",
]

#: Passwords that appear at the top of every breach corpus. A full 10k list would
#: add a data file to the wheel for little gain over rejecting the obvious ones
#: plus a length floor; projects with stricter needs supply their own validator.
_COMMON = frozenset(
    {
        "password",
        "password1",
        "password123",
        "123456",
        "1234567890",
        "12345678",
        "qwerty",
        "qwerty123",
        "letmein",
        "welcome",
        "admin",
        "admin123",
        "administrator",
        "iloveyou",
        "monkey",
        "dragon",
        "sunshine",
        "princess",
        "football",
        "baseball",
        "changeme",
        "secret",
        "abc123",
        "passw0rd",
        "trustno1",
    }
)


@lru_cache(maxsize=1)
def _hasher() -> PasswordHash:
    """One hasher per process: constructing it is not free and it is stateless."""
    return PasswordHash((Argon2Hasher(),))


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A real hash of a value nobody knows, used to burn time on a missing user."""
    return _hasher().hash("fastfort-timing-equaliser")


#: Exposed so callers can be explicit about what they are comparing against.
DUMMY_HASH = ""


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    """Check a password, taking the same time whether or not the hash is usable.

    A missing or malformed hash still costs one Argon2 verification, because
    returning early on it would leak which identities exist.
    """
    candidate = hashed or _dummy_hash()
    try:
        return _hasher().verify(password, candidate)
    except Exception:
        # A hash produced by another library, or a corrupted column. Neither is a
        # reason to leak timing, so the cost is paid before returning False.
        _hasher().verify(password, _dummy_hash())
        return False


def needs_rehash(hashed: str) -> bool:
    """Whether a stored hash was made with weaker parameters than we now use.

    Called after a successful login so that raising the cost parameters upgrades
    existing accounts as people sign in, rather than never.
    """
    try:
        return _hasher().verify_and_update("not-the-password", hashed)[1] is not None
    except Exception:
        return True


def validate_password(password: str, settings: AuthSettings, *, identity: str = "") -> None:
    """Raise `ValidationError` if a password is too weak to accept.

    Length first, because it is the single strongest factor, then the obvious
    rejections. Deliberately not a composition rule ("one uppercase, one digit"):
    those push people towards `Password1!` and measurably reduce entropy.
    """
    problems: list[str] = []

    if len(password) < settings.password_min_length:
        problems.append(
            f"Use at least {settings.password_min_length} characters "
            f"(this one has {len(password)})."
        )

    lowered = password.lower()
    if settings.password_reject_common and lowered in _COMMON:
        problems.append("This is one of the most commonly used passwords.")

    if identity and lowered and lowered in identity.lower():
        problems.append("The password must not be part of your username or email.")

    if len(set(password)) < 5:
        problems.append("Use more than a handful of distinct characters.")

    if problems:
        raise ValidationError(
            "That password cannot be used.",
            field_errors={"password": problems},
        )
