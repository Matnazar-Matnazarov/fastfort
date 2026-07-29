"""FastFort exception hierarchy.

Guiding rule: every error says **what happened**, **where**, and **what to do next**.
A user should be able to fix the problem from the message alone, without opening
the documentation.
"""

from __future__ import annotations

__all__ = [
    "AdapterError",
    "ConfigurationError",
    "FastFortError",
    "ImproperlyConfigured",
    "ObjectNotFound",
    "PermissionDenied",
    "RegistrationError",
    "SchemaVersionError",
    "SecurityError",
    "StaleObjectError",
    "ValidationError",
]


class FastFortError(Exception):
    """Root of every exception raised by FastFort.

    When ``hint`` is given it is appended to the message on its own line, so the
    user is always pointed at a concrete next step.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.message = message
        self.hint = hint
        super().__init__(self._render())

    def _render(self) -> str:
        if self.hint:
            return f"{self.message}\n\nHow to fix: {self.hint}"
        return self.message


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigurationError(FastFortError):
    """FastFort is wired up incorrectly -- raised during application start-up."""


class ImproperlyConfigured(ConfigurationError):
    """A required setting is missing or holds an invalid value."""


class RegistrationError(ConfigurationError):
    """Something is wrong with a model or ModelAdmin registration."""


class SchemaVersionError(ConfigurationError):
    """The FastFort schema in the database does not match the installed package."""


# ---------------------------------------------------------------------------
# ORM adapters
# ---------------------------------------------------------------------------


class AdapterError(FastFortError):
    """The ORM adapter could not carry out the requested operation."""


class ObjectNotFound(AdapterError):
    """The requested object does not exist, or the user is not allowed to see it.

    Note: a permission failure is deliberately reported as "not found" so that the
    existence of an object never leaks to a user who may not access it.
    """


class StaleObjectError(AdapterError):
    """The object was modified by someone else after you loaded it."""


# ---------------------------------------------------------------------------
# Request and data handling
# ---------------------------------------------------------------------------


class ValidationError(FastFortError):
    """User supplied data failed validation.

    ``field_errors`` maps a field name to its messages; ``non_field_errors`` holds
    messages that do not belong to any single field.
    """

    def __init__(
        self,
        message: str = "The submitted data is invalid.",
        *,
        field_errors: dict[str, list[str]] | None = None,
        non_field_errors: list[str] | None = None,
        hint: str | None = None,
    ) -> None:
        self.field_errors: dict[str, list[str]] = field_errors or {}
        self.non_field_errors: list[str] = non_field_errors or []
        super().__init__(message, hint=hint)


class PermissionDenied(FastFortError):
    """The user is not allowed to perform this action."""


class SecurityError(FastFortError):
    """A security control rejected the request (CSRF, lockout, token reuse)."""
