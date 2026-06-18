"""
Metrics subsystem — tiered SQLite time-series storage for long-term vehicle data.

Public surface:
- :class:`MetricsDatabase` — schema, writes, rollups, retention, queries.
- :class:`MetricsSink` — background thread sampling the Store into the database.
- :data:`SIGNALS` / :func:`sample_all` / :func:`detect_events` — signal catalog.
"""

from .database import AggrPoint, EventRow, MetricsDatabase
from .signals import SIGNALS, SIGNALS_BY_NAME, Signal, detect_events, sample_all
from .sink import MetricsSink

__all__ = [
    "MetricsDatabase",
    "AggrPoint",
    "EventRow",
    "MetricsSink",
    "Signal",
    "SIGNALS",
    "SIGNALS_BY_NAME",
    "sample_all",
    "detect_events",
]
