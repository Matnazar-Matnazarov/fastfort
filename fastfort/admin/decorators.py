"""`@admin.register`, and the pending-registration buffer behind it.

A decorator runs at import time, before any `FastFort` instance necessarily
exists, so it cannot register straight into one. It records the intent here, and
`include_admin`, `autodiscover` and `mount` drain the buffer.

That indirection is why an `admin.py` can be written without importing the
application, which is the whole point: a models module and an admin module that
both import `main` would be a circular import in every project.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from fastfort.core.exceptions import RegistrationError

if TYPE_CHECKING:
    from .options import ModelAdmin

__all__ = ["display", "pending_registrations", "register", "take_pending"]

AdminT = TypeVar("AdminT", bound="type[ModelAdmin]")


@dataclass(frozen=True, slots=True)
class PendingRegistration:
    """A `@register` call waiting for an application to attach it to."""

    model: type
    admin: type[ModelAdmin]
    key: str | None


#: Module-level on purpose: decorators have nowhere else to put their result.
#: Drained rather than read, so importing the same module twice cannot register
#: the same model twice.
_pending: list[PendingRegistration] = []


def register(model: type, *, key: str | None = None) -> Callable[[AdminT], AdminT]:
    """Register a model with the admin::

        @admin.register(Product)
        class ProductAdmin(admin.ModelAdmin):
            list_display = ("id", "name", "price")

    `key` overrides the derived registry key, which is needed when two models in
    different packages would otherwise derive the same one.
    """
    if not isinstance(model, type):
        raise RegistrationError(
            f"@register expects a model class, got {type(model).__name__}.",
            hint="Write @admin.register(Product), not @admin.register(Product()).",
        )

    def decorator(admin_class: AdminT) -> AdminT:
        from .options import ModelAdmin

        if not (isinstance(admin_class, type) and issubclass(admin_class, ModelAdmin)):
            raise RegistrationError(
                f"@register must decorate a ModelAdmin subclass, got {admin_class!r}.",
                hint="Declare it as `class ProductAdmin(admin.ModelAdmin):`.",
            )
        _pending.append(PendingRegistration(model=model, admin=admin_class, key=key))
        # Returned unchanged: the class stays usable and testable on its own.
        return admin_class

    return decorator


def display(
    *,
    label: str | None = None,
    ordering: str | None = None,
    boolean: bool = False,
) -> Callable[[Any], Any]:
    """Describe a computed column::

        @admin.display(label="Price", ordering="price")
        def price_display(self, obj):
            return f"{obj.price:,.0f}"

    `ordering` names the real column to sort by, since a computed value has none
    of its own. Without it the header is rendered as unsortable rather than
    producing an ordering the database cannot honour.
    """

    def decorator(method: Any) -> Any:
        method.ff_display = True
        method.ff_label = label
        method.ff_ordering = ordering
        method.ff_boolean = boolean
        return method

    return decorator


def pending_registrations() -> tuple[PendingRegistration, ...]:
    """What is waiting to be attached. Read-only; for diagnostics."""
    return tuple(_pending)


def take_pending() -> tuple[PendingRegistration, ...]:
    """Return everything buffered and empty the buffer."""
    taken = tuple(_pending)
    _pending.clear()
    return taken
