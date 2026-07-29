"""Tests for wiring FastFort into a FastAPI application."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from fastfort import FastFort, FastFortSettings
from fastfort.core.exceptions import ConfigurationError, ImproperlyConfigured, RegistrationError

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"


class User:
    """Stand-in for a project's user model, with conventional attribute names."""

    id = 1
    email = "admin@example.com"
    hashed_password = ""
    is_active = True
    is_staff = True
    is_superuser = False


class Product:
    pass


class NoOpUnitOfWork:
    """Enough of a unit of work for a dashboard with nothing registered."""

    async def __aenter__(self) -> NoOpUnitOfWork:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeBackend:
    """A backend that answers questions about models and opens no connection.

    These tests are about wiring, not queries: a real backend would drag the ORM
    into a module whose job is to prove `mount()` works.
    """

    name = "fake"
    dialect = "sqlite"

    def __init__(self, *, supported: tuple[type, ...] | None = None) -> None:
        self._supported = supported

    def supports(self, model: type) -> bool:
        return self._supported is None or model in self._supported

    def unit_of_work(self) -> NoOpUnitOfWork:
        return NoOpUnitOfWork()


@pytest.fixture
def fort() -> FastFort:
    instance = FastFort(
        FastFortSettings(secret_key=SECRET, project_name="Shop"),  # type: ignore[arg-type]
        backend=FakeBackend(),  # type: ignore[arg-type]
    )
    instance.set_user_model(User)
    return instance


# ---------------------------------------------------------------------------
# Configuration errors
# ---------------------------------------------------------------------------


def test_missing_backend_says_how_to_supply_one() -> None:
    bare = FastFort(FastFortSettings(secret_key=SECRET))  # type: ignore[arg-type]
    with pytest.raises(ImproperlyConfigured, match="SQLAlchemyBackend"):
        _ = bare.backend


def test_missing_user_model_says_how_to_supply_one() -> None:
    bare = FastFort(FastFortSettings(secret_key=SECRET))  # type: ignore[arg-type]
    with pytest.raises(ImproperlyConfigured, match="set_user_model"):
        _ = bare.user_config


def test_mount_refuses_an_incomplete_configuration() -> None:
    bare = FastFort(FastFortSettings(secret_key=SECRET))  # type: ignore[arg-type]
    with pytest.raises(ImproperlyConfigured):
        bare.mount(FastAPI())


def test_mounting_twice_is_an_error(fort: FastFort) -> None:
    fort.mount(FastAPI())
    with pytest.raises(ConfigurationError, match="already mounted"):
        fort.mount(FastAPI())


def test_backend_cannot_change_after_mount(fort: FastFort) -> None:
    fort.mount(FastAPI())
    with pytest.raises(ConfigurationError, match="after mount"):
        fort.set_backend(FakeBackend())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# User model detection
# ---------------------------------------------------------------------------


def test_conventional_field_names_are_detected(fort: FastFort) -> None:
    config = fort.user_config
    assert config.identity_field == "email"
    assert config.password_field == "hashed_password"
    assert config.superuser_field == "is_superuser"


def test_unconventional_field_names_can_be_declared() -> None:
    class LegacyUser:
        id = 1
        login = "admin"
        pwd_hash = ""
        enabled = True
        is_admin = True

    instance = FastFort(FastFortSettings(secret_key=SECRET))  # type: ignore[arg-type]
    config = instance.set_user_model(
        LegacyUser,
        identity_field="login",
        password_field="pwd_hash",
        active_field="enabled",
        staff_field="is_admin",
        superuser_field="is_admin",
    )
    assert config.identity_of(LegacyUser()) == "admin"
    assert config.is_active(LegacyUser())


def test_an_unmappable_user_model_names_what_was_tried() -> None:
    class Anonymous:
        pass

    instance = FastFort(FastFortSettings(secret_key=SECRET))  # type: ignore[arg-type]
    with pytest.raises(ImproperlyConfigured) as caught:
        instance.set_user_model(Anonymous)

    message = str(caught.value)
    assert "identity_field" in message
    assert "tried:" in message
    assert "set_user_model" in message


def test_a_named_field_that_does_not_exist_is_reported() -> None:
    instance = FastFort(FastFortSettings(secret_key=SECRET))  # type: ignore[arg-type]
    with pytest.raises(ImproperlyConfigured, match="no attribute for"):
        instance.set_user_model(User, identity_field="nickname")


