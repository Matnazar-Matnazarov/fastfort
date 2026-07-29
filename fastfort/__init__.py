"""FastFort -- batteries-included authentication and admin framework for FastAPI.

This module is the public API surface. Anything not exported here is private and
may change between minor releases; see CONTRIBUTING.md for the stability policy.
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"

#: Version of the database schema owned by FastFort. It moves independently of
#: `__version__`; `fastfort db upgrade` migrates the database up to this number.
SCHEMA_VERSION = 1

__all__ = [
    "SCHEMA_VERSION",
    "__version__",
]
