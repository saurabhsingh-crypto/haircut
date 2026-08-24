"""
Haircut Margin Calculator - framework-free core.

Nothing in this package imports Streamlit, so the same code runs from a
notebook, a script or a test as it does behind the web UI.

    schema     the canonical portfolio / haircut tables and field metadata
    ingest     read a broker file, profile its columns, normalise it
    pipeline   file bytes -> canonical DataFrame, with manual overrides
    engine     join holdings to a haircut master and compute margin
    store      the saved-haircut library (MySQL, or SQLite for local dev)
    report     export a calculation to Excel
    templates  known broker layouts that skip column detection
"""
from __future__ import annotations

from . import engine, ingest, pipeline, report, schema, store, templates
from .ingest import UnsupportedFile
from .store import StorageError

__all__ = [
    "engine", "ingest", "pipeline", "report", "schema", "store", "templates",
    "UnsupportedFile", "StorageError",
]
