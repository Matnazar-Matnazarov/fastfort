"""The names 1.0 promises, written down so removing one is a decision.

From 1.0 a name dropped from one of these `__all__` lists is a breaking change
and needs a major version. Without this file that happens by accident: a
refactor moves a class, the old import keeps working in the test suite because
the tests import from the new place, and the first to notice is a project
upgrading a patch release. Adding a name is fine -- add it here too.
"""

from __future__ import annotations

import importlib

import pytest

PUBLIC = {
    "fastfort": {
        "SCHEMA_VERSION",
        "AdminSettings",
        "AuthSettings",
        "ConfigurationError",
        "FastFort",
        "FastFortError",
        "FastFortSettings",
        "Hook",
        "ImproperlyConfigured",
        "ObjectNotFound",
        "PermissionDenied",
        "SecuritySettings",
        "UISettings",
        "ValidationError",
        "__version__",
        "admin",
    },
    "fastfort.admin": {
        "FULL",
        "HALF",
        "THIRD",
        "Action",
        "ApiTokenAdmin",
        "Breakdown",
        "Counts",
        "Form",
        "FormField",
        "InlineAdmin",
        "Message",
        "MessageLevel",
        "Messages",
        "Metric",
        "ModelAdmin",
        "Recent",
        "Signups",
        "TabularInline",
        "Trend",
        "Widget",
        "action",
        "build_admin_router",
        "display",
        "register",
        "register_widget",
    },
    "fastfort.orm.sqlalchemy": {
        "ApiTokenMixin",
        "AuditEntryMixin",
        "Classification",
        "DialectProfile",
        "FavoriteMixin",
        "QueryBuilder",
        "SQLAlchemyAdapter",
        "SQLAlchemyBackend",
        "SQLAlchemyUnitOfWork",
        "SignInRecordMixin",
        "TypeRule",
        "classify",
        "introspect_model",
        "is_sqlalchemy_model",
        "profile_for",
        "register_type",
    },
    "fastfort.orm.tortoise": {
        "TortoiseAdapter",
        "TortoiseBackend",
        "TortoiseUnitOfWork",
        "introspect_model",
        "is_tortoise_model",
    },
}


@pytest.mark.parametrize("module", sorted(PUBLIC))
def test_no_public_name_disappears(module: str) -> None:
    exported = set(importlib.import_module(module).__all__)

    missing = PUBLIC[module] - exported
    assert not missing, f"{module} no longer exports {sorted(missing)}: that is a major version"


@pytest.mark.parametrize("module", sorted(PUBLIC))
def test_every_exported_name_resolves(module: str) -> None:
    """An `__all__` entry with nothing behind it fails at `import *`, far from here."""
    loaded = importlib.import_module(module)

    for name in loaded.__all__:
        assert hasattr(loaded, name), f"{module}.__all__ names {name!r}, which does not exist"
