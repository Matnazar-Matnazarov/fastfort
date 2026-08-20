"""The admin over a project's API token table.

    from fastfort.admin import ApiTokenAdmin
    from fastfort.orm.sqlalchemy import ApiTokenMixin

    class ApiToken(ApiTokenMixin, Base):
        __tablename__ = "admin_api_token"

    fort.enable_api_tokens(ApiToken)
    fort.register(ApiToken, ApiTokenAdmin)

Almost every column here is read-only, and that is the design rather than
caution. A token's digest, prefix and owner are decided when it is minted and
have no meaning if edited afterwards -- a form that let somebody paste a new
`token_hash` would be a form for forging a credential. What is left editable is
`name`, because what a token is *for* is the one thing about it a person is
entitled to change their mind on.

Creating one does not go through the normal add form. A secret has to be
generated rather than typed, and it has to be shown exactly once, so the add
view branches into the minting page -- see `_is_token_model` in `site.py`.
"""

from __future__ import annotations

from typing import ClassVar

from .options import Action, ModelAdmin

__all__ = ["REVOKE_ACTION", "ApiTokenAdmin"]

#: Stops a token working without deleting the row, so it still answers what
#: this was and when it stopped.
REVOKE_ACTION = "revoke"


class ApiTokenAdmin(ModelAdmin):
    """A list of tokens, a way to revoke them, and no way to edit a digest."""

    list_display: ClassVar[tuple[str, ...]] = (
        "name",
        "prefix",
        "user_key",
        "last_used_at",
        "expires_at",
        "revoked_at",
    )
    list_filter: ClassVar[tuple[str, ...]] = ()
    search_fields: ClassVar[tuple[str, ...]] = ("name", "prefix")
    ordering: ClassVar[tuple[str, ...]] = ("-created_at",)

    #: Everything the mint decided. `name` is deliberately absent: what a token
    #: is for is the one thing worth changing later.
    readonly_fields: ClassVar[tuple[str, ...]] = (
        "token_hash",
        "prefix",
        "user_key",
        "scopes",
        "created_at",
        "last_used_at",
        "revoked_at",
    )

    def action_specs(self) -> tuple[Action, ...]:
        """Revoke, beside whatever the base class already offers.

        Delete stays as well. They are different answers: revoking keeps the
        row and the history, deleting says this should never have existed.
        """
        return (
            *super().action_specs(),
            Action(
                name=REVOKE_ACTION,
                label="Revoke",
                icon="lock",
                danger=True,
                confirm="Revoke {count} token(s)? Anything using them stops working immediately.",
            ),
        )
