"""Tests for the exception hierarchy and its message contract."""

from __future__ import annotations

import pytest

from fastfort.core.exceptions import (
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

ALL_ERRORS = [
    AdapterError,
    ConfigurationError,
    ImproperlyConfigured,
    ObjectNotFound,
    PermissionDenied,
    RegistrationError,
    SchemaVersionError,
    SecurityError,
    StaleObjectError,
    ValidationError,
]


@pytest.mark.parametrize("error_class", ALL_ERRORS)
def test_every_error_inherits_from_root(error_class: type[FastFortError]) -> None:
    """A single `except FastFortError` must be enough to catch anything we raise."""
    assert issubclass(error_class, FastFortError)


def test_hint_is_appended_to_message() -> None:
    """The hint is what turns an error into an actionable instruction."""
    error = ImproperlyConfigured(
        "`secret_key` is not configured.",
        hint="Set the `FASTFORT_SECRET_KEY` environment variable.",
    )
    text = str(error)
    assert "`secret_key` is not configured." in text
    assert "FASTFORT_SECRET_KEY" in text
    assert error.hint is not None


def test_message_without_hint_is_unchanged() -> None:
    assert str(FastFortError("Something went wrong.")) == "Something went wrong."


def test_configuration_errors_share_a_base() -> None:
    """Start-up failures can be caught with one `except ConfigurationError`."""
    for error_class in (ImproperlyConfigured, RegistrationError, SchemaVersionError):
        assert issubclass(error_class, ConfigurationError)


def test_adapter_errors_share_a_base() -> None:
    for error_class in (ObjectNotFound, StaleObjectError):
        assert issubclass(error_class, AdapterError)


def test_validation_error_collects_field_errors() -> None:
    error = ValidationError(field_errors={"email": ["Enter a valid address."]})
    assert error.field_errors == {"email": ["Enter a valid address."]}
    assert error.non_field_errors == []


def test_validation_error_defaults_are_not_shared() -> None:
    """Mutable defaults must never be shared between instances."""
    first = ValidationError()
    second = ValidationError()
    first.field_errors["a"] = ["x"]
    first.non_field_errors.append("y")
    assert second.field_errors == {}
    assert second.non_field_errors == []


def test_not_found_and_permission_denied_stay_distinct() -> None:
    """Hiding an object from an unauthorised user is reported as "not found".

    That behaviour is deliberate, which is exactly why the two exception types must
    not be substitutable for one another.
    """
    assert not issubclass(ObjectNotFound, PermissionDenied)
    assert not issubclass(PermissionDenied, ObjectNotFound)
