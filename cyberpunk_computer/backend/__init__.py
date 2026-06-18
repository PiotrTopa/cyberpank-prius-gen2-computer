"""
Headless backend package.

The backend composes the UI-free VirtualTwin with the metrics subsystem and the
network API. Run it with:

    python -m cyberpunk_computer.backend

See :class:`BackendService` / :class:`BackendConfig` for programmatic use.
"""

from .service import BackendConfig, BackendService

__all__ = ["BackendConfig", "BackendService"]
