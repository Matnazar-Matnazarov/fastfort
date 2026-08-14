"""SQLAlchemy 2.0 backend (async sessions)."""

from __future__ import annotations

from .adapter import SQLAlchemyAdapter
from .backend import SQLAlchemyBackend, SQLAlchemyUnitOfWork
from .dialects import DialectProfile, profile_for
from .introspect import introspect_model, is_sqlalchemy_model
from .models import SignInRecordMixin
from .query import QueryBuilder
from .types import Classification, TypeRule, classify, register_type

__all__ = [
    "Classification",
    "DialectProfile",
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
]
