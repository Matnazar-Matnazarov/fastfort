"""Authentication, sessions, tokens and permissions.

Reaches the database only through the backend's adapter protocol, never through
an ORM directly.

Two similarly spelled things live here and mean different things. *Logout* is the
person leaving, which clears the session cookie. *Lockout* is brute-force
protection: after a number of failed attempts an address or an identity is
refused for a growing delay.
"""

from __future__ import annotations

from .csrf import CsrfProtection
from .lockout import InMemoryLockoutStore, Lockout, LockoutState, LockoutStore
from .passwords import hash_password, validate_password, verify_password
from .service import AdminAuth, AuthResult
from .sessions import AdminSession, SessionCodec
from .user_config import UserModelConfig

__all__ = [
    "AdminAuth",
    "AdminSession",
    "AuthResult",
    "CsrfProtection",
    "InMemoryLockoutStore",
    "Lockout",
    "LockoutState",
    "LockoutStore",
    "SessionCodec",
    "UserModelConfig",
    "hash_password",
    "validate_password",
    "verify_password",
]
