"""
Satellite authentication handshake (transport-agnostic, NDJSON payloads).

See ``docs/SATELLITES.md`` §3. One :class:`LinkAuth` per link (BLE
connection, Wi-Fi TCP session) runs this exchange right after the link comes
up::

    car → sat  {"a":"auth","v":1,"nonce":"<b64 32 B>","car":"<car_fp>"}
    sat → car  {"a":"auth","v":1,"fp":…,"pubkey":…,"cert":{…}|null,"nonce":…,"sig":…}
    car → sat  {"a":"auth_ok","sat_id":N,"sig":…[, "cert":{…}]}  |  {"a":"auth_fail","why":"…"}

    MSG  = b"prius-sat-auth-v1" + car_nonce + sat_nonce + car_fp + sat_fp + transport
    MSG2 = b"prius-car-auth-v1" + sat_nonce + car_nonce + sat_fp + car_fp

Why a separate, store-free state machine: the decision "trust / refuse /
hold for pairing" must be identical on every transport and testable without
sockets. :class:`LinkAuth` only *decides*; the registry sends the replies,
updates the keychain (cert-accept) and drops links.

The satellite side (:class:`SatelliteAuthClient`) lives here too so tests,
``scripts/sat_wifi_sim.py`` and future reference firmware ports share one
definition of the bytes being signed.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .satellite_keychain import (
    Keychain,
    KeychainError,
    b64d,
    b64e,
    fingerprint,
    sign,
    verify,
    verify_cert,
)

logger = logging.getLogger(__name__)

AUTH_VERSION = 1
AUTH_TIMEOUT_S = 10.0
NONCE_LEN = 32
SAT_CONTEXT = b"prius-sat-auth-v1"
CAR_CONTEXT = b"prius-car-auth-v1"

# LinkAuth states
PENDING = "pending"      # challenge sent, waiting for the satellite's reply
OK = "ok"                # authenticated
FAILED = "failed"        # refused (reply carries auth_fail); drop the link
HELD = "held"            # valid signature, unknown key, pairing window open


def sat_message(car_nonce: bytes, sat_nonce: bytes, car_fp: str, sat_fp: str,
                transport: str) -> bytes:
    """MSG — what the satellite signs."""
    return (SAT_CONTEXT + car_nonce + sat_nonce + car_fp.encode("ascii")
            + sat_fp.encode("ascii") + transport.encode("ascii"))


def car_message(sat_nonce: bytes, car_nonce: bytes, sat_fp: str, car_fp: str) -> bytes:
    """MSG2 — what the car signs back (mutual auth)."""
    return (CAR_CONTEXT + sat_nonce + car_nonce + sat_fp.encode("ascii")
            + car_fp.encode("ascii"))


@dataclass
class AuthResult:
    """Outcome of feeding one satellite message into :meth:`LinkAuth.handle`."""
    state: str                          # PENDING (ignored msg) | OK | FAILED | HELD
    reply: Optional[Dict[str, Any]] = None   # payload to send back (None = send nothing)
    sat_id: Optional[int] = None
    fp: Optional[str] = None
    pubkey: Optional[str] = None
    cert: Optional[Dict[str, Any]] = None     # valid cert of a not-yet-enrolled satellite
    cert_sat_id: Optional[int] = None
    why: str = ""


class LinkAuth:
    """Car-side handshake state machine for one link."""

    def __init__(
        self,
        keychain: Keychain,
        transport: str,
        address: str,
        now: Optional[float] = None,
        timeout_s: float = AUTH_TIMEOUT_S,
        nonce_fn: Callable[[int], bytes] = os.urandom,
    ):
        self.keychain = keychain
        self.transport = transport
        self.address = address
        self.timeout_s = timeout_s
        self.started = time.monotonic() if now is None else now
        self.car_nonce = nonce_fn(NONCE_LEN)
        self.sat_nonce: Optional[bytes] = None
        self.state = PENDING
        self.fp: Optional[str] = None
        self.pubkey: Optional[str] = None
        self.sat_id: Optional[int] = None
        self.why = ""

    # ── car → sat ───────────────────────────────────────────────────────────

    def challenge(self) -> Optional[Dict[str, Any]]:
        """The opening ``auth`` message, or None when the car has no key."""
        car_fp = self.keychain.car_fp
        if not car_fp:
            return None
        return {"a": "auth", "v": AUTH_VERSION, "nonce": b64e(self.car_nonce), "car": car_fp}

    def expired(self, now: float) -> bool:
        """Handshake deadline passed (HELD links are exempt: the registry
        keeps them while the pairing window is open)."""
        return self.state == PENDING and (now - self.started) >= self.timeout_s

    def _fail(self, why: str) -> AuthResult:
        self.state = FAILED
        self.why = why
        logger.info("Satellite auth refused on %s %s: %s", self.transport, self.address, why)
        return AuthResult(FAILED, {"a": "auth_fail", "why": why}, fp=self.fp,
                          pubkey=self.pubkey, why=why)

    def auth_ok(self, sat_id: int, cert: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build ``auth_ok`` (also used after a HELD link gets enrolled)."""
        car_fp = self.keychain.car_fp or ""
        sig = self.keychain.car_sign(car_message(self.sat_nonce or b"", self.car_nonce,
                                                 self.fp or "", car_fp))
        self.state = OK
        self.sat_id = sat_id
        msg: Dict[str, Any] = {"a": "auth_ok", "sat_id": sat_id, "sig": b64e(sig)}
        if cert:
            msg["cert"] = cert
        return msg

    # ── sat → car ───────────────────────────────────────────────────────────

    def handle(self, payload: Any, now: Optional[float] = None,
               pairing_open: bool = False) -> AuthResult:
        """Feed one payload from the satellite. Non-``auth`` payloads before
        the decision are ignored (state stays PENDING); anything after it is
        ignored too (the registry routes post-auth traffic itself)."""
        if self.state != PENDING:
            return AuthResult(self.state, sat_id=self.sat_id, fp=self.fp, pubkey=self.pubkey)
        if not isinstance(payload, dict) or payload.get("a") != "auth":
            return AuthResult(PENDING)
        now = time.monotonic() if now is None else now
        if (now - self.started) >= self.timeout_s:
            return self._fail("timeout")
        if payload.get("v") != AUTH_VERSION:
            return self._fail("version")
        car_fp = self.keychain.car_fp
        if not car_fp or self.keychain.car_priv is None:
            return self._fail("car has no key")
        try:
            pub = b64d(payload["pubkey"])
            sat_nonce = b64d(payload["nonce"])
            sig = b64d(payload["sig"])
            fp = str(payload["fp"])
        except (KeyError, ValueError, TypeError):
            return self._fail("malformed")
        if len(pub) != 32 or len(sat_nonce) != NONCE_LEN or len(sig) != 64:
            return self._fail("malformed")
        if fingerprint(pub) != fp:
            return self._fail("fp does not match pubkey")
        self.fp, self.pubkey, self.sat_nonce = fp, payload["pubkey"], sat_nonce
        if self.keychain.is_revoked(fp):
            return self._fail("revoked")
        # Proof of possession first: even a device we are only going to hold
        # for pairing must have signed this session's car nonce.
        if not verify(pub, sig, sat_message(self.car_nonce, sat_nonce, car_fp, fp, self.transport)):
            return self._fail("bad signature")

        enrolled = self.keychain.by_fp(fp)
        if enrolled is not None:
            sat_id, entry = enrolled
            if entry.get("transport") != self.transport:
                return self._fail("enrolled for another transport")
            return self._ok(sat_id)

        cert = payload.get("cert")
        if cert:
            reason = verify_cert(self.keychain.car_pub or b"", cert)
            if reason is None and (cert.get("fp") != fp or cert.get("transport") != self.transport):
                reason = "cert does not match key/transport"
            if reason is None:
                other = self.keychain.get(int(cert["sat_id"]))
                if other is not None and other.get("fp") != fp:
                    reason = "cert id already taken"
            if reason is None:
                res = self._ok(int(cert["sat_id"]))
                res.cert = dict(cert)
                res.cert_sat_id = int(cert["sat_id"])
                return res
            if not pairing_open:
                return self._fail(reason)
            logger.info("Satellite on %s %s offered an invalid cert (%s)",
                        self.transport, self.address, reason)

        if pairing_open:
            self.state = HELD
            self.why = "unknown key (pairing window open)"
            logger.info("Satellite %s on %s %s held for enrolment (pairing open)",
                        fp, self.transport, self.address)
            return AuthResult(HELD, None, fp=fp, pubkey=self.pubkey, why=self.why)
        return self._fail("unknown device")

    def _ok(self, sat_id: int) -> AuthResult:
        try:
            reply = self.auth_ok(sat_id)
        except KeychainError as exc:
            return self._fail(str(exc))
        logger.info("Satellite %d (%s) authenticated on %s %s",
                    sat_id, self.fp, self.transport, self.address)
        return AuthResult(OK, reply, sat_id=sat_id, fp=self.fp, pubkey=self.pubkey)


