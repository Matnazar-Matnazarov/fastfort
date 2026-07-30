"""FastFort -- batteries-included authentication and admin framework for FastAPI.

This module is the public API surface. Anything not exported here is private and
may change between minor releases; see CONTRIBUTING.md for the stability policy.
"""

from __future__ import annotations

from . import admin
from ._version import SCHEMA_VERSION, __version__
from .core.app import FastFort
from .core.exceptions import (
    ConfigurationError,
    FastFortError,
    ImproperlyConfigured,
    ObjectNotFound,
    PermissionDenied,
    ValidationError,
)
from .core.hooks import Hook
from .core.settings import (
    AdminSettings,
    AuthSettings,
    FastFortSettings,
    SecuritySettings,
    UISettings,
)

__all__ = [
    "SCHEMA_VERSION",
    "AdminSettings",
    "AuthSettings",
    "ConfigurationError",
    "FastFort",
    "FastFortError",
    "FastFortSettings",
    "Hook",
    "ImproperlyConfigured",
    "ObjectNotFound",
    "PermissionDenied",
    "SecuritySettings",
    "UISettings",
    "ValidationError",
    "__version__",
    "admin",
]
