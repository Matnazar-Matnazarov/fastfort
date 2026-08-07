"""Tests that enforce the architectural boundaries of the package.

These do not check code quality -- they protect the **layering**. A failure here
means the design has been broken, not that a linter is unhappy.

The rules themselves are documented in CONTRIBUTING.md.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "fastfort"

#: These packages may never import an ORM. That is what lets a new ORM adapter be
#: added without touching a single line of the admin, UI or spec layers.
ORM_FREE_PACKAGES = ("core", "spec", "admin", "auth", "ui", "cli", "i18n")

#: Top-level module names that count as "an ORM or a database driver".
FORBIDDEN_ORM_MODULES = frozenset({"sqlalchemy", "tortoise", "aiosqlite", "asyncpg", "aiomysql"})

#: Only these paths are allowed to depend on an ORM.
ORM_ALLOWED_PREFIXES = ("fastfort/orm/",)


def _python_files(package: str) -> list[Path]:
    return sorted((PACKAGE_ROOT / package).rglob("*.py"))


def _imported_roots(source: str) -> set[str]:
    """Return the top-level module name of every absolute import in a file."""
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        # `node.level > 0` means a relative import (`from . import x`), which is
        # always in-package and therefore never a boundary violation.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("package", ORM_FREE_PACKAGES)
def test_layer_does_not_import_orm(package: str) -> None:
    """ORM-agnostic layers import no ORM library."""
    violations: list[str] = []

    for path in _python_files(package):
        roots = _imported_roots(path.read_text(encoding="utf-8"))
        for forbidden in sorted(roots & FORBIDDEN_ORM_MODULES):
            rel = path.relative_to(PACKAGE_ROOT.parent)
            violations.append(f"{rel}: `import {forbidden}`")

    assert not violations, (
        f"`fastfort/{package}/` must stay ORM-agnostic, but these imports were found:\n  "
        + "\n  ".join(violations)
    )


def test_orm_imports_are_confined_to_orm_package() -> None:
    """Across the whole package, ORM imports live only under `fastfort/orm/`."""
    violations: list[str] = []

    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        rel = path.relative_to(PACKAGE_ROOT.parent).as_posix()
        if rel.startswith(ORM_ALLOWED_PREFIXES):
            continue
        roots = _imported_roots(path.read_text(encoding="utf-8"))
        for forbidden in sorted(roots & FORBIDDEN_ORM_MODULES):
            violations.append(f"{rel}: `import {forbidden}`")

    assert not violations, "ORM import outside `fastfort/orm/`:\n  " + "\n  ".join(violations)


def test_spec_layer_is_free_of_web_frameworks() -> None:
    """`fastfort/spec/` is pure data -- it must not depend on a web framework.

    The spec layer is reused by future renderers (JSON API, alternative front ends),
    so binding it to FastAPI, Starlette or Jinja2 would defeat its purpose.
    """
    violations: list[str] = []
    web_modules = {"fastapi", "starlette", "jinja2"}

    for path in _python_files("spec"):
        roots = _imported_roots(path.read_text(encoding="utf-8"))
        for forbidden in sorted(roots & web_modules):
            rel = path.relative_to(PACKAGE_ROOT.parent)
            violations.append(f"{rel}: `import {forbidden}`")

    assert not violations, "`fastfort/spec/` must not depend on a web framework:\n  " + "\n  ".join(
        violations
    )


def test_every_package_has_init() -> None:
    """Every package directory ships an `__init__.py` (no implicit namespace packages)."""
    missing = [
        str(d.relative_to(PACKAGE_ROOT.parent))
        for d in PACKAGE_ROOT.rglob("*")
        if d.is_dir()
        and not d.name.startswith("__")
        and any(d.glob("*.py"))
        and not (d / "__init__.py").exists()
    ]
    assert not missing, f"missing `__init__.py`: {missing}"


def test_package_ships_py_typed() -> None:
    """The `py.typed` marker is present so downstream users get type information."""
    assert (PACKAGE_ROOT / "py.typed").exists()


def test_neither_orm_backend_imports_the_other() -> None:
    """The two are independent implementations of one protocol.

    They may share what is genuinely about the *database* -- `orm/coerce.py`
    turns a query string into a column's type, and `dialects.py` records which
    databases have `NULLS LAST` -- and neither may reach into the other's
    ORM-specific modules. A Tortoise adapter that imported `sqlalchemy.query`
    would make installing one ORM require the other, which is exactly what the
    two optional extras exist to avoid.
    """
    shared = {"coerce", "dialects", "introspect"}
    violations: list[str] = []

    for backend, other in (("sqlalchemy", "tortoise"), ("tortoise", "sqlalchemy")):
        for path in sorted((PACKAGE_ROOT / "orm" / backend).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                # `from ..tortoise.adapter import ...` -- a relative import of
                # the sibling backend, which is what this is looking for.
                if node.module.split(".")[0] != other:
                    continue
                if node.module.split(".")[-1] in shared:
                    continue
                rel = path.relative_to(PACKAGE_ROOT.parent)
                violations.append(f"{rel}: `from {node.module} import ...`")

    assert not violations, (
        "the two ORM backends must stay independent of each other:\n  " + "\n  ".join(violations)
    )


def test_a_project_can_install_one_orm_without_the_other() -> None:
    """Both are optional extras, so neither may be imported at package level.

    `import fastfort` on a project that installed only `fastfort[tortoise]` has
    to work. The moment something in `fastfort/core/`, `fastfort/admin/` or
    `fastfort/__init__.py` reaches for either ORM, that install is broken --
    which the layering rules above already prevent, and this states as the
    reason they exist.
    """
    for extra in ("sqlalchemy", "tortoise"):
        entry = PACKAGE_ROOT / "orm" / extra / "__init__.py"
        assert entry.is_file(), f"the {extra} backend should ship its own entry point"

    # And the top level reaches for neither.
    roots = _imported_roots((PACKAGE_ROOT / "__init__.py").read_text(encoding="utf-8"))
    assert not roots & FORBIDDEN_ORM_MODULES
