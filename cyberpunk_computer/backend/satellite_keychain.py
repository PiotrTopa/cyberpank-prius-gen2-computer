"""
Satellite keychain — the car's root of trust for BLE / Wi-Fi satellites.

See ``docs/SATELLITES.md`` §1–2. The car holds one Ed25519 key pair (the
**car key**); every authenticated satellite holds its own. A satellite is
trusted when its fingerprint is enrolled in ``keychain.json`` *or* when it
presents a certificate signed by the car key (so a satellite can be signed on
the laptop with a copy of the car key and simply show up). Revoked
fingerprints are refused regardless of certificate.

Files (directory = ``BACKEND_SATELLITE_KEYCHAIN_DIR``, default
``/etc/prius/satellites``; falls back to ``./satellites`` with a warning when
that is not writable, e.g. on the P52)::

    car.key        car private key, PKCS#8 PEM, 0600
    car.pub        car public key, SubjectPublicKeyInfo PEM, 0644
    keychain.json  enrolled satellites + revocations, 0600

Why this module looks the way it does:

* The backend and the ``prius-satkey`` CLI both edit ``keychain.json``.
  Every mutation goes through :meth:`Keychain.transaction` (``flock`` on a
  side lock file, reload, mutate, atomic write via temp file + ``os.replace``)
  so neither side ever sees a torn file or loses the other's change.
* Fingerprints, canonical JSON and base64 are pure stdlib, so the module
  imports (and the registry runs RS485-only) without ``cryptography``; the
  Ed25519 parts import it lazily and raise :class:`CryptoUnavailable` with a
  clear message.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:  # fcntl is POSIX-only; the car and the P52 both have it
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

from ..io.satellite_transport import (
    ID_RANGES,
    REQUIRE_AUTH,
    TRANSPORT_RS485,
    TRANSPORTS,
)

logger = logging.getLogger(__name__)

ENV_KEYCHAIN_DIR = "BACKEND_SATELLITE_KEYCHAIN_DIR"
DEFAULT_KEYCHAIN_DIR = "/etc/prius/satellites"
FALLBACK_KEYCHAIN_DIR = "satellites"     # relative to the working directory

CAR_KEY_FILE = "car.key"
CAR_PUB_FILE = "car.pub"
KEYCHAIN_FILE = "keychain.json"
LOCK_FILE = ".keychain.lock"

KEYCHAIN_VERSION = 1
CERT_VERSION = 1
BLOB_VERSION = 1

AUTH_NONE = "none"
AUTH_ED25519 = "ed25519"


class KeychainError(Exception):
    """Bad keychain operation (unknown id, name taken, range full …)."""


class CryptoUnavailable(KeychainError):
    """The ``cryptography`` package is missing (needed for Ed25519)."""


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (no cryptography needed)
# ─────────────────────────────────────────────────────────────────────────────

def b64e(data: bytes) -> str:
    """Standard base64 with padding (what the satellites parse)."""
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    """Strict standard-base64 decode; raises ValueError on garbage."""
    if not isinstance(text, str):
        raise ValueError("base64 value must be a string")
    return base64.b64decode(text.encode("ascii"), validate=True)


def fingerprint(pubkey_raw: bytes) -> str:
    """``fp`` = first 16 bytes of SHA-256(raw pubkey), base32 lower, no padding (26 chars)."""
    digest = hashlib.sha256(pubkey_raw).digest()[:16]
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def fingerprint_b64(pubkey_b64: str) -> str:
    return fingerprint(b64d(pubkey_b64))


def canonical_json(obj: Any) -> bytes:
    """The one serialization that gets signed: sorted keys, no spaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def transport_for_id(sat_id: int) -> Optional[str]:
    """Which transport's id range ``sat_id`` falls into (None if none)."""
    for name, (lo, hi) in ID_RANGES.items():
        if lo <= sat_id <= hi:
            return name
    return None


def id_in_range(sat_id: int, transport: str) -> bool:
    rng = ID_RANGES.get(transport)
    return rng is not None and rng[0] <= sat_id <= rng[1]


# ─────────────────────────────────────────────────────────────────────────────
# Ed25519 (lazy cryptography import)
# ─────────────────────────────────────────────────────────────────────────────

