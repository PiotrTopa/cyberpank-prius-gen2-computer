"""
Frontend package — pygame UI that runs against the headless backend over the
network API instead of local hardware.

Run on a remote display (e.g. Raspberry Pi):

    python -m cyberpunk_computer.frontend --host 10.200.0.5 --port 8080

See :class:`RemoteTwin` (a VirtualTwin-compatible facade) and :class:`RemoteStore`.
"""

from .remote_store import RemoteStore, RemoteTwin

__all__ = ["RemoteStore", "RemoteTwin"]
