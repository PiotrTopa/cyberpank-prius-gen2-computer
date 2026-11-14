"""
Frontend package — pygame UI that runs against the headless backend over the
network API instead of local hardware.

Run on a remote display (e.g. Raspberry Pi):

    python -m cyberpunk_computer.frontend --host 10.200.0.5 --port 8080

See :class:`RemoteTwin` (a VirtualTwin-compatible facade) and :class:`RemoteStore`.
"""

import sys as _sys

# pygame imports pygame.surfarray -> numpy on its own when numpy is installed
# (the Pi has it as a system package). The UI never uses either, and on the
# Pi Zero that import costs ~3 s of a ~11 s start-up. A None entry makes
# "import numpy" fail fast, and pygame then simply leaves surfarray out.
if "numpy" not in _sys.modules and "pygame" not in _sys.modules:
    _sys.modules["numpy"] = None  # type: ignore[assignment]

from .remote_store import RemoteStore, RemoteTwin

__all__ = ["RemoteStore", "RemoteTwin"]
