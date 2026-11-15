"""Long-run battery-health data module (HV NiMH pack + 12 V auxiliary battery).

Pure standard library: the same code runs in the backend on the phone and
offline on a laptop (``python -m cyberpunk_computer.battery``). Layout:

    model.py      constants, thresholds, the compact column codec
    frames.py     raw gateway / panda / powerbox messages -> collector events
    collector.py  event stream -> per-ride and per-rest raw records
    analysis.py   raw record -> features (balance, resistance, capacity proxies, ...)
    trends.py     robust trends, temperature normalisation, change points
    summary.py    health indicators and the precomputed UI aggregates
    store.py      SQLite storage (schema versioned)
    backfill.py   trip recordings / metrics database -> store
    service.py    backend glue (tap + low-priority worker thread)
    api.py        REST routes under /api/v1/battery
    synth.py      synthetic pack simulator (tests, demo data)

Definitions of every metric: docs/BATTERY_HEALTH.md.
"""

from .model import ANALYSIS_VERSION, SCHEMA_VERSION  # noqa: F401
