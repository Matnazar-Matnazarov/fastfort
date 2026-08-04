"""The package version, in a module of its own.

`fastfort/__init__.py` imports the admin namespace, and the admin needs the
version to render it in the footer. Reading it from the package root would be a
circular import, so it lives here where anything can import it.
"""

from __future__ import annotations

__all__ = ["SCHEMA_VERSION", "__version__"]

__version__ = "0.1.0"

#: Version of the database schema owned by FastFort. It moves independently of
#: `__version__`; `fastfort db upgrade` migrates the database up to this number.
SCHEMA_VERSION = 1
