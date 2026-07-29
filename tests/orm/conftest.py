"""ORM-suite fixtures.

The database fixtures themselves live in the top-level conftest, because the UI
tests need them too -- a rendered list view is only meaningful against real rows.
"""

from __future__ import annotations
