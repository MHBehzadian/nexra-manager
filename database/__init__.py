"""Persistence layer (SQLAlchemy 2.0 async + SQLite).

Exposes:
  * ``Database``     — async repository for numbers + per-account read cursors.
  * ``NumberStatus`` — allowed status constants (pending/used/unknown/completed).
"""

from .db import Database
from .models import Number, NumberStatus, ReadCursor

__all__ = ["Database", "Number", "NumberStatus", "ReadCursor"]
