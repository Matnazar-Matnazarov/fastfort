"""Authentication, sessions, tokens and permissions.

Reaches the database only through the backend's adapter protocol, never through
an ORM directly.

Two similarly spelled things live here and mean different things. *Logout* is the
person leaving, which clears the session cookie. *Lockout* is brute-force
protection: after a number of failed attempts an address or an identity is
refused for a growing delay.
"""

from __future__ import annotations

from .addresses import client_address
from .api import bearer_user, build_auth_router
from .csrf import CsrfProtection
from .devices import Device, read_device
from .lockout import InMemoryLockoutStore, Lockout, LockoutState, LockoutStore
from .passwords import hash_password, validate_password, verify_password
from .service import AdminAuth, AuthResult
from .sessions import AdminSession, SessionCodec
from .signins import SignIn, SignInRecorder
from .tokens import (
    InMemoryRefreshTokenStore,
    RefreshRecord,
    RefreshTokenStore,
    TokenPair,
    TokenService,
)
from .user_config import UserModelConfig

__all__ = [
    "AdminAuth",
    "AdminSession",
    "AuthResult",
    "CsrfProtection",
    "Device",
    "InMemoryLockoutStore",
    "InMemoryRefreshTokenStore",
    "Lockout",
    "LockoutState",
    "LockoutStore",
    "RefreshRecord",
    "RefreshTokenStore",
    "SessionCodec",
    "SignIn",
    "SignInRecorder",
    "TokenPair",
    "TokenService",
    "UserModelConfig",
    "bearer_user",
    "build_auth_router",
    "client_address",
    "hash_password",
    "read_device",
    "validate_password",
    "verify_password",
]