def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization
        from cryptography.exceptions import InvalidSignature
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise CryptoUnavailable(
            "Ed25519 needs the 'cryptography' package (pip install cryptography / "
            "apk add py3-cryptography); BLE/Wi-Fi satellite auth is unavailable"
        ) from exc
    return ed25519, serialization, InvalidSignature


def crypto_available() -> bool:
    try:
        _ed25519()
        return True
    except CryptoUnavailable:
        return False


def generate_keypair() -> Tuple[bytes, bytes]:
    """Return ``(private_raw_32, public_raw_32)``."""
    ed25519, serialization, _ = _ed25519()
    priv = ed25519.Ed25519PrivateKey.generate()
    return _priv_raw(priv), _pub_raw(priv.public_key())


def _priv_raw(priv) -> bytes:
    _, serialization, _ = _ed25519()
    return priv.private_bytes(serialization.Encoding.Raw,
                              serialization.PrivateFormat.Raw,
                              serialization.NoEncryption())


def _pub_raw(pub) -> bytes:
    _, serialization, _ = _ed25519()
    return pub.public_bytes(serialization.Encoding.Raw,
                            serialization.PublicFormat.Raw)


def public_from_private(priv_raw: bytes) -> bytes:
    ed25519, _, _ = _ed25519()
    return _pub_raw(ed25519.Ed25519PrivateKey.from_private_bytes(priv_raw).public_key())


def sign(priv_raw: bytes, message: bytes) -> bytes:
    ed25519, _, _ = _ed25519()
    return ed25519.Ed25519PrivateKey.from_private_bytes(priv_raw).sign(message)


def verify(pub_raw: bytes, signature: bytes, message: bytes) -> bool:
    """True iff ``signature`` is a valid Ed25519 signature of ``message``."""
    ed25519, _, InvalidSignature = _ed25519()
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(pub_raw).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


def private_to_pem(priv_raw: bytes) -> bytes:
    ed25519, serialization, _ = _ed25519()
    return ed25519.Ed25519PrivateKey.from_private_bytes(priv_raw).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())


def public_to_pem(pub_raw: bytes) -> bytes:
    ed25519, serialization, _ = _ed25519()
    return ed25519.Ed25519PublicKey.from_public_bytes(pub_raw).public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def private_from_pem(pem: bytes) -> bytes:
    ed25519, serialization, _ = _ed25519()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise KeychainError("car.key is not an Ed25519 private key")
    return _priv_raw(key)


def public_from_pem(pem: bytes) -> bytes:
    ed25519, serialization, _ = _ed25519()
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise KeychainError("car.pub is not an Ed25519 public key")
    return _pub_raw(key)


# ─────────────────────────────────────────────────────────────────────────────
# Certificates
# ─────────────────────────────────────────────────────────────────────────────

CERT_FIELDS = ("v", "sat_id", "name", "transport", "fp", "pubkey", "caps", "issued", "expires")


def cert_body(sat_id: int, name: str, transport: str, pubkey_b64: str,
              caps: Optional[List[str]] = None, issued: Optional[int] = None,
              expires: Optional[int] = None) -> Dict[str, Any]:
    """The signed part of a certificate (docs §2)."""
    return {
        "v": CERT_VERSION,
        "sat_id": int(sat_id),
        "name": str(name),
        "transport": str(transport),
        "fp": fingerprint_b64(pubkey_b64),
        "pubkey": pubkey_b64,
        "caps": list(caps or []),
        "issued": int(issued if issued is not None else time.time()),
        "expires": expires,
    }


def sign_cert(car_priv_raw: bytes, body: Dict[str, Any]) -> Dict[str, Any]:
    """Certificate = body + ``"sig"`` (car Ed25519 signature over the canonical
    JSON of the body, i.e. of the certificate without its ``sig`` key)."""
    unsigned = {k: body[k] for k in CERT_FIELDS}
    cert = dict(unsigned)
    cert["sig"] = b64e(sign(car_priv_raw, canonical_json(unsigned)))
    return cert


