"""Tortoise ORM backend.

The second implementation of the protocols in `fastfort/orm/base.py`, and the
proof of what the layering claims: adding it changed nothing in `fastfort/admin/`,
`fastfort/ui/` or `fastfort/spec/`. Everything above `fastfort/orm/` reads a
`ModelSpec` and a `ListQuery` and cannot tell which ORM produced them.

    from tortoise import Tortoise
    from fastfort.orm.tortoise import TortoiseBackend

    await Tortoise.init(db_url="sqlite://db.sqlite3", modules={"models": ["app.models"]})
    fort = FastFort(settings=..., backend=TortoiseBackend())
"""

from __future__ import annotations

from .adapter import TortoiseAdapter
from .backend import TortoiseBackend, TortoiseUnitOfWork
from .introspect import introspect_model, is_tortoise_model

__all__ = [
    "TortoiseAdapter",
    "TortoiseBackend",
    "TortoiseUnitOfWork",
    "introspect_model",
    "is_tortoise_model",
]