def test_a_superuser_counts_as_staff(fort: FastFort) -> None:
    """Requiring both flags locks people out of their own installation."""

    class Owner:
        is_staff = False
        is_superuser = True

    assert fort.user_config.is_staff(Owner())


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_check_is_clean_for_a_complete_configuration(fort: FastFort) -> None:
    fort.register(Product, object(), key="shop.product")
    assert fort.check() == []


def test_check_reports_an_empty_registry(fort: FastFort) -> None:
    assert any("No models are registered" in issue for issue in fort.check())


def test_check_reports_a_model_the_backend_cannot_handle() -> None:
    instance = FastFort(
        FastFortSettings(secret_key=SECRET),  # type: ignore[arg-type]
        backend=FakeBackend(supported=()),  # type: ignore[arg-type]
    )
    instance.set_user_model(User)
    instance.register(Product, object(), key="shop.product")

    assert any("cannot handle" in issue for issue in instance.check())


def test_check_deploy_includes_settings_issues() -> None:
    instance = FastFort(
        FastFortSettings(secret_key=SECRET, debug=True),  # type: ignore[arg-type]
        backend=FakeBackend(),  # type: ignore[arg-type]
    )
    instance.set_user_model(User)
    instance.register(Product, object(), key="shop.product")

    assert instance.check() == []
    assert any("debug=True" in issue for issue in instance.check(deploy=True))


# ---------------------------------------------------------------------------
# Mounting
# ---------------------------------------------------------------------------


async def test_mounting_with_an_empty_registry_still_serves_the_admin(
    fort: FastFort, client_for: Any
) -> None:
    """An empty admin is a warning, not a failure; the site must still come up."""
    app = FastAPI()
    fort.mount(app)

    response = await (await client_for(app)).get("/admin/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Shop" in response.text
    # An empty admin explains itself rather than showing a blank page.
    assert "No models are registered" in response.text


async def test_the_stylesheet_is_reachable_from_a_mounted_admin(
    fort: FastFort, client_for: Any
) -> None:
    app = FastAPI()
    fort.mount(app)

    response = await (await client_for(app)).get("/admin/static/fastfort.css")

    assert response.status_code == 200
    assert "--ff-h:" in response.text


async def test_the_admin_honours_a_custom_url(fort: FastFort, client_for: Any) -> None:
    fort.settings.admin.url = "/back-office"
    app = FastAPI()
    fort.mount(app)

    client = await client_for(app)
    assert (await client.get("/back-office/")).status_code == 200
    assert (await client.get("/back-office/static/fastfort.css")).status_code == 200
    assert (await client.get("/admin/")).status_code == 404


# ---------------------------------------------------------------------------
# Module discovery
# ---------------------------------------------------------------------------


def test_include_admin_reports_a_missing_module(fort: FastFort) -> None:
    with pytest.raises(RegistrationError, match="does not exist"):
        fort.include_admin("nowhere.at.all")


def test_include_admin_does_not_hide_errors_inside_the_module(
    fort: FastFort, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing dependency inside admin.py is a real bug, not a typo in the path."""
    package = tmp_path / "brokenapp"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "admin.py").write_text("import a_library_that_is_not_installed\n")

    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "brokenapp", raising=False)

    with pytest.raises(ModuleNotFoundError, match="a_library_that_is_not_installed"):
        fort.include_admin("brokenapp.admin")


def test_autodiscover_imports_every_admin_module(
    fort: FastFort, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "shopapp"
    (root / "products").mkdir(parents=True)
    (root / "orders").mkdir()
    (root / "__init__.py").write_text("")
    (root / "products" / "__init__.py").write_text("")
    (root / "orders" / "__init__.py").write_text("")

    body = textwrap.dedent("""
        import shopapp
        shopapp.loaded = getattr(shopapp, "loaded", [])
        shopapp.loaded.append(__name__)
    """)
    (root / "products" / "admin.py").write_text(body)
    (root / "orders" / "admin.py").write_text(body)
    # A module that is not named `admin` must be left alone.
    (root / "products" / "views.py").write_text("raise AssertionError('should not be imported')")

    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [n for n in sys.modules if n.startswith("shopapp")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    discovered = fort.autodiscover("shopapp")

    assert set(discovered) == {"shopapp.products.admin", "shopapp.orders.admin"}
    assert len(sys.modules["shopapp"].loaded) == 2  # type: ignore[attr-defined]


def test_autodiscover_reports_a_missing_package(fort: FastFort) -> None:
    with pytest.raises(RegistrationError, match="does not exist"):
        fort.autodiscover("no_such_package")


def test_repr_summarises_the_installation(fort: FastFort) -> None:
    assert "not mounted" in repr(fort)
    fort.mount(FastAPI())
    assert "mounted" in repr(fort)