def verify_cert(car_pub_raw: bytes, cert: Any, now: Optional[float] = None) -> Optional[str]:
    """Check a certificate. Returns ``None`` when valid, else a short reason."""
    if not isinstance(cert, dict):
        return "cert not an object"
    if cert.get("v") != CERT_VERSION:
        return "cert version"
    missing = [k for k in CERT_FIELDS if k not in cert]
    if missing or "sig" not in cert:
        return "cert fields missing"
    try:
        sig = b64d(cert["sig"])
        pub = b64d(cert["pubkey"])
        sat_id = int(cert["sat_id"])
    except (ValueError, TypeError):
        return "cert encoding"
    if len(pub) != 32:
        return "cert pubkey length"
    if fingerprint(pub) != cert["fp"]:
        return "cert fp/pubkey mismatch"
    if cert["transport"] not in TRANSPORTS or not id_in_range(sat_id, cert["transport"]):
        return "cert id/transport"
    expires = cert.get("expires")
    if expires is not None:
        try:
            if float(expires) < (now if now is not None else time.time()):
                return "cert expired"
        except (TypeError, ValueError):
            return "cert expires"
    unsigned = {k: cert[k] for k in CERT_FIELDS}
    if not verify(car_pub_raw, sig, canonical_json(unsigned)):
        return "cert signature"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Files
# ─────────────────────────────────────────────────────────────────────────────

def _dir_usable(path: str) -> bool:
    if os.path.isdir(path):
        return os.access(path, os.W_OK | os.X_OK)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        return True
    except OSError:
        return False


def resolve_keychain_dir(explicit: Optional[str] = None, warn: bool = True) -> str:
    """Pick the keychain directory: ``explicit`` > env > default, falling back
    to ``./satellites`` when the chosen one is not writable."""
    wanted = explicit or os.environ.get(ENV_KEYCHAIN_DIR) or DEFAULT_KEYCHAIN_DIR
    if _dir_usable(wanted):
        return wanted
    fallback = os.path.abspath(FALLBACK_KEYCHAIN_DIR)
    if warn:
        logger.warning("Satellite keychain dir %s not writable; falling back to %s",
                       wanted, fallback)
    os.makedirs(fallback, mode=0o700, exist_ok=True)
    return fallback


def atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically with the given permissions."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _empty_doc() -> Dict[str, Any]:
    return {"version": KEYCHAIN_VERSION, "car_fp": None, "satellites": {}, "revoked": []}


# ─────────────────────────────────────────────────────────────────────────────
# Keychain
# ─────────────────────────────────────────────────────────────────────────────

