"""The mapping from a model class to the admin that describes it.

A registry key such as ``shop.product`` appears in URLs, permission names and
audit records, so it has to stay stable across restarts and be derivable without
the developer having to declare it. It is derived from the model's module and
class name, and can always be overridden when the derivation is wrong or two
models collide.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

from .exceptions import RegistrationError

__all__ = ["AdminRegistry", "RegistryEntry", "default_model_key"]

AdminT = TypeVar("AdminT")

#: Keys go into URLs and permission strings, so the character set is deliberately
#: narrow: two lowercase segments separated by a dot.
KEY_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

#: Module names that describe where a model lives rather than what it belongs to.
_UNINFORMATIVE_MODULES = frozenset({"models", "model", "entities", "tables", "schema"})

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _snake(name: str) -> str:
    """Turn ``ProductVariant`` into ``product_variant``."""
    snake = _CAMEL_BOUNDARY.sub("_", name).lower()
    return re.sub(r"[^a-z0-9]+", "_", snake).strip("_")


def default_model_key(model: type) -> str:
    """Derive ``namespace.model_name`` from a model class.

    The namespace comes from the innermost meaningful package: a model defined in
    ``app.products.models`` belongs to ``products``. Modules named ``models`` and
    friends are skipped because they say where the class lives, not what it is
    part of.
    """
    parts = [
        part
        for part in model.__module__.split(".")
        if part.lower() not in _UNINFORMATIVE_MODULES and part.strip("_")
    ]
    namespace = _snake(parts[-1]) if parts else "default"
    name = _snake(model.__name__)
    if not name:
        raise RegistrationError(
            f"Cannot derive a registry key from model class {model.__name__!r}.",
            hint="Pass an explicit key: `registry.register(Model, admin, key='shop.product')`.",
        )
    return f"{namespace or 'default'}.{name}"


@dataclass(frozen=True, slots=True)
class RegistryEntry(Generic[AdminT]):
    """One registered model together with its admin and stable key."""

    key: str
    model: type
    admin: AdminT


class AdminRegistry(Generic[AdminT]):
    """An ordered, collision-checked map of models to their admin objects.

    Registration order is preserved because it is what the navigation sidebar
    shows; developers group related models by declaring them together.
    """

    def __init__(self) -> None:
        self._by_model: dict[type, RegistryEntry[AdminT]] = {}
        self._by_key: dict[str, RegistryEntry[AdminT]] = {}

    # -- mutation -----------------------------------------------------------

    def register(
        self, model: type, admin: AdminT, *, key: str | None = None
    ) -> RegistryEntry[AdminT]:
        """Register `model`, returning the resulting entry.

        Raises `RegistrationError` when the model is already registered or when
        the derived key belongs to a different model. Both cases are silent data
        corruption if allowed through: the second registration would shadow the
        first and its admin configuration would simply never apply.
        """
        if not isinstance(model, type):
            raise RegistrationError(
                f"Expected a model class, got {type(model).__name__}: {model!r}.",
                hint="Register the class itself, not an instance of it.",
            )

        existing = self._by_model.get(model)
        if existing is not None:
            raise RegistrationError(
                f"{model.__name__} is already registered as {existing.key!r}.",
                hint="Remove the duplicate registration, or call `unregister()` first.",
            )

        resolved = default_model_key(model) if key is None else key
        if not KEY_PATTERN.match(resolved):
            raise RegistrationError(
                f"{resolved!r} is not a valid registry key.",
                hint="Use two lowercase segments separated by a dot, e.g. 'shop.product'.",
            )

        clash = self._by_key.get(resolved)
        if clash is not None:
            raise RegistrationError(
                f"Registry key {resolved!r} is already used by "
                f"{clash.model.__module__}.{clash.model.__name__}, "
                f"which collides with {model.__module__}.{model.__name__}.",
                hint="Give one an explicit key: `@admin.register(Model, key='shop.product')`.",
            )

        entry = RegistryEntry(key=resolved, model=model, admin=admin)
        self._by_model[model] = entry
        self._by_key[resolved] = entry
        return entry

    def unregister(self, model: type) -> None:
        entry = self._by_model.pop(model, None)
        if entry is None:
            raise RegistrationError(f"{model.__name__} is not registered.")
        del self._by_key[entry.key]

    def clear(self) -> None:
        self._by_model.clear()
        self._by_key.clear()

    # -- lookup -------------------------------------------------------------

    def get(self, model: type) -> RegistryEntry[AdminT] | None:
        return self._by_model.get(model)

    def get_by_key(self, key: str) -> RegistryEntry[AdminT] | None:
        return self._by_key.get(key)

    def entry_for(self, model: type) -> RegistryEntry[AdminT]:
        entry = self._by_model.get(model)
        if entry is None:
            raise RegistrationError(
                f"{model.__name__} is not registered with FastFort.",
                hint="Decorate its admin class with `@admin.register(Model)`, and make sure "
                "the module is imported (see `include_admin` and `autodiscover`).",
            )
        return entry

    def entry_for_key(self, key: str) -> RegistryEntry[AdminT]:
        entry = self._by_key.get(key)
        if entry is None:
            known = ", ".join(sorted(self._by_key)) or "none"
            raise RegistrationError(
                f"No model is registered under {key!r}. Registered keys: {known}."
            )
        return entry

    # -- views --------------------------------------------------------------

    def __contains__(self, item: object) -> bool:
        return item in self._by_model or item in self._by_key

    def __iter__(self) -> Iterator[RegistryEntry[AdminT]]:
        return iter(self._by_model.values())

    def __len__(self) -> int:
        return len(self._by_model)

    def __repr__(self) -> str:
        return f"<AdminRegistry: {len(self)} model(s)>"

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(entry.key for entry in self._by_model.values())

    @property
    def models(self) -> tuple[type, ...]:
        return tuple(self._by_model)

    def grouped(self) -> Mapping[str, tuple[RegistryEntry[AdminT], ...]]:
        """Entries bucketed by the namespace part of their key.

        This is the sidebar: models registered under ``shop.*`` appear together
        under a "shop" heading, in registration order.
        """
        groups: dict[str, list[RegistryEntry[AdminT]]] = {}
        for entry in self._by_model.values():
            groups.setdefault(entry.key.split(".", 1)[0], []).append(entry)
        return {name: tuple(items) for name, items in groups.items()}
