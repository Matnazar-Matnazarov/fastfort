"""Kernel: settings, registry, exceptions, hooks and application lifecycle.

This package knows nothing about any ORM (enforced by `tests/test_architecture.py`).
"""

from __future__ import annotations

from .app import FastFort
from .exceptions import (
    AdapterError,
    ConfigurationError,
    FastFortError,
    ImproperlyConfigured,
    ObjectNotFound,
    PermissionDenied,
    RegistrationError,
    SchemaVersionError,
    SecurityError,
    StaleObjectError,
    ValidationError,
)
from .hooks import Hook, HookRegistry
from .registry import AdminRegistry, RegistryEntry, default_model_key
from .settings import (
    AdminSettings,
    AuthSettings,
    FastFortSettings,
    SecuritySettings,
    UISettings,
)

__all__ = [
    "AdapterError",
    "AdminRegistry",
    "AdminSettings",
    "AuthSettings",
    "ConfigurationError",
    "FastFort",
    "FastFortError",
    "FastFortSettings",
    "Hook",
    "HookRegistry",
    "ImproperlyConfigured",
    "ObjectNotFound",
    "PermissionDenied",
    "RegistrationError",
    "RegistryEntry",
    "SchemaVersionError",
    "SecurityError",
    "SecuritySettings",
    "StaleObjectError",
    "UISettings",
    "ValidationError",
    "default_model_key",
]
