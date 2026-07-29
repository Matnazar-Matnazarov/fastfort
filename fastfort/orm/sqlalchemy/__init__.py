"""SQLAlchemy 2.0 backend (async sessions)."""

from __future__ import annotations

from .adapter import SQLAlchemyAdapter
from .backend import SQLAlchemyBackend, SQLAlchemyUnitOfWork
from .dialects import DialectProfile, profile_for
from .introspect import introspect_model, is_sqlalchemy_model
from .query import QueryBuilder

__all__ = [
    "DialectProfile",
    "QueryBuilder",
    "SQLAlchemyAdapter",
    "SQLAlchemyBackend",
    "SQLAlchemyUnitOfWork",
    "introspect_model",
    "is_sqlalchemy_model",
    "profile_for",
]
