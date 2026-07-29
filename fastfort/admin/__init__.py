"""Admin site: ModelAdmin, the registry site, views, forms, filters and actions."""

from __future__ import annotations

from .options import ModelAdmin
from .site import build_admin_router

__all__ = ["ModelAdmin", "build_admin_router"]
