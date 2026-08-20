"""Admin site: ModelAdmin, registration, views, forms, filters and messages."""

from __future__ import annotations

from .api_tokens import ApiTokenAdmin
from .dashboard import (
    FULL,
    HALF,
    THIRD,
    Breakdown,
    Counts,
    Metric,
    Recent,
    Signups,
    Trend,
    Widget,
)
from .decorators import display, register
from .forms import Form, FormField
from .inlines import InlineAdmin, TabularInline
from .messages import Message, MessageLevel, Messages
from .options import Action, ModelAdmin, action
from .site import build_admin_router
from .widgets import register_widget

__all__ = [
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
]