class Keychain:
    """``keychain.json`` + the car key pair. Not thread-safe; one owner thread
    (the backend engine loop, or the CLI process)."""

    def __init__(self, directory: Optional[str] = None, warn: bool = True):
        self.directory = resolve_keychain_dir(directory, warn=warn)
        self.path = os.path.join(self.directory, KEYCHAIN_FILE)
        self.car_key_path = os.path.join(self.directory, CAR_KEY_FILE)
        self.car_pub_path = os.path.join(self.directory, CAR_PUB_FILE)
        self._doc: Dict[str, Any] = _empty_doc()
        self._mtime: Optional[float] = None
        self._loaded_stamp: Tuple[Optional[int], Optional[int]] = (None, None)
        self._car_priv: Optional[bytes] = None
        self._car_pub: Optional[bytes] = None
        self.load()

    # ── load / save ─────────────────────────────────────────────────────────

    def load(self) -> None:
        """(Re)read keychain.json and car.pub. Missing files = empty keychain."""
        self._loaded_stamp = self._stamp()
        try:
            st = os.stat(self.path)
            with open(self.path, "rb") as fh:
                doc = json.loads(fh.read().decode("utf-8") or "{}")
            self._mtime = st.st_mtime_ns
        except FileNotFoundError:
            doc = _empty_doc()
            self._mtime = None
        except (OSError, ValueError) as exc:
            logger.error("Satellite keychain %s unreadable (%s); using empty", self.path, exc)
            doc = _empty_doc()
        base = _empty_doc()
        base.update({k: v for k, v in doc.items() if k in base or k.startswith("x_")})
        if not isinstance(base.get("satellites"), dict):
            base["satellites"] = {}
        if not isinstance(base.get("revoked"), list):
            base["revoked"] = []
        self._doc = base
        self._car_priv = None   # re-read lazily (key may have been created by the CLI)
        self._car_pub = None
        with contextlib.suppress(OSError, KeychainError, CryptoUnavailable):
            with open(self.car_pub_path, "rb") as fh:
                self._car_pub = public_from_pem(fh.read())

    def _stamp(self) -> Tuple[Optional[int], Optional[int]]:
        out = []
        for path in (self.path, self.car_pub_path):
            try:
                out.append(os.stat(path).st_mtime_ns)
            except OSError:
                out.append(None)
        return out[0], out[1]

    def reload_if_changed(self) -> bool:
        """Reload when another process (prius-satkey) rewrote the files."""
        stamp = self._stamp()
        if stamp == self._loaded_stamp:
            return False
        self.load()
        return True

    def save(self) -> None:
        if self._car_pub is not None:
            self._doc["car_fp"] = fingerprint(self._car_pub)
        data = (json.dumps(self._doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
        atomic_write(self.path, data, 0o600)
        self._loaded_stamp = self._stamp()

    @contextlib.contextmanager
    def transaction(self) -> Iterator["Keychain"]:
        """Locked read-modify-write; saves on clean exit."""
        lock_path = os.path.join(self.directory, LOCK_FILE)
        fh = open(lock_path, "a+")
        try:
            with contextlib.suppress(OSError):
                os.chmod(lock_path, 0o600)
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            self.load()
            yield self
            self.save()
        finally:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    # ── car key ─────────────────────────────────────────────────────────────

    def has_car_key(self) -> bool:
        return os.path.exists(self.car_key_path) and os.path.exists(self.car_pub_path)

    def init_car_key(self) -> Tuple[str, bool]:
        """Create car.key/car.pub when missing. Returns ``(car_fp, created)``."""
        if self.has_car_key():
            return self.car_fp or "", False
        priv, pub = generate_keypair()
        atomic_write(self.car_key_path, private_to_pem(priv), 0o600)
        atomic_write(self.car_pub_path, public_to_pem(pub), 0o644)
        self._car_priv, self._car_pub = priv, pub
        with self.transaction():
            self._doc["car_fp"] = fingerprint(pub)
        self._car_priv, self._car_pub = priv, pub
        logger.info("Satellite car key created in %s (car_fp=%s)", self.directory, fingerprint(pub))
        return fingerprint(pub), True

    @property
    def car_pub(self) -> Optional[bytes]:
        if self._car_pub is None:
            with contextlib.suppress(OSError, KeychainError):
                with open(self.car_pub_path, "rb") as fh:
                    self._car_pub = public_from_pem(fh.read())
        return self._car_pub

    @property
    def car_priv(self) -> Optional[bytes]:
        if self._car_priv is None:
            with contextlib.suppress(OSError, KeychainError):
                with open(self.car_key_path, "rb") as fh:
                    self._car_priv = private_from_pem(fh.read())
        return self._car_priv

    @property
    def car_fp(self) -> Optional[str]:
        pub = self.car_pub
        return fingerprint(pub) if pub is not None else None

    def car_sign(self, message: bytes) -> bytes:
        priv = self.car_priv
        if priv is None:
            raise KeychainError("no car key (run: prius-satkey init)")
        return sign(priv, message)

    # ── queries ─────────────────────────────────────────────────────────────

    @property
    def revoked(self) -> List[str]:
        return list(self._doc["revoked"])

    def is_revoked(self, fp: Optional[str]) -> bool:
        return bool(fp) and fp in self._doc["revoked"]

    def entries(self) -> Dict[int, Dict[str, Any]]:
        """``{sat_id: entry}`` (deep copies)."""
        out = {}
        for key, entry in self._doc["satellites"].items():
            try:
                out[int(key)] = copy.deepcopy(entry)
            except (TypeError, ValueError):
                continue
        return out

    def get(self, sat_id: int) -> Optional[Dict[str, Any]]:
        entry = self._doc["satellites"].get(str(int(sat_id)))
        return copy.deepcopy(entry) if entry is not None else None

    def by_fp(self, fp: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        for sat_id, entry in self.entries().items():
            if entry.get("fp") == fp:
                return sat_id, entry
        return None

    def by_name(self, name: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        for sat_id, entry in self.entries().items():
            if entry.get("name") == name:
                return sat_id, entry
        return None

    def resolve(self, ref: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        """Find an entry by id, fingerprint or name."""
        ref = str(ref).strip()
        if ref.isdigit():
            entry = self.get(int(ref))
            if entry is not None:
                return int(ref), entry
        return self.by_fp(ref) or self.by_name(ref)

    def ids_for(self, transport: str) -> List[int]:
        return sorted(i for i, e in self.entries().items() if e.get("transport") == transport)

    def free_id(self, transport: str) -> int:
        """Lowest unused id in the transport's range."""
        rng = ID_RANGES.get(transport)
        if rng is None:
            raise KeychainError(f"unknown transport {transport!r}")
        used = set(self.entries())
        for sat_id in range(rng[0], rng[1] + 1):
            if sat_id not in used:
                return sat_id
        raise KeychainError(f"no free satellite id left for {transport}")

    def requires_auth(self, sat_id: int) -> bool:
        entry = self.get(sat_id)
        transport = (entry or {}).get("transport") or transport_for_id(sat_id) or ""
        if entry is not None and entry.get("auth") == AUTH_ED25519:
            return True
        return REQUIRE_AUTH.get(transport, True)

    # ── mutations (callers wrap in transaction() when persisting) ───────────

    def _check_new(self, sat_id: int, name: str, transport: str, fp: Optional[str]) -> None:
        if transport not in TRANSPORTS:
            raise KeychainError(f"unknown transport {transport!r}")
        if not id_in_range(sat_id, transport):
            lo, hi = ID_RANGES[transport]
            raise KeychainError(f"id {sat_id} outside the {transport} range {lo}-{hi}")
        if not name or len(name) > 32 or any(c.isspace() for c in name):
            raise KeychainError("name must be a short slug without spaces")
        existing = self.get(sat_id)
        if existing is not None:
            raise KeychainError(f"id {sat_id} already enrolled ({existing.get('name')})")
        other = self.by_name(name)
        if other is not None:
            raise KeychainError(f"name {name!r} already used by id {other[0]}")
        if fp is not None:
            if self.is_revoked(fp):
                raise KeychainError(f"fingerprint {fp} is revoked")
            dup = self.by_fp(fp)
            if dup is not None:
                raise KeychainError(f"fingerprint {fp} already enrolled as id {dup[0]}")

    def add_rs485(self, sat_id: int, name: str, caps: Optional[List[str]] = None) -> Dict[str, Any]:
        """Enrol a wired RS485 satellite (``auth: none``)."""
        self._check_new(sat_id, name, TRANSPORT_RS485, None)
        entry = {"name": name, "transport": TRANSPORT_RS485, "auth": AUTH_NONE,
                 "address": str(sat_id), "caps": list(caps or []),
                 "enrolled": int(time.time())}
        self._doc["satellites"][str(sat_id)] = entry
        return copy.deepcopy(entry)

    def enrol_pubkey(self, name: str, transport: str, pubkey_b64: str,
                     sat_id: Optional[int] = None, caps: Optional[List[str]] = None,
                     address: str = "", expires: Optional[int] = None) -> Tuple[int, Dict[str, Any]]:
        """Enrol a satellite by its public key and issue a cert (when the car
        private key is present). Returns ``(sat_id, entry)``."""
        if transport == TRANSPORT_RS485:
            raise KeychainError("RS485 satellites are enrolled without a key (add_rs485)")
        pub = b64d(pubkey_b64)
        if len(pub) != 32:
            raise KeychainError("pubkey must be 32 raw bytes (base64)")
        fp = fingerprint(pub)
        if sat_id is None:
            sat_id = self.free_id(transport)
        self._check_new(int(sat_id), name, transport, fp)
        now = int(time.time())
        entry: Dict[str, Any] = {
            "name": name, "transport": transport, "fp": fp, "pubkey": pubkey_b64,
            "address": address, "caps": list(caps or []), "enrolled": now,
            "cert": None, "auth": AUTH_ED25519,
        }
        if self.car_priv is not None:
            entry["cert"] = sign_cert(self.car_priv, cert_body(
                int(sat_id), name, transport, pubkey_b64, caps, now, expires))
        self._doc["satellites"][str(int(sat_id))] = entry
        return int(sat_id), copy.deepcopy(entry)

    def accept_cert(self, cert: Dict[str, Any], address: str = "") -> Tuple[int, Dict[str, Any]]:
        """Add a satellite that presented a valid cert (caller verified it).
        A taken name gets an ``-<id>`` suffix rather than refusing a validly
        signed satellite."""
        sat_id = int(cert["sat_id"])
        name = str(cert["name"])
        if self.by_name(name) is not None:
            name = f"{name}-{sat_id}"
        self._check_new(sat_id, name, cert["transport"], cert["fp"])
        entry = {
            "name": name, "transport": cert["transport"], "fp": cert["fp"],
            "pubkey": cert["pubkey"], "address": address, "caps": list(cert.get("caps") or []),
            "enrolled": int(time.time()), "cert": dict(cert), "auth": AUTH_ED25519,
        }
        self._doc["satellites"][str(sat_id)] = entry
        return sat_id, copy.deepcopy(entry)

    def update_entry(self, sat_id: int, **fields: Any) -> None:
        entry = self._doc["satellites"].get(str(int(sat_id)))
        if entry is None:
            raise KeychainError(f"id {sat_id} not enrolled")
        entry.update(fields)

    def forget(self, sat_id: int) -> Dict[str, Any]:
        """Remove an entry (not revoked: a valid cert can bring it back)."""
        entry = self._doc["satellites"].pop(str(int(sat_id)), None)
        if entry is None:
            raise KeychainError(f"id {sat_id} not enrolled")
        return entry

    def revoke(self, ref: str) -> Tuple[Optional[int], Optional[str]]:
        """Revoke by id, fingerprint or name: the fp goes on the revocation
        list (refused forever, cert or not) and the entry is removed.
        A bare fingerprint that is not enrolled is revoked as well."""
        found = self.resolve(ref)
        sat_id: Optional[int] = None
        fp: Optional[str] = None
        if found is not None:
            sat_id, entry = found
            fp = entry.get("fp")
            if not fp:
                # auth "none" (RS485): no key, nothing a revocation could refuse.
                raise KeychainError("nothing to revoke (no fingerprint; use forget)")
            self._doc["satellites"].pop(str(sat_id), None)
        elif len(str(ref)) == 26 and str(ref).isalnum():
            fp = str(ref)
        else:
            raise KeychainError(f"nothing matches {ref!r}")
        if fp and fp not in self._doc["revoked"]:
            self._doc["revoked"].append(fp)
        return sat_id, fp

    # ── provisioning ────────────────────────────────────────────────────────

    def provisioning_blob(self, sat_id: int, priv_raw: Optional[bytes] = None,
                          wifi_port: int = 8788) -> Dict[str, Any]:
        """JSON to flash into a satellite: its id/name, (private key), cert and
        the car public key so it can verify ``auth_ok`` (mutual auth)."""
        entry = self.get(sat_id)
        if entry is None:
            raise KeychainError(f"id {sat_id} not enrolled")
        if self.car_pub is None:
            raise KeychainError("no car key (run: prius-satkey init)")
        blob: Dict[str, Any] = {
            "v": BLOB_VERSION, "sat_id": sat_id, "name": entry["name"],
            "transport": entry["transport"], "fp": entry.get("fp"),
            "pubkey": entry.get("pubkey"), "cert": entry.get("cert"),
            "car_pub": b64e(self.car_pub), "car_fp": self.car_fp,
        }
        if priv_raw is not None:
            blob["privkey"] = b64e(priv_raw)
        if entry["transport"] == "wifi":
            blob["wifi"] = {"ssid": "prius", "car_ip": "10.42.0.1", "port": wifi_port,
                            "beacon_port": 8787}
        if entry["transport"] == "ble":
            blob["ble"] = {"service": "580e04f7-d4c0-458b-b2e0-4ebb01a960c8",
                           "car_addr": "02:00:E8:51:02:58"}
        return blob

    def new_satellite(self, name: str, transport: str, sat_id: Optional[int] = None,
                      caps: Optional[List[str]] = None) -> Dict[str, Any]:
        """Generate a key pair, enrol it, return the provisioning blob."""
        if self.car_priv is None:
            raise KeychainError("no car key (run: prius-satkey init)")
        priv, pub = generate_keypair()
        sat_id, _ = self.enrol_pubkey(name, transport, b64e(pub), sat_id=sat_id, caps=caps)
        return self.provisioning_blob(sat_id, priv_raw=priv)
