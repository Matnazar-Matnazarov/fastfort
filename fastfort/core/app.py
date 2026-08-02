"""The `FastFort` object: configuration, registry and mounting.

There is one way to set FastFort up. A shorter "convenience" constructor would
double the number of code paths that have to stay correct, so the terse form
lives in `fastfort init`, which writes a working project, rather than in a second
signature here.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from types import ModuleType
from typing import TYPE_CHECKING, Any

from fastfort.auth.user_config import UserModelConfig

from .exceptions import ConfigurationError, ImproperlyConfigured, RegistrationError
from .hooks import HookRegistry
from .registry import AdminRegistry, RegistryEntry, default_model_key
from .settings import FastFortSettings

if TYPE_CHECKING:
    from fastapi import FastAPI

    from fastfort.orm.base import Backend

__all__ = ["FastFort"]


class FastFort:
    """A configured FastFort installation, ready to be mounted onto an app."""

    def __init__(
        self,
        settings: FastFortSettings,
        *,
        backend: Backend | None = None,
    ) -> None:
        self.settings = settings
        self.registry: AdminRegistry[Any] = AdminRegistry()
        self.hooks = HookRegistry()
        self._backend = backend
        self._teach_backend_the_registry()
        self._user_config: UserModelConfig | None = None
        self._mounted_on: FastAPI | None = None
        #: Set by the admin router when it is built. Exposed so a project can
        #: reach `fort.auth.set_password(user, ...)` from a CLI or a seed script.
        self.auth: Any = None

    def __repr__(self) -> str:
        state = "mounted" if self._mounted_on is not None else "not mounted"
        return f"<FastFort {self.settings.project_name!r}: {len(self.registry)} model(s), {state}>"

    # -- wiring -------------------------------------------------------------

    @property
    def backend(self) -> Backend:
        if self._backend is None:
            raise ImproperlyConfigured(
                "No ORM backend is configured.",
                hint=(
                    "Pass one when constructing FastFort, for example: "
                    "FastFort(settings, backend=SQLAlchemyBackend(session_factory=..., base=Base))."
                ),
            )
        return self._backend

    def set_backend(self, backend: Backend) -> None:
        if self._mounted_on is not None:
            raise ConfigurationError(
                "The backend cannot be changed after mount().",
                hint="Configure FastFort fully before calling mount().",
            )
        self._backend = backend
        self._teach_backend_the_registry()

    def _teach_backend_the_registry(self) -> None:
        """Let the backend name a relation's target by its *registered* key.

        Introspection derives a key from the model's module by default, so a
        `Product.category` pointing at a model registered as `shop.category`
        came back as `test_api.category` -- a key nothing could look up. Every
        feature that resolves a relation to the admin behind it was therefore
        quietly doing nothing: the autocomplete fell back to guessing which
        columns to search, and the buttons beside a foreign key never appeared.

        Resolved through the registry when the target is registered, and by the
        backend's own rule when it is not, because pointing at a model that has
        no admin page of its own is perfectly normal.
        """
        backend = self._backend
        setter = getattr(backend, "set_key_resolver", None)
        if setter is None:
            return

        def resolve(model: type) -> str:
            entry = self.registry.get(model)
            return entry.key if entry is not None else default_model_key(model)

        setter(resolve)

    @property
    def user_config(self) -> UserModelConfig:
        if self._user_config is None:
            raise ImproperlyConfigured(
                "No user model has been configured.",
                hint="Call fort.set_user_model(User) before mount().",
            )
        return self._user_config

    @property
    def has_user_model(self) -> bool:
        return self._user_config is not None

    def set_user_model(self, model: type, **fields: str) -> UserModelConfig:
        """Tell FastFort which class represents a user.

        Field names that match the usual conventions are detected; anything named
        differently is passed explicitly::

            fort.set_user_model(User, identity_field="login", password_field="pwd")
        """
        self._user_config = UserModelConfig.detect(model, **fields)
        return self._user_config

    # -- registration -------------------------------------------------------

    def register(self, model: type, admin: Any, *, key: str | None = None) -> RegistryEntry[Any]:
        """Register a model together with the admin object describing it."""
        return self.registry.register(model, admin, key=key)

    def _attach_pending(self) -> int:
        """Attach anything `@admin.register` buffered at import time.

        Drained rather than read, so importing the same module twice cannot
        register the same model twice.
        """
        from fastfort.admin.decorators import take_pending

        attached = 0
        for item in take_pending():
            self.registry.register(item.model, item.admin, key=item.key)
            attached += 1
        return attached

    def include_admin(self, *module_paths: str) -> tuple[ModuleType, ...]:
        """Import modules so their registration decorators run.

        The explicit counterpart to `autodiscover`: nothing is guessed, and an
        import error is reported against the module that was asked for rather
        than swallowed.
        """
        modules: list[ModuleType] = []
        for path in module_paths:
            try:
                modules.append(importlib.import_module(path))
            except ModuleNotFoundError as exc:
                # A missing dependency *inside* the module is a real error; only a
                # missing module of the requested name is a configuration mistake.
                if exc.name == path or (exc.name and path.startswith(f"{exc.name}.")):
                    raise RegistrationError(
                        f"Cannot import admin module {path!r}: it does not exist.",
                        hint="Check the dotted path, and that the package is importable.",
                    ) from exc
                raise
        self._attach_pending()
        return tuple(modules)

    def autodiscover(self, *packages: str, module_name: str = "admin") -> tuple[str, ...]:
        """Import every ``<package>/**/admin.py`` and return what was imported.

        Convenience, not magic: the return value names each module that was
        loaded, and ``fastfort check`` prints it, so what happened is always
        visible. Modules that raise during import are not suppressed.
        """
        discovered: list[str] = []

        for package_name in packages:
            try:
                package = importlib.import_module(package_name)
            except ModuleNotFoundError as exc:
                raise RegistrationError(
                    f"Cannot autodiscover in {package_name!r}: the package does not exist.",
                    hint="Pass the importable package name, for example fort.autodiscover('app').",
                ) from exc

            direct = f"{package_name}.{module_name}"
            if _module_exists(direct):
                importlib.import_module(direct)
                discovered.append(direct)

            for path in getattr(package, "__path__", ()):
                for info in pkgutil.walk_packages([path], prefix=f"{package_name}."):
                    if info.name.rsplit(".", 1)[-1] != module_name:
                        continue
                    importlib.import_module(info.name)
                    discovered.append(info.name)

        self._attach_pending()
        return tuple(dict.fromkeys(discovered))

    # -- diagnostics --------------------------------------------------------

    def check(self, *, deploy: bool = False) -> list[str]:
        """Return everything that looks wrong, most severe first.

        Collecting problems instead of raising on the first one means a single
        run tells you everything to fix.
        """
        issues: list[str] = []

        if self._backend is None:
            issues.append("No ORM backend is configured; pass one to FastFort(...).")
        if self._user_config is None:
            issues.append("No user model is configured; call fort.set_user_model(User).")

        if self._backend is not None:
            unsupported = [
                f"{entry.model.__name__} ({entry.key})"
                for entry in self.registry
                if not self._backend.supports(entry.model)
            ]
            if unsupported:
                issues.append(
                    f"The {self._backend.name} backend cannot handle: {', '.join(unsupported)}."
                )

        if not len(self.registry):
            issues.append(
                "No models are registered, so the admin will be empty. "
                "Did you forget fort.autodiscover(...) or fort.include_admin(...)?"
            )

        if deploy:
            issues.extend(self.settings.deployment_issues())

        return issues

    # -- mounting -----------------------------------------------------------

    def mount(self, app: FastAPI) -> None:
        """Attach the admin and auth routes to `app`.

        Called once, after everything is registered. Configuration problems that
        would only surface on a later request are raised here instead.
        """
        if self._mounted_on is not None:
            raise ConfigurationError(
                "This FastFort instance is already mounted.",
                hint="Create a separate FastFort instance per application.",
            )

        # A project that imported its admin modules directly, without going
        # through include_admin or autodiscover, still gets its registrations.
        self._attach_pending()

        # Accessing the properties raises with the actionable message.
        _ = self.backend
        _ = self.user_config

        blocking = [issue for issue in self.check() if "No models are registered" not in issue]
        if blocking:
            listed = "\n".join(f"  - {issue}" for issue in blocking)
            raise ImproperlyConfigured(f"FastFort cannot be mounted:\n{listed}")

        from fastfort.admin.security import LoginRequired, SecurityHeadersMiddleware

        app.include_router(self._build_router(), prefix=self.settings.admin.url)

        # The gate raises; this turns it into the redirect. Registered on the app
        # because exception handlers are not a router-level concern in Starlette.
        async def _login_required(_: Any, exc: Exception) -> Any:
            assert isinstance(exc, LoginRequired)
            return exc.to_response()

        app.add_exception_handler(LoginRequired, _login_required)
        app.add_middleware(SecurityHeadersMiddleware, settings=self.settings)

        self._mounted_on = app

    def _build_router(self) -> Any:
        """Build the admin router.

        Imported here rather than at module scope: `fastfort.admin` sits above
        `fastfort.core`, and a top-level import would invert the layering.
        """
        from fastfort.admin.site import build_admin_router

        return build_admin_router(self)


def _module_exists(dotted_path: str) -> bool:
    """Whether a module can be located without importing it."""
    try:
        return importlib.util.find_spec(dotted_path) is not None
    except (ImportError, AttributeError, ValueError):
        return False
