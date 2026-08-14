"""What the admin may do to an account.

A public demo is the case this exists for: everything visible, nothing that can
be taken apart. The administrator it signs visitors in as has to survive the
visit, and so do the accounts anyone might want to look at.

Three things are deliberate.

*It applies to the user model and to nothing else.* The model FastFort was told
about in `set_user_model` is the one these rules are about; every other model
behaves exactly as it did.

*It is a property of the deployment, not of the person.* Every administrator is
subject to it equally, including the one who wrote the setting. Rules that
differ per person are authorisation, and that is the project's own -- the admin
gate is where it plugs in.

*A refusal says why.* `AccountGuard` returns a sentence rather than a boolean, so
the page can print the reason instead of a button that silently does nothing.
Every sentence is a translation key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastfort.core.app import FastFort

__all__ = ["AccountGuard"]

#: The refusals, as keys the catalogues carry.
DELETE_REFUSED = "Accounts cannot be deleted in this deployment."
SUPERUSER_DELETE_REFUSED = "Superusers cannot be deleted in this deployment."
PASSWORD_REFUSED = "Passwords cannot be changed in this deployment."  # noqa: S105 -- a sentence
SUPERUSER_PASSWORD_REFUSED = "Only a superuser can change their own password here."  # noqa: S105


@dataclass(frozen=True, slots=True)
class AccountGuard:
    """Answers the two questions the settings above are about."""

    fort: FastFort
    #: Resolves a key to the *instance* of its ModelAdmin. The registry holds
    #: the class, and `password_field_names` on a class is an unbound method.
    admin_for: Callable[[str], Any]

    @property
    def key(self) -> str:
        """The registry key of the user model, or `""` when there is none.

        Compared against the key from the URL rather than against the class,
        because the views work in keys and a model can be registered under one
        the derivation would not have chosen.
        """
        if not self.fort.has_user_model:
            return ""
        entry = self.fort.registry.get(self.fort.user_config.model)
        return str(entry.key) if entry is not None else ""

    def guards(self, model_key: str) -> bool:
        return bool(self.key) and model_key == self.key

    # -- deleting -----------------------------------------------------------

    def delete_refusal(self, model_key: str, obj: Any) -> str:
        """Why `obj` may not be deleted, or `""` when it may be."""
        if not self.guards(model_key):
            return ""
        auth = self.fort.settings.auth
        if not auth.allow_user_delete:
            return DELETE_REFUSED
        if not auth.allow_superuser_delete and self._is_superuser(obj):
            return SUPERUSER_DELETE_REFUSED
        return ""

    def deletable(self, model_key: str, objects: list[Any]) -> tuple[list[Any], str]:
        """Split a bulk selection into what may go and the reason the rest stays.

        Partial rather than all-or-nothing: someone who ticked forty rows and
        one protected account meant to delete the forty. The message names what
        was kept, so the count in the success notice is never a surprise.
        """
        if not self.guards(model_key):
            return objects, ""

        allowed: list[Any] = []
        refusal = ""
        for obj in objects:
            reason = self.delete_refusal(model_key, obj)
            if reason:
                refusal = reason
            else:
                allowed.append(obj)
        return allowed, refusal

    # -- passwords ----------------------------------------------------------

    def password_refusal(self, model_key: str, obj: Any, *, actor: Any = None) -> str:
        """Why `obj`'s password may not be changed, or `""` when it may be.

        `actor` is the signed-in user. Identity is by primary key rather than by
        object: the two come from different queries in some views, and `is`
        would then be False for the same row.
        """
        if not self.guards(model_key):
            return ""
        auth = self.fort.settings.auth
        if not auth.allow_password_change:
            return PASSWORD_REFUSED
        if (
            not auth.allow_superuser_password_change
            and self._is_superuser(obj)
            and not self._same(obj, actor)
        ):
            return SUPERUSER_PASSWORD_REFUSED
        return ""

    def locked_fields(self, model_key: str, obj: Any, *, actor: Any = None) -> frozenset[str]:
        """Field names this request may not write.

        Narrowing only. `FieldSpec.editable` remains the one thing that decides
        what *could* be written; this takes names away from that set and can
        never add one, which is why the mass-assignment boundary still has a
        single source of truth.
        """
        if not self.password_refusal(model_key, obj, actor=actor):
            return frozenset()
        return frozenset(self.admin_for(model_key).password_field_names())

    # -- helpers ------------------------------------------------------------

    def _is_superuser(self, obj: Any) -> bool:
        if obj is None or not self.fort.has_user_model:
            return False
        return bool(self.fort.user_config.is_superuser(obj))

    def _same(self, obj: Any, actor: Any) -> bool:
        if obj is None or actor is None:
            return False
        field = self.fort.user_config.id_field
        left, right = getattr(obj, field, None), getattr(actor, field, None)
        return left is not None and left == right
