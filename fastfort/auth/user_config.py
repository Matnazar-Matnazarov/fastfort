"""How FastFort finds the fields it needs on someone else's user model.

FastFort does not ship a mandatory user model. Every project already has one, and
forcing a second is how frameworks end up with two sources of truth about who a
user is. Instead a project points at its own class and, where the names differ
from the defaults, says which attribute means what.

Field names are resolved against the set of attributes the model actually has,
which the caller supplies. `hasattr` alone is not enough: SQLAlchemy's
declarative puts a descriptor on the class for every column, and Tortoise does
not -- its fields live only in `Model._meta`, so `hasattr(User, "email")` is
False for a model that plainly has an email. Detecting from a set the ORM layer
produced keeps this module free of both, which is the rule for everything under
`fastfort/auth/`.

Historically this used `hasattr` against the class, which worked for both
SQLAlchemy declarative models and Tortoise models without importing either.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastfort.core.exceptions import ImproperlyConfigured

__all__ = ["UserModelConfig"]

#: Names tried when a field is not given explicitly, in order of preference.
_CANDIDATES = {
    "id_field": ("id", "pk", "uuid"),
    "identity_field": ("email", "username", "login"),
    "password_field": ("hashed_password", "password_hash", "password"),
    "active_field": ("is_active", "active", "enabled"),
    "staff_field": ("is_staff", "is_admin", "staff"),
    "superuser_field": ("is_superuser", "is_admin", "superuser"),
}


@dataclass(frozen=True, slots=True)
class UserModelConfig:
    """The mapping between FastFort's vocabulary and a project's user model."""

    model: type
    id_field: str = "id"
    identity_field: str = "email"
    password_field: str = "hashed_password"  # noqa: S105 -- an attribute name
    active_field: str = "is_active"
    staff_field: str = "is_staff"
    superuser_field: str = "is_superuser"

    @classmethod
    def detect(
        cls, model: type, known: frozenset[str] | None = None, **overrides: str
    ) -> UserModelConfig:
        """Build a configuration, guessing any field that was not supplied.

        `known` is the model's own attribute names, as the ORM layer reports
        them. Without it this falls back to `hasattr`, which is right for an
        ORM that puts a descriptor on the class and wrong for one that does not
        -- so `FastFort.set_user_model` passes the set it got from the backend
        and only a direct caller ever takes the fallback.

        Detection only ever picks a name the model actually has, so a wrong guess
        is impossible; what can happen is no guess at all, which is reported as a
        configuration error naming the field and the alternatives that were tried.
        """
        resolved: dict[str, str] = {}
        missing: list[str] = []
        has = _presence(model, known)

        for field_name, candidates in _CANDIDATES.items():
            explicit = overrides.get(field_name)
            if explicit is not None:
                resolved[field_name] = explicit
                continue
            found = next((name for name in candidates if has(name)), None)
            if found is None:
                missing.append(f"{field_name} (tried: {', '.join(candidates)})")
            else:
                resolved[field_name] = found

        if missing:
            listed = "\n".join(f"  - {item}" for item in missing)
            raise ImproperlyConfigured(
                f"Cannot work out which attributes of {model.__name__} to use:\n{listed}",
                hint=(
                    "Name them explicitly, for example: "
                    "fort.set_user_model(User, identity_field='login', "
                    "password_field='pwd_hash')."
                ),
            )

        config = cls(model=model, **resolved)
        config.validate(known)
        return config

    def validate(self, known: frozenset[str] | None = None) -> None:
        """Check that every configured attribute exists on the model.

        Runs at start-up rather than on first login, because a typo here means
        nobody can sign in and the failure should not wait for a user to find it.
        """
        has = _presence(self.model, known)
        missing = [
            f"{label}={name!r}"
            for label, name in self._configured_fields().items()
            if not has(name)
        ]
        if missing:
            available = ", ".join(sorted(self._public_attributes())) or "none"
            raise ImproperlyConfigured(
                f"User model {self.model.__name__} has no attribute for: {', '.join(missing)}.\n"
                f"Attributes found on the class: {available}",
                hint=(
                    "Either add the column to your model, or point FastFort at the "
                    "existing one via fort.set_user_model(...)."
                ),
            )

    def _configured_fields(self) -> dict[str, str]:
        return {
            "id_field": self.id_field,
            "identity_field": self.identity_field,
            "password_field": self.password_field,
            "active_field": self.active_field,
            "staff_field": self.staff_field,
            "superuser_field": self.superuser_field,
        }

    def _public_attributes(self) -> list[str]:
        return [name for name in dir(self.model) if not name.startswith("_")]

    # -- reading a user instance -------------------------------------------

    def identity_of(self, user: Any) -> str:
        return str(getattr(user, self.identity_field))

    def password_hash_of(self, user: Any) -> str:
        value = getattr(user, self.password_field)
        return "" if value is None else str(value)

    def is_active(self, user: Any) -> bool:
        return bool(getattr(user, self.active_field, True))

    def is_staff(self, user: Any) -> bool:
        """Whether the user may open the admin at all.

        A superuser is always staff; requiring both flags to be set by hand is a
        footgun that locks people out of their own installation.
        """
        return bool(getattr(user, self.staff_field, False)) or self.is_superuser(user)

    def is_superuser(self, user: Any) -> bool:
        return bool(getattr(user, self.superuser_field, False))


def _presence(model: type, known: frozenset[str] | None) -> Callable[[str], bool]:
    """Whether the model has an attribute by that name.

    From the set the ORM layer reported when there is one, and from `hasattr`
    when there is not. The two disagree for Tortoise, whose fields exist only in
    `Model._meta` -- and knowing that here would put ORM-specific knowledge in a
    layer the architecture test keeps free of it.
    """
    if known is None:
        return lambda name: hasattr(model, name)
    return lambda name: name in known or hasattr(model, name)