# ─────────────────────────────────────────────────────────────────────────────
# Satellite side (tests, simulator, reference for firmware)
# ─────────────────────────────────────────────────────────────────────────────

class SatelliteAuthError(Exception):
    """The car refused us, or the car could not prove it is our car."""


class SatelliteAuthClient:
    """Satellite half of the handshake, driven by a provisioning blob from
    ``prius-satkey new`` (``privkey``, ``cert``, ``car_pub``, ``transport``)."""

    def __init__(self, blob: Dict[str, Any], nonce_fn: Callable[[int], bytes] = os.urandom):
        self.priv = b64d(blob["privkey"])
        self.pubkey = blob.get("pubkey") or ""
        self.fp = blob.get("fp") or fingerprint(b64d(self.pubkey))
        self.cert = blob.get("cert")
        self.transport = blob["transport"]
        self.car_pub = b64d(blob["car_pub"])
        self.car_fp = fingerprint(self.car_pub)
        self.sat_id: Optional[int] = blob.get("sat_id")
        self._nonce_fn = nonce_fn
        self._car_nonce: Optional[bytes] = None
        self._sat_nonce: Optional[bytes] = None
        self.authenticated = False

    def respond(self, challenge: Dict[str, Any]) -> Dict[str, Any]:
        """Answer the car's ``auth`` challenge."""
        if challenge.get("a") != "auth" or challenge.get("v") != AUTH_VERSION:
            raise SatelliteAuthError("not an auth challenge")
        if challenge.get("car") != self.car_fp:
            raise SatelliteAuthError("challenge from a foreign car %r" % challenge.get("car"))
        self._car_nonce = b64d(challenge["nonce"])
        self._sat_nonce = self._nonce_fn(NONCE_LEN)
        msg = sat_message(self._car_nonce, self._sat_nonce, self.car_fp, self.fp, self.transport)
        return {"a": "auth", "v": AUTH_VERSION, "fp": self.fp, "pubkey": self.pubkey,
                "cert": self.cert, "nonce": b64e(self._sat_nonce),
                "sig": b64e(sign(self.priv, msg))}

    def check_result(self, msg: Dict[str, Any]) -> int:
        """Verify ``auth_ok`` (car signature over MSG2); returns our sat_id."""
        if msg.get("a") == "auth_fail":
            raise SatelliteAuthError("car refused: %s" % msg.get("why"))
        if msg.get("a") != "auth_ok" or self._car_nonce is None or self._sat_nonce is None:
            raise SatelliteAuthError("unexpected %r" % msg.get("a"))
        m2 = car_message(self._sat_nonce, self._car_nonce, self.fp, self.car_fp)
        if not verify(self.car_pub, b64d(msg.get("sig", "")), m2):
            raise SatelliteAuthError("car signature invalid (not our car)")
        self.sat_id = int(msg["sat_id"])
        if msg.get("cert"):
            self.cert = msg["cert"]
        self.authenticated = True
        return self.sat_id
