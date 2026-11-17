"""
ZMQ transport liveness options shared by the backend (``backend/zmq_server``)
and the MFD frontend (``frontend/client``).

Incident 2026-11-17: the phone lost power and rebooted; the Pi's SUB
connection stayed ESTABLISHED to the vanished peer (no FIN, no RST) and was
never probed, because a SUB socket sends nothing after its subscription. ZMTP
heartbeats (libzmq >= 4.2) probe every connection; a peer that does not answer
within HEARTBEAT_TIMEOUT (or a rebooted host answering with RST) drops the
connection and libzmq reconnects after RECONNECT_IVL. TCP keepalive is a
second, kernel-level probe. CONNECT_TIMEOUT / HANDSHAKE_IVL bound how long a
reconnect attempt to a dead or frozen host can hang.
"""
from __future__ import annotations

import logging

import zmq

logger = logging.getLogger(__name__)

# Milliseconds / seconds, as libzmq expects them.
HEARTBEAT_IVL_MS = 2000
HEARTBEAT_TIMEOUT_MS = 6000
HEARTBEAT_TTL_MS = 10000
RECONNECT_IVL_MS = 500
RECONNECT_IVL_MAX_MS = 2000
CONNECT_TIMEOUT_MS = 3000
HANDSHAKE_IVL_MS = 5000
TCP_KEEPALIVE_IDLE_S = 5
TCP_KEEPALIVE_INTVL_S = 2
TCP_KEEPALIVE_CNT = 3


def apply_liveness_options(sock) -> None:
    """Heartbeats + keepalive + bounded reconnect on a ZMQ TCP socket.
    Options this libzmq lacks are skipped (each is best effort)."""
    opts = (
        ("HEARTBEAT_IVL", HEARTBEAT_IVL_MS),
        ("HEARTBEAT_TIMEOUT", HEARTBEAT_TIMEOUT_MS),
        ("HEARTBEAT_TTL", HEARTBEAT_TTL_MS),
        ("RECONNECT_IVL", RECONNECT_IVL_MS),
        ("RECONNECT_IVL_MAX", RECONNECT_IVL_MAX_MS),
        ("CONNECT_TIMEOUT", CONNECT_TIMEOUT_MS),
        ("HANDSHAKE_IVL", HANDSHAKE_IVL_MS),
        ("TCP_KEEPALIVE", 1),
        ("TCP_KEEPALIVE_IDLE", TCP_KEEPALIVE_IDLE_S),
        ("TCP_KEEPALIVE_INTVL", TCP_KEEPALIVE_INTVL_S),
        ("TCP_KEEPALIVE_CNT", TCP_KEEPALIVE_CNT),
    )
    for name, value in opts:
        opt = getattr(zmq, name, None)
        if opt is None:
            continue
        try:
            sock.setsockopt(opt, value)
        except zmq.ZMQError:
            logger.debug("ZMQ option %s unsupported", name)
