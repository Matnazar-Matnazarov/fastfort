"""Tests for `FastFortSettings` and its groups.

The settings object is where an unsafe deployment is supposed to become
impossible, so most of these check that a bad configuration is rejected rather
than quietly accepted.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from fastfort.core.exceptions import ImproperlyConfigured
from fastfort.core.settings import (
    AdminSettings,
    FastFortSettings,
    SecuritySettings,
)

GOOD_SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"  # 32 chars


def settings(**kwargs: object) -> FastFortSettings:
    return FastFortSettings(secret_key=GOOD_SECRET, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Secret key
# ---------------------------------------------------------------------------


def test_secret_key_is_required() -> None:
    """There is no default. A shipped default is a guaranteed production incident."""
    with pytest.raises(PydanticValidationError):
        FastFortSettings()  # type: ignore[call-arg]


def test_short_secret_key_is_rejected_with_a_usable_message() -> None:
    with pytest.raises(PydanticValidationError, match="at least 32 characters"):
        FastFortSettings(secret_key="too-short")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "weak",
    [
        "change-this-in-production",
        "CHANGEME-CHANGEME-CHANGEME-CHANGEME",
        "your-secret-key-goes-right-here-ok",
        "fastfort-demo-key-fastfort-demo-key",
    ],
)
def test_placeholder_secret_keys_are_rejected(weak: str) -> None:
    """Padding a documented placeholder to the required length must not help."""
    with pytest.raises(PydanticValidationError, match="placeholder"):
        FastFortSettings(secret_key=weak)  # type: ignore[arg-type]


@pytest.mark.parametrize("low_entropy", ["a" * 40, "1234512345123451234512345123451234"])
def test_long_but_low_entropy_secret_keys_are_rejected(low_entropy: str) -> None:
    """Length alone is not entropy."""
    with pytest.raises(PydanticValidationError, match="distinct characters"):
        FastFortSettings(secret_key=low_entropy)  # type: ignore[arg-type]


def test_secret_key_is_not_shown_in_repr() -> None:
    assert GOOD_SECRET not in repr(settings())


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [("admin", "/admin"), ("/admin/", "/admin"), ("  /back-office  ", "/back-office")],
)
def test_admin_url_is_normalised(given: str, expected: str) -> None:
    assert AdminSettings(url=given).url == expected


def test_admin_url_cannot_be_the_site_root() -> None:
    """Mounting at / would shadow the application it is supposed to administer."""
    with pytest.raises(PydanticValidationError, match="site root"):
        AdminSettings(url="/")


def test_admin_and_auth_urls_must_differ() -> None:
    with pytest.raises(PydanticValidationError, match="both"):
        settings(admin=AdminSettings(url="/auth"), auth_url="/auth")


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_page_size_cannot_exceed_the_maximum() -> None:
    with pytest.raises(PydanticValidationError, match="cannot exceed"):
        AdminSettings(page_size=500, max_page_size=100)


def test_unknown_settings_are_rejected() -> None:
    """A typo in a setting name must fail loudly, not be silently ignored."""
    with pytest.raises(PydanticValidationError):
        AdminSettings(page_sizes=10)  # type: ignore[call-arg]


def test_samesite_none_requires_a_secure_cookie() -> None:
    with pytest.raises(PydanticValidationError, match="requires cookie_secure"):
        SecuritySettings(cookie_samesite="none", cookie_secure=False)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def test_values_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FASTFORT_SECRET_KEY", GOOD_SECRET)
    monkeypatch.setenv("FASTFORT_PROJECT_NAME", "Shop")
    monkeypatch.setenv("FASTFORT_ADMIN__PAGE_SIZE", "50")
    monkeypatch.setenv("FASTFORT_SECURITY__COOKIE_SECURE", "false")

    loaded = FastFortSettings()  # type: ignore[call-arg]

    assert loaded.project_name == "Shop"
    assert loaded.admin.page_size == 50
    assert loaded.security.cookie_secure is False


# ---------------------------------------------------------------------------
# Deployment review
# ---------------------------------------------------------------------------


def test_default_settings_are_production_ready() -> None:
    """The out-of-the-box configuration must pass its own deployment check."""
    assert settings().deployment_issues() == []
    settings().require_production_ready()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"debug": True}, "debug=True"),
        ({"security": SecuritySettings(cookie_secure=False)}, "cookie_secure"),
        ({"security": SecuritySettings(cookie_httponly=False)}, "cookie_httponly"),
        ({"security": SecuritySettings(csrf_enabled=False)}, "csrf_enabled"),
        ({"security": SecuritySettings(security_headers=False)}, "security_headers"),
    ],
)
def test_unsafe_configuration_is_reported(kwargs: dict[str, object], expected: str) -> None:
    issues = settings(**kwargs).deployment_issues()
    assert any(expected in issue for issue in issues), issues


def test_every_deployment_issue_says_what_to_do() -> None:
    """A warning without a remedy just gets ignored."""
    issues = settings(
        debug=True, security=SecuritySettings(cookie_secure=False)
    ).deployment_issues()
    assert len(issues) == 2
    for issue in issues:
        assert len(issue) > 60


def test_require_production_ready_lists_every_problem_at_once() -> None:
    unsafe = settings(debug=True, security=SecuritySettings(cookie_secure=False))
    with pytest.raises(ImproperlyConfigured) as caught:
        unsafe.require_production_ready()

    message = str(caught.value)
    assert "debug=True" in message
    assert "cookie_secure" in message
    assert "How to fix:" in message
