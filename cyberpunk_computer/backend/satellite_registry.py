"""
SatelliteRegistry — one API for satellites on every transport.

See ``docs/SATELLITES.md`` §5. The rest of the backend addresses a satellite
by its **satellite id**; the registry maps ids to transports and addresses,
runs the Ed25519 handshake on every BLE/Wi-Fi link, keeps the keychain, and
mirrors everything into ``SatellitesState`` (``nodes``, ``transports``,
``discovered``, ``pairing``).

What it owns:

* the transports (:class:`Rs485Transport` always, ``BleTransport`` when the
  BLE lane's module is installed, :class:`WifiTransport`), started/stopped
  and ticked from the engine loop;
* the :class:`~.satellite_keychain.Keychain` (reloaded when ``prius-satkey``
  edits it) and one :class:`~.satellite_auth.LinkAuth` per unauthenticated
  link;
* one :class:`~.satellites.SatelliteJobQueue` per transport. The RS485 queue
  is the pre-existing queue (gateway OutputPort, rail wake-lock, unchanged);
  BLE/Wi-Fi queues hold no wake-lock and wait for the medium plus an
  authenticated link to the job's satellite;
* the :class:`~.satellites.SatelliteScheduler` / :class:`SatelliteSupervisor`
  generalised over those queues;
* rediscovery cadence (``rediscover_s``; immediately on an availability edge)
  and the pairing window.

Wire format on BLE/Wi-Fi links (one NDJSON line each way):
``{"id": <sat_id>, "d": {...payload...}}`` — ``id`` omitted until
``auth_ok``. Incoming lines without a ``"d"`` key are taken as the payload
itself (lenient for small firmware).

Everything here runs on the engine thread (the only thread allowed to
dispatch into the store); transports hand over their IO through ``poll()``.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..io.ports import OutputPort, SATELLITE_NAMES
from ..io.satellite_transport import (
    DiscoveredDevice,
    ID_RANGES,
    LinkEvent,
    REQUIRE_AUTH,
    SatelliteTransport,
    TRANSPORT_BLE,
    TRANSPORT_RS485,
    TRANSPORT_WIFI,
    TRANSPORTS,
)
from ..state.actions import (
    ActionSource,
    RemoveSatelliteNodeAction,
    SetSatelliteRegistryAction,
    UpdateSatelliteNodeAction,
)
from ..state.store import Store
from .satellite_auth import FAILED, HELD, OK, PENDING, AUTH_TIMEOUT_S, LinkAuth
from .satellite_keychain import (
    Keychain,
    KeychainError,
    crypto_available,
    transport_for_id,
    verify_cert,
)
from .satellites import (
    PRIORITY_NORMAL,
    PRIORITY_SCHEDULED,
    SatelliteJob,
    SatelliteJobQueue,
    SatelliteScheduler,
    SatelliteSupervisor,
    command_job,
)

logger = logging.getLogger(__name__)

BLE_SOCKET_PATH = "/run/prius/bled.sock"
CONNECT_BACKOFF_S = 30.0       # Wi-Fi: wait before re-dialling an address that failed/was refused
OFFER_TTL_S = 600.0            # how long a proven-but-unknown pubkey stays enrolable
PUBLISH_PERIOD_S = 1.0
KEYCHAIN_CHECK_S = 2.0
ADV_INTERVAL_IDLE_MS = 10_000
ADV_INTERVAL_ACTIVE_MS = 1_000
CENTRAL_CHECK_S = 1.0          # how often the beacon address list is recomputed from the keychain
BLE_ROLE_BEACON = "beacon"     # keychain ``ble_role``: the satellite advertises, the car connects


class RegistryError(Exception):
    """Operator command refused (bad id, window closed, nothing to revoke …)."""


@dataclass
class _Link:
    transport: str
    address: str
    auth: Optional[LinkAuth]
    up_since: float
    sat_id: Optional[int] = None
    authenticated: bool = False
    last_ping: float = 0.0
    last_seen_dispatch: float = 0.0


@dataclass
class _Offer:
    """A device that proved possession of an unknown key (pairing window)."""
    transport: str
    address: str
    fp: str
    pubkey: str
    name: str = ""
    ts: float = 0.0              # time.time()
    cert_sat_id: Optional[int] = None


class SatelliteRegistry:
    """See module docstring."""

    def __init__(
        self,
        store: Store,
        output_port: Optional[OutputPort] = None,
        transports_enabled: Sequence[str] = TRANSPORTS,
        keychain: Optional[Keychain] = None,
        keychain_dir: Optional[str] = None,
        rediscover_s: float = 300.0,
        discovery_ttl_s: float = 600.0,
        offline_after_s: float = 15.0,
        desired_configs: Optional[Dict[int, dict]] = None,
        poll_devices: Iterable[int] = (),
        poll_payload: Optional[dict] = None,
        wifi_options: Optional[Dict[str, Any]] = None,
        ble_socket_path: str = BLE_SOCKET_PATH,
        transports: Optional[Dict[str, SatelliteTransport]] = None,
        on_message: Optional[Callable[[int, dict], None]] = None,
        auth_timeout_s: float = AUTH_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._store = store
        self._output_port = output_port
        self._clock = clock
        self.enabled = tuple(t for t in transports_enabled if t in TRANSPORTS)
        self.rediscover_s = rediscover_s
        self.discovery_ttl_s = discovery_ttl_s
        self.auth_timeout_s = auth_timeout_s
        self.on_message = on_message
        self._poll_devices = tuple(int(d) for d in poll_devices)
        self._poll_payload = dict(poll_payload or {"a": "status"})

        if keychain is None:
            try:
                keychain = Keychain(keychain_dir)
            except Exception:
                tmp = tempfile.mkdtemp(prefix="prius-satkeys-")
                logger.exception("Satellite keychain unusable; using a volatile one in %s", tmp)
                keychain = Keychain(tmp)
        self.keychain = keychain
        self._keychain_check = 0.0
        if not crypto_available():
            logger.warning("python 'cryptography' missing: BLE/Wi-Fi satellites cannot "
                           "authenticate (RS485 unaffected)")
        elif not self.keychain.has_car_key():
            logger.warning("No satellite car key in %s (run: prius-satkey init); "
                           "BLE/Wi-Fi satellites will be refused", self.keychain.directory)

        # ── transports ──────────────────────────────────────────────────────
        self.transports: Dict[str, SatelliteTransport] = {}
        self._missing: Dict[str, str] = {}
        if transports is not None:
            self.transports.update({k: v for k, v in transports.items() if k in self.enabled})
        else:
            self._build_transports(wifi_options or {}, ble_socket_path)

        # ── queues (one per transport) ──────────────────────────────────────
        self.queues: Dict[str, SatelliteJobQueue] = {}
        for name in self.enabled:
            if name == TRANSPORT_RS485:
                self.queues[name] = SatelliteJobQueue(store, output_port=output_port,
                                                      clock=clock)
            elif name in self.transports:
                self.queues[name] = SatelliteJobQueue(
                    store, clock=clock, transport=name, use_rail=False,
                    transport_ready=self._ready_fn(name), sender=self._send_payload)
        self.scheduler = SatelliteScheduler(self.queue, store, clock=clock, queues=self.queues)
        self.supervisor = SatelliteSupervisor(
            self.queue, store, desired_configs=desired_configs,
            offline_after_s=offline_after_s, clock=clock, queue_for=self.queue_for_id)
        self._sup_last = 0.0

        # ── runtime ─────────────────────────────────────────────────────────
        self._links: Dict[Tuple[str, str], _Link] = {}
        self._offers: Dict[Tuple[str, str], _Offer] = {}
        self._backoff: Dict[Tuple[str, str], float] = {}
        self._avail: Dict[str, bool] = {}
        self._next_discovery: Dict[str, float] = {}
        self._pairing = {"open": False, "until": 0.0, "transport": None, "opened_at": 0.0}
        self._published: Optional[tuple] = None
        self._publish_last = 0.0
        self._adv: Optional[Tuple[int, int]] = None
        self._central: Optional[Tuple[bool, Tuple[str, ...]]] = None
        self._central_check = float("-inf")
        self._seeded_ids: set = set()
        self._started = False

    # ── construction helpers ────────────────────────────────────────────────

    def _build_transports(self, wifi_options: Dict[str, Any], ble_socket_path: str) -> None:
        from ..io.rs485_transport import Rs485Transport
        if TRANSPORT_RS485 in self.enabled:
            self.transports[TRANSPORT_RS485] = Rs485Transport(
                self._output_port, lambda: self._store.state,
                candidate_ids=self._rs485_candidates,
                request_poll=self._rs485_request_poll,
                discovery_ttl_s=self.discovery_ttl_s,
                poll_payload=self._poll_payload,
            )
        if TRANSPORT_BLE in self.enabled:
            try:
                from ..io.ble_transport import BleTransport  # built by the BLE lane
                self.transports[TRANSPORT_BLE] = BleTransport(socket_path=ble_socket_path)
            except ImportError as exc:
                self._missing[TRANSPORT_BLE] = "BLE transport not installed"
                logger.warning("BLE satellite transport unavailable (%s); running without BLE", exc)
            except Exception:
                self._missing[TRANSPORT_BLE] = "BLE transport failed to build"
                logger.exception("BLE satellite transport failed to build; running without BLE")
        if TRANSPORT_WIFI in self.enabled:
            from ..io.wifi_transport import WifiTransport
            opts = dict(wifi_options)
            opts.setdefault("discovery_ttl_s", self.discovery_ttl_s)
            self.transports[TRANSPORT_WIFI] = WifiTransport(**opts)

    def _ready_fn(self, name: str) -> Callable[[], bool]:
        def ready() -> bool:
            t = self.transports.get(name)
            return t is not None and t.status().available
        return ready

    @property
    def queue(self) -> SatelliteJobQueue:
        """The RS485 queue (legacy single-queue API); a stand-in rail queue
        when RS485 is disabled so callers never see None."""
        q = self.queues.get(TRANSPORT_RS485)
        if q is None:
            q = SatelliteJobQueue(self._store, output_port=self._output_port, clock=self._clock)
            self.queues[TRANSPORT_RS485] = q
        return q

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.supervisor.seed()
        self._seed_from_keychain()
        car_fp = self.keychain.car_fp
        for name, t in self.transports.items():
            if hasattr(t, "set_car_fp"):
                t.set_car_fp(car_fp)
            try:
                ok = t.start()
                logger.info("Satellite transport %s %s", name, "started" if ok else "failed to start")
            except Exception:
                logger.exception("Satellite transport %s failed to start", name)
        self._publish(force=True)

    def stop(self) -> None:
        for name, t in self.transports.items():
            try:
                t.stop()
            except Exception:
                logger.exception("Satellite transport %s stop failed", name)
        self._started = False

    # ── id / transport mapping ──────────────────────────────────────────────

    def transport_of(self, sat_id: int) -> Optional[str]:
        entry = self.keychain.get(sat_id)
        if entry is not None and entry.get("transport") in TRANSPORTS:
            return entry["transport"]
        return transport_for_id(int(sat_id))

    def queue_for_id(self, sat_id: int) -> Optional[SatelliteJobQueue]:
        t = self.transport_of(sat_id)
        return self.queues.get(t) if t else None

    def _link_for(self, sat_id: int) -> Optional[_Link]:
        for link in self._links.values():
            if link.authenticated and link.sat_id == sat_id:
                return link
        return None

    # ── public API ──────────────────────────────────────────────────────────

    def send(self, sat_id: int, payload: dict, priority: int = PRIORITY_NORMAL,
             name: str = "") -> Optional[SatelliteJob]:
        """Queue one command to ``sat_id`` on its transport's queue."""
        sat_id = int(sat_id)
        t = self.transport_of(sat_id)
        queue = self.queues.get(t) if t else None
        if queue is None:
            logger.warning("Satellite send to %d dropped: transport %s not available", sat_id, t)
            return None
        # BLE/Wi-Fi: the job waits for an authenticated link (node online).
        requires = () if t == TRANSPORT_RS485 else (sat_id,)
        job = command_job(sat_id, payload, name=name, priority=priority, requires_online=requires)
        queue.submit(job)
        return job

    def _envelope(self, payload: dict, sat_id: Optional[int] = None) -> str:
        obj: Dict[str, Any] = {"d": payload}
        if sat_id is not None:
            obj = {"id": sat_id, "d": payload}
        return json.dumps(obj, separators=(",", ":"))

    def _send_payload(self, sat_id: int, payload: dict, priority: int = PRIORITY_NORMAL) -> bool:
        """JobContext sender for BLE/Wi-Fi queues: only over an authenticated link."""
        link = self._link_for(int(sat_id))
        if link is None:
            logger.warning("Satellite %d not linked/authenticated; send dropped", sat_id)
            return False
        t = self.transports.get(link.transport)
        return bool(t and t.send(link.address, self._envelope(payload, sat_id)))

    def nodes(self):
        return self._store.state.satellites.nodes

    def discovered(self) -> List[Dict[str, Any]]:
        return self._discovered_list()

    @property
    def pairing(self) -> Dict[str, Any]:
        return dict(self._pairing)

    def pairing_open_for(self, transport: str) -> bool:
        p = self._pairing
        return bool(p["open"]) and (p["transport"] in (None, transport))

    def pair_open(self, seconds: float = 120.0, transport: Optional[str] = None) -> None:
        if transport is not None and transport not in TRANSPORTS:
            raise RegistryError(f"unknown transport {transport!r}")
        now = time.time()
        if seconds and seconds > 0:
            self._pairing = {"open": True, "until": now + float(seconds),
                             "transport": transport, "opened_at": now}
            logger.info("Satellite pairing window open for %.0fs (%s)", seconds, transport or "all")
        else:
            self._close_pairing("closed by operator")
            return
        for name, t in self.transports.items():
            if transport in (None, name):
                try:
                    t.set_pairable(float(seconds))
                except Exception:
                    logger.exception("set_pairable on %s failed", name)
        self._update_adv()
        self._update_central(force=True)
        self._publish(force=True)

    def _close_pairing(self, why: str) -> None:
        was = self._pairing
        self._pairing = {"open": False, "until": 0.0, "transport": None, "opened_at": 0.0}
        if was["open"]:
            logger.info("Satellite pairing window closed (%s)", why)
            for name, t in self.transports.items():
                if was["transport"] in (None, name):
                    try:
                        t.set_pairable(0)
                    except Exception:
                        logger.exception("set_pairable(0) on %s failed", name)
        self._update_adv()
        self._update_central(force=True)
        self._publish(force=True)

    def discover(self, transport: Optional[str] = None) -> None:
        """Force a rediscovery pass now (operator)."""
        for name, t in self.transports.items():
            if transport in (None, name):
                try:
                    t.discover(force=True)
                except Exception:
                    logger.exception("discover on %s failed", name)
                self._next_discovery[name] = self._clock() + self.rediscover_s

    def enrol(self, transport: str, address: str, name: str, sat_id: Optional[int] = None,
              caps: Optional[List[str]] = None) -> int:
        """Enrol a device. RS485: wired, no window needed. BLE/Wi-Fi: the
        pairing window must be open and the device must have offered a pubkey."""
        if transport not in TRANSPORTS:
            raise RegistryError(f"unknown transport {transport!r}")
        caps = [str(c) for c in (caps or [])]
        if transport == TRANSPORT_RS485:
            if sat_id is None:
                try:
                    sat_id = int(address)
                except (TypeError, ValueError):
                    raise RegistryError("RS485 enrolment needs sat_id or a numeric address")
            try:
                with self.keychain.transaction() as kc:
                    kc.add_rs485(int(sat_id), name, caps)
            except KeychainError as exc:
                raise RegistryError(str(exc))
            self._store.dispatch(UpdateSatelliteNodeAction(
                int(sat_id), name=name, transport=TRANSPORT_RS485, trusted=True,
                address=str(sat_id), caps=caps, source=ActionSource.INTERNAL))
            self._seeded_ids.add(int(sat_id))
            self._publish(force=True)
            return int(sat_id)

        if not self.pairing_open_for(transport):
            raise RegistryError("pairing window is closed")
        offer = self._offers.get((transport, address))
        if offer is None:
            raise RegistryError("device has not offered a public key")
        try:
            with self.keychain.transaction() as kc:
                sat_id, entry = kc.enrol_pubkey(name, transport, offer.pubkey, sat_id=sat_id,
                                                caps=caps, address=address)
        except KeychainError as exc:
            raise RegistryError(str(exc))
        self._offers.pop((transport, address), None)
        self._seed_entry(sat_id, entry)
        link = self._links.get((transport, address))
        if link is not None and link.auth is not None and link.auth.state == HELD:
            reply = link.auth.auth_ok(sat_id, entry.get("cert"))
            self._authenticated(link, sat_id, entry)
            self._raw_send(link, reply)
        self._publish(force=True)
        return sat_id

    def forget(self, sat_id: int) -> None:
        sat_id = int(sat_id)
        try:
            with self.keychain.transaction() as kc:
                kc.forget(sat_id)
        except KeychainError as exc:
            raise RegistryError(str(exc))
        self._drop_sat(sat_id, "forgotten")

    def revoke(self, sat_id: int) -> None:
        sat_id = int(sat_id)
        entry = self.keychain.get(sat_id)
        if entry is None:
            raise RegistryError(f"id {sat_id} not enrolled")
        if not entry.get("fp"):
            raise RegistryError("nothing to revoke")
        try:
            with self.keychain.transaction() as kc:
                kc.revoke(str(sat_id))
        except KeychainError as exc:
            raise RegistryError(str(exc))
        self._drop_sat(sat_id, "revoked")

    def _drop_sat(self, sat_id: int, why: str) -> None:
        for key, link in list(self._links.items()):
            if link.sat_id == sat_id:
                self._disconnect(link, why)
        self._seeded_ids.discard(sat_id)
        if sat_id in SATELLITE_NAMES:
            self._store.dispatch(UpdateSatelliteNodeAction(
                sat_id, name=SATELLITE_NAMES[sat_id], caps=[], source=ActionSource.INTERNAL))
        else:
            self._store.dispatch(RemoveSatelliteNodeAction(sat_id))
        logger.info("Satellite %d %s", sat_id, why)
        self._publish(force=True)

    def handle_command(self, op: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Run one operator command (REST via SatelliteRegistryCommandAction).
        Returns ``{"ok":bool, "error":str, ...}``; never raises."""
        try:
            if op == "discover":
                self.discover(params.get("transport"))
                res: Dict[str, Any] = {}
            elif op == "pair_open":
                self.pair_open(float(params.get("seconds", 120)), params.get("transport"))
                res = {}
            elif op == "enrol":
                sid = self.enrol(params["transport"], str(params.get("address", "")),
                                 params["name"], params.get("sat_id"), params.get("caps"))
                res = {"sat_id": sid}
            elif op == "forget":
                self.forget(int(params["sat_id"]))
                res = {}
            elif op == "revoke":
                self.revoke(int(params["sat_id"]))
                res = {}
            else:
                raise RegistryError(f"unknown op {op!r}")
        except (RegistryError, KeychainError, KeyError, ValueError, TypeError) as exc:
            logger.warning("Satellite command %s %s refused: %s", op, params, exc)
            result: Dict[str, Any] = {"op": op, "ok": False, "error": str(exc), "ts": time.time()}
        else:
            logger.info("Satellite command %s %s: ok %s", op, params, res or "")
            result = {"op": op, "ok": True, "error": "", "ts": time.time(), **res}
        self._store.dispatch(SetSatelliteRegistryAction(last_command=result))
        return result

    # ── keychain → nodes ────────────────────────────────────────────────────

    def _seed_entry(self, sat_id: int, entry: Dict[str, Any]) -> None:
        t = entry.get("transport") or transport_for_id(sat_id) or TRANSPORT_RS485
        kw: Dict[str, Any] = dict(name=entry.get("name") or None, transport=t, trusted=True,
                                  caps=list(entry.get("caps") or []))
        if entry.get("fp"):
            kw["fp"] = entry["fp"]
        if t == TRANSPORT_RS485:
            kw["address"] = str(sat_id)
        self._store.dispatch(UpdateSatelliteNodeAction(sat_id, source=ActionSource.INTERNAL, **kw))
        self._seeded_ids.add(sat_id)

    def _seed_from_keychain(self) -> None:
        entries = self.keychain.entries()
        for sat_id, entry in entries.items():
            self._seed_entry(sat_id, entry)
        # Entries removed by prius-satkey: drop their links/nodes.
        for sat_id in list(self._seeded_ids):
            if sat_id not in entries:
                self._seeded_ids.discard(sat_id)
                for link in list(self._links.values()):
                    if link.sat_id == sat_id:
                        self._disconnect(link, "removed from keychain")
                if sat_id not in SATELLITE_NAMES and transport_for_id(sat_id) != TRANSPORT_RS485:
                    self._store.dispatch(RemoveSatelliteNodeAction(sat_id))
        # Revoked fingerprints: drop live links.
        for link in list(self._links.values()):
            if link.auth is not None and self.keychain.is_revoked(link.auth.fp):
                self._disconnect(link, "revoked")

    # ── RS485 rediscovery through the RS485 queue ───────────────────────────

    def _rs485_candidates(self) -> List[int]:
        ids = set(self.keychain.ids_for(TRANSPORT_RS485))
        ids.update(self._poll_devices)
        ids.update(SATELLITE_NAMES)
        return sorted(ids)

    def _rs485_request_poll(self, ids: List[int], payload: dict) -> None:
        queue = self.queues.get(TRANSPORT_RS485)
        t = self.transports.get(TRANSPORT_RS485)
        if queue is None or queue.has("disc:rs485"):
            return

        def start(ctx) -> None:
            for dev in ids:
                ctx.send(dev, dict(payload))

        def done(_ok: bool) -> None:
            if t is not None and hasattr(t, "finish_discovery"):
                t.finish_discovery()

        queue.submit(SatelliteJob(name="disc:rs485", start=start,
                                  priority=PRIORITY_SCHEDULED, on_done=done))

    def _rs485_rail_confirmed(self) -> bool:
        pb = self._store.state.powerbox
        return bool(pb.connected and pb.rs485_power)

    # ── engine tick ─────────────────────────────────────────────────────────

    def tick(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        if not self._started:
            self.start()

        if now - self._keychain_check >= KEYCHAIN_CHECK_S:
            self._keychain_check = now
            try:
                if self.keychain.reload_if_changed():
                    logger.info("Satellite keychain changed on disk; reloaded")
                    self._seed_from_keychain()
                    for t in self.transports.values():
                        if hasattr(t, "set_car_fp"):
                            t.set_car_fp(self.keychain.car_fp)
            except Exception:
                logger.exception("Satellite keychain reload failed")

        if self._pairing["open"] and time.time() >= self._pairing["until"]:
            self._close_pairing("expired")

        for name, t in self.transports.items():
            try:
                t.tick(now)
            except Exception:
                logger.exception("Satellite transport %s tick failed", name)
            for _ in range(256):
                ev = t.poll()
                if ev is None:
                    break
                try:
                    self._on_event(t, ev, now)
                except Exception:
                    logger.exception("Satellite %s event %s failed", name, ev.kind)

        self._auth_housekeeping(now)
        self._discovery_cadence(now)
        self._keepalives(now)
        self._auto_connect(now)
        self._update_adv()
        self._update_central(now)

        for q in self.queues.values():
            q.tick()
        self.scheduler.tick()
        if now - self._sup_last >= 1.0:
            self._sup_last = now
            self.supervisor.tick()
        if now - self._publish_last >= PUBLISH_PERIOD_S:
            self._publish()

    # ── events ──────────────────────────────────────────────────────────────

    def _on_event(self, t: SatelliteTransport, ev: LinkEvent, now: float) -> None:
        key = (ev.transport or t.name, ev.address)
        if ev.kind == "up":
            self._link_up(t, key, now)
        elif ev.kind == "down":
            link = self._links.pop(key, None)
            if link is not None:
                self._link_gone(link, (ev.extra or {}).get("why", "down"))
            self._backoff[key] = now + CONNECT_BACKOFF_S
        elif ev.kind == "data":
            self._on_data(t, key, ev.line, now)
        elif ev.kind == "discovery":
            self._publish_last = 0.0   # publish promptly

    def _requires_auth(self, transport: str) -> bool:
        return REQUIRE_AUTH.get(transport, True)

    def _link_up(self, t: SatelliteTransport, key: Tuple[str, str], now: float) -> Optional[_Link]:
        old = self._links.get(key)
        if old is not None:
            return old
        transport, address = key
        auth = LinkAuth(self.keychain, transport, address, now=now, timeout_s=self.auth_timeout_s)
        link = _Link(transport, address, auth, up_since=now)
        self._links[key] = link
        challenge = auth.challenge()
        if challenge is None:
            logger.warning("Satellite link %s %s refused: car has no key (prius-satkey init)",
                           transport, address)
            self._raw_send(link, {"a": "auth_fail", "why": "car has no key"})
            self._disconnect(link, "car has no key")
            return None
        self._raw_send(link, challenge)
        return link

    def _raw_send(self, link: _Link, payload: dict) -> bool:
        t = self.transports.get(link.transport)
        sid = link.sat_id if link.authenticated else None
        return bool(t and t.send(link.address, self._envelope(payload, sid)))

    def _disconnect(self, link: _Link, why: str) -> None:
        self._links.pop((link.transport, link.address), None)
        self._backoff[(link.transport, link.address)] = self._clock() + CONNECT_BACKOFF_S
        t = self.transports.get(link.transport)
        if t is not None:
            try:
                t.disconnect(link.address)
            except Exception:
                logger.exception("disconnect %s failed", link.address)
        self._link_gone(link, why)

    def _link_gone(self, link: _Link, why: str) -> None:
        if link.authenticated and link.sat_id is not None:
            # Another authenticated link to the same satellite may survive.
            if self._link_for(link.sat_id) is None:
                self._store.dispatch(UpdateSatelliteNodeAction(
                    link.sat_id, online=False, linked=False, authenticated=False,
                    source=ActionSource.INTERNAL))
            logger.info("Satellite %d link %s %s down (%s)", link.sat_id, link.transport,
                        link.address, why)
        self._publish_last = 0.0

    def _on_data(self, t: SatelliteTransport, key: Tuple[str, str], line: str, now: float) -> None:
        link = self._links.get(key)
        if link is None:
            # Data without an "up" (BLE may report the link lazily): start auth.
            link = self._link_up(t, key, now)
            if link is None:
                return
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            logger.debug("Satellite %s %s: non-JSON line dropped", *key)
            return
        if not isinstance(obj, dict):
            return
        payload = obj.get("d", obj) if "d" in obj else obj
        if not isinstance(payload, dict):
            return

        if not link.authenticated:
            if link.auth is None:
                return
            res = link.auth.handle(payload, now, self.pairing_open_for(link.transport))
            if res.state == OK:
                self._on_auth_ok(link, res)
            elif res.state == FAILED:
                if res.reply:
                    self._raw_send(link, res.reply)
                self._disconnect(link, res.why)
            elif res.state == HELD:
                dev = self._find_discovered(link.transport, link.address)
                self._offers[key] = _Offer(link.transport, link.address, res.fp or "",
                                           res.pubkey or "", name=(dev.name if dev else ""),
                                           ts=time.time(), cert_sat_id=self._cert_id(payload))
                self._publish_last = 0.0
            return

        # Authenticated traffic.
        a = payload.get("a")
        if a == "ping":
            self._raw_send(link, {"a": "pong"})
        sat_id = link.sat_id
        if now - link.last_seen_dispatch >= 1.0 or payload.get("ver") is not None:
            link.last_seen_dispatch = now
            fw = payload.get("ver")
            self._store.dispatch(UpdateSatelliteNodeAction(
                sat_id, seen=True, fw_version=str(fw) if fw is not None else None,
                source=ActionSource.INTERNAL))
        if a in ("ping", "pong", "auth"):
            return
        if self.on_message is not None:
            try:
                self.on_message(sat_id, payload)
            except Exception:
                logger.exception("Satellite %d message handler failed", sat_id)

    def _cert_id(self, payload: dict) -> Optional[int]:
        cert = payload.get("cert")
        if cert and self.keychain.car_pub and verify_cert(self.keychain.car_pub, cert) is None:
            try:
                return int(cert["sat_id"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def _on_auth_ok(self, link: _Link, res) -> None:
        sat_id = int(res.sat_id)
        entry = self.keychain.get(sat_id)
        if res.cert is not None and entry is None:
            try:
                with self.keychain.transaction() as kc:
                    sat_id, entry = kc.accept_cert(res.cert, address=link.address)
                logger.info("Satellite %d (%s) added to the keychain from its certificate",
                            sat_id, res.fp)
            except KeychainError as exc:
                self._raw_send(link, {"a": "auth_fail", "why": str(exc)})
                self._disconnect(link, str(exc))
                return
        if entry is None:
            entry = {"transport": link.transport, "fp": res.fp}
        self._authenticated(link, sat_id, entry)
        self._raw_send(link, res.reply)     # envelope carries "id" from auth_ok on

    def _authenticated(self, link: _Link, sat_id: int, entry: Dict[str, Any]) -> None:
        # One link per satellite: an older session (car dialled out while the
        # satellite dialled in) is closed.
        for other in list(self._links.values()):
            if other is not link and other.authenticated and other.sat_id == sat_id:
                self._disconnect(other, "superseded by a newer link")
        link.authenticated = True
        link.sat_id = sat_id
        link.last_ping = self._clock()
        link.last_seen_dispatch = self._clock()
        self._offers.pop((link.transport, link.address), None)
        self._backoff.pop((link.transport, link.address), None)
        self._seeded_ids.add(sat_id)
        dev = self._find_discovered(link.transport, link.address)
        self._store.dispatch(UpdateSatelliteNodeAction(
            sat_id, seen=True, linked=True, authenticated=True, trusted=True,
            transport=link.transport, address=link.address,
            fp=entry.get("fp") or (link.auth.fp if link.auth else None),
            name=entry.get("name") or None, caps=list(entry.get("caps") or []),
            rssi=dev.rssi if dev is not None else None,
            source=ActionSource.INTERNAL))
        self._publish_last = 0.0

    # ── periodic duties ─────────────────────────────────────────────────────

    def _auth_housekeeping(self, now: float) -> None:
        wall = time.time()
        for link in list(self._links.values()):
            if link.authenticated or link.auth is None:
                continue
            if link.auth.state == PENDING and link.auth.expired(now):
                self._raw_send(link, {"a": "auth_fail", "why": "timeout"})
                self._disconnect(link, "auth timeout")
            elif link.auth.state == HELD and not self.pairing_open_for(link.transport):
                self._raw_send(link, {"a": "auth_fail", "why": "pairing window closed"})
                self._disconnect(link, "pairing window closed")
        for key, offer in list(self._offers.items()):
            if wall - offer.ts > OFFER_TTL_S or (
                    not self.pairing_open_for(offer.transport) and key not in self._links):
                del self._offers[key]

    def _discovery_cadence(self, now: float) -> None:
        for name, t in self.transports.items():
            try:
                st = t.status()
            except Exception:
                continue
            available = bool(st.available)
            if name == TRANSPORT_RS485:
                # Background RS485 polls only ride on a rail the powerbox
                # confirms is up: never power the rail just to rediscover.
                available = self._rs485_rail_confirmed()
            was = self._avail.get(name, False)
            self._avail[name] = available
            if not available:
                continue
            due = self._next_discovery.get(name, 0.0)
            if not was or now >= due:
                self._next_discovery[name] = now + self.rediscover_s
                try:
                    t.discover(force=False)
                except Exception:
                    logger.exception("rediscovery on %s failed", name)

    def _keepalives(self, now: float) -> None:
        for link in list(self._links.values()):
            if not link.authenticated:
                continue
            period = getattr(self.transports.get(link.transport), "keepalive_s", 0.0) or 0.0
            if period > 0 and now - link.last_ping >= period:
                link.last_ping = now
                self._raw_send(link, {"a": "ping"})

    def _auto_connect(self, now: float) -> None:
        """Wi-Fi: dial beaconing satellites we trust (or any, while pairing)."""
        t = self.transports.get(TRANSPORT_WIFI)
        if t is None or not hasattr(t, "connect"):
            return
        pairing = self.pairing_open_for(TRANSPORT_WIFI)
        authed_fps = {l.auth.fp for l in self._links.values()
                      if l.authenticated and l.auth is not None}
        for dev in t.discovered():
            key = (TRANSPORT_WIFI, dev.address)
            if dev.linked or key in self._links or not (dev.extra or {}).get("beacon"):
                continue
            if self._backoff.get(key, 0.0) > now:
                continue
            if dev.fp and (dev.fp in authed_fps or self.keychain.is_revoked(dev.fp)):
                continue
            hit = self.keychain.by_fp(dev.fp) if dev.fp else None
            trusted = hit is not None and hit[1].get("transport") == TRANSPORT_WIFI
            if trusted or pairing:
                self._backoff[key] = now + CONNECT_BACKOFF_S
                t.connect(dev.address)

    def _update_adv(self) -> None:
        t = self.transports.get(TRANSPORT_BLE)
        if t is None:
            return
        acc = bool(self._store.state.powerbox.acc_on)
        pairing = self.pairing_open_for(TRANSPORT_BLE)
        flags = (1 if acc else 0) | (2 if pairing else 0)
        interval = ADV_INTERVAL_ACTIVE_MS if flags else ADV_INTERVAL_IDLE_MS
        if self._adv != (interval, flags):
            self._adv = (interval, flags)
            try:
                t.set_advertisement(interval, flags)
            except Exception:
                logger.exception("set_advertisement failed")

    def central_wanted(self) -> bool:
        """BLE central role (beacon satellites, docs/SATELLITES.md §4 "BLE
        roles"): on while the POCO power profile is ``full`` (ACC on, or held
        full by roadsense) or a BLE pairing window is open; never in ``low``.
        ``acc_on`` counts too, so the role still follows the key when the
        powerbox computer-side rules (which set ``power_mode``) are disabled."""
        pb = self._store.state.powerbox
        return (getattr(pb, "power_mode", "low") == "full" or bool(pb.acc_on)
                or self.pairing_open_for(TRANSPORT_BLE))

    def _beacon_addresses(self) -> Tuple[str, ...]:
        """Enrolled BLE satellites with ``ble_role: beacon`` and an address."""
        out = set()
        for entry in self.keychain.entries().values():
            if (entry.get("transport") == TRANSPORT_BLE
                    and entry.get("ble_role") == BLE_ROLE_BEACON and entry.get("address")):
                out.add(str(entry["address"]).upper())
        return tuple(sorted(out))

    def _update_central(self, now: Optional[float] = None, force: bool = False) -> None:
        """Tell the BLE transport to scan for / connect to beacon satellites;
        only on change (the transport re-sends after a bled reconnect)."""
        t = self.transports.get(TRANSPORT_BLE)
        if t is None:
            return
        now = self._clock() if now is None else now
        if not force and now - self._central_check < CENTRAL_CHECK_S:
            return
        self._central_check = now
        want = (self.central_wanted(), self._beacon_addresses())
        if want != self._central:
            self._central = want
            logger.info("BLE central role %s (%d beacon address(es))",
                        "on" if want[0] else "off", len(want[1]))
            try:
                t.set_central(want[0], list(want[1]))
            except Exception:
                logger.exception("set_central failed")

    # ── state mirror ────────────────────────────────────────────────────────

    def _find_discovered(self, transport: str, address: str) -> Optional[DiscoveredDevice]:
        t = self.transports.get(transport)
        if t is None:
            return None
        try:
            for dev in t.discovered():
                if dev.address == address:
                    return dev
        except Exception:
            return None
        return None

    def _discovered_list(self) -> List[Dict[str, Any]]:
        entries = self.keychain.entries()
        enrolled_fps = {e.get("fp") for e in entries.values() if e.get("fp")}
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for name, t in self.transports.items():
            try:
                devices = t.discovered()
            except Exception:
                continue
            for dev in devices:
                key = (name, dev.address)
                link = self._links.get(key)
                if link is not None and link.authenticated:
                    continue                      # it is a node now
                if name == TRANSPORT_RS485:
                    try:
                        if int(dev.address) in entries:
                            continue
                    except ValueError:
                        pass
                elif dev.fp and dev.fp in enrolled_fps:
                    continue
                out[key] = {
                    "transport": name, "address": dev.address, "name": dev.name or "",
                    "fp": dev.fp, "rssi": dev.rssi, "last_seen": float(dev.last_seen or 0.0),
                    "linked": bool(dev.linked), "pubkey": dev.pubkey, "sat_id": None,
                }
        for key, offer in self._offers.items():
            d = out.get(key) or {
                "transport": offer.transport, "address": offer.address, "name": offer.name,
                "fp": None, "rssi": None, "last_seen": offer.ts,
                "linked": key in self._links, "pubkey": None, "sat_id": None,
            }
            d.update(fp=offer.fp, pubkey=offer.pubkey, sat_id=offer.cert_sat_id)
            out[key] = d
        return sorted(out.values(), key=lambda d: (d["transport"], not d["linked"], d["address"]))

    def _transports_view(self) -> Dict[str, Dict[str, Any]]:
        view: Dict[str, Dict[str, Any]] = {}
        for name in TRANSPORTS:
            base = {"require_auth": bool(REQUIRE_AUTH.get(name, True))}
            t = self.transports.get(name)
            if t is not None:
                try:
                    st = t.status()
                    base.update(available=bool(st.available), state=st.state, detail=st.detail,
                                last_discovery=float(st.last_discovery or 0.0),
                                devices_seen=int(st.devices_seen))
                except Exception as exc:
                    base.update(available=False, state="error", detail=str(exc))
            elif name in self._missing:
                base.update(available=False, state="unavailable", detail=self._missing[name])
            else:
                base.update(available=False, state="disabled", detail="")
            view[name] = base
        return view

    def _publish(self, force: bool = False) -> None:
        self._publish_last = self._clock()
        transports = self._transports_view()
        discovered = self._discovered_list()
        pairing = dict(self._pairing)
        snapshot = (json.dumps(transports, sort_keys=True), json.dumps(discovered, sort_keys=True),
                    json.dumps(pairing, sort_keys=True))
        if not force and snapshot == self._published:
            return
        self._published = snapshot
        self._store.dispatch(SetSatelliteRegistryAction(
            transports=transports, discovered=discovered, pairing=pairing))
