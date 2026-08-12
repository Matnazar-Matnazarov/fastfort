"""Each backend actually satisfies the protocol it is written against.

`mypy --strict` over `fastfort/` passed while this was broken, because nothing
inside the package ever assigns a concrete backend to a `Backend`. The one place
that happens is a project's own `main.py` — the line the README tells everybody
to write — so the error landed on users and not on CI.

These are static assertions expressed at runtime: the `assert_type`-style check
below is what a type checker verifies, and the `isinstance` checks are what a
misuse actually hits.
"""

from __future__ import annotations

import pytest

from fastfort.orm.base import Backend, UnitOfWork


def test_the_sqlalchemy_backend_is_a_backend() -> None:
    """`SQLAlchemyBackend` narrowed `adapter(uow=...)` to its own unit of work.

    Parameter types are contravariant, so narrowing one makes the class
    structurally *incompatible* with the protocol it implements. Every project
    following the README and running mypy got

        Argument "backend" to "FastFort" has incompatible type
        "SQLAlchemyBackend"; expected "Backend | None"

    on a line the documentation told them to write, with no way to act on it
    short of an ignore comment.
    """
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import DeclarativeBase

    from fastfort.orm.sqlalchemy import SQLAlchemyBackend

    class Base(DeclarativeBase):
        pass

    engine = create_async_engine("sqlite+aiosqlite://")
    backend = SQLAlchemyBackend(
        session_factory=async_sessionmaker(engine, expire_on_commit=False), base=Base
    )

    # What a type checker is asked to prove, stated where a reader can see it.
    checked: Backend = backend
    assert checked is backend
    assert isinstance(backend, Backend)

    del sqlalchemy


def test_the_tortoise_backend_is_a_backend() -> None:
    """The same narrowing, in the second backend."""
    pytest.importorskip("tortoise")

    from fastfort.orm.tortoise import TortoiseBackend

    backend = TortoiseBackend()

    checked: Backend = backend
    assert checked is backend
    assert isinstance(backend, Backend)


def test_a_backend_refuses_another_backends_unit_of_work() -> None:
    """Widening the parameter moved the narrowing to runtime, so it has to be
    a runtime error with a sentence rather than an `AttributeError` three frames
    down."""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import DeclarativeBase

    from fastfort.orm.sqlalchemy import SQLAlchemyBackend

    class Base(DeclarativeBase):
        pass

    class Alien:
        """Something that is a `UnitOfWork` in shape and not this backend's."""

        async def __aenter__(self) -> Alien:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def commit(self) -> None: ...
        async def rollback(self) -> None: ...
        async def flush(self) -> None: ...

    engine = create_async_engine("sqlite+aiosqlite://")
    backend = SQLAlchemyBackend(
        session_factory=async_sessionmaker(engine, expire_on_commit=False), base=Base
    )

    alien: UnitOfWork = Alien()  # type: ignore[assignment]
    with pytest.raises(TypeError, match="unit of work from this backend"):
        backend.adapter(Base, alien)
