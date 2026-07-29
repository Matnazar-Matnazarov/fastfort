"""Authentication, sessions, tokens and permissions.

Reaches the database only through repository protocols, never through an ORM.
"""

from __future__ import annotations

from .user_config import UserModelConfig

__all__ = ["UserModelConfig"]
