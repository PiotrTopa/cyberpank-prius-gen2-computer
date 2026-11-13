"""
BackendService — the headless engine process.

Composes the existing (UI-free) VirtualTwin with the metrics subsystem and the
network API, then runs the single-threaded engine loop. This module must NOT
import pygame; the UI is a separate frontend that talks to this service over the
network API.

Threads:
    main        engine loop: vt.update() + bridge.drain_commands()  [owns Store]
    metrics     MetricsSink: samples Store snapshots -> SQLite        [own conn]
    api-server  uvicorn/FastAPI: REST + WebSocket                     [own loop]

Only the main thread ever calls store.dispatch (commands are marshaled to it by
the StoreBridge), preserving the Store's lock-free single-threaded model.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from ..io import (
    DEVICE_POWERBOX_BASE,
    DEVICE_VFD,
    DEVICE_CAN,
    ExecutionMode,
    MultiInputPort,
    OutgoingCommand,
    RecordingConfig,
    SerialConfig,
    SerialPort,
    TripRecorder,
    VirtualTwin,
    VirtualTwinConfig,
    create_virtual_twin,
)
from ..io.powerbox import (
    POCO2_BUTTON_CONNECTED,
    PowerboxCommander,
    PriusPowerController,
    register_powerbox_ingress,
)
from ..io.discovery import (
    ROLE_GATEWAY,
    ROLE_POWERBOX,
    discover_roles_combined,
    enumerate_candidates,
)
from ..io.usb_monitor import UsbSerialMonitor
from ..io.powerbox import reset_identity_log as reset_powerbox_identity_log
from ..state.actions import (
    SetPowerboxConnectionAction, SetConnectionStateAction, SetPandaStateAction,
)
from ..io.panda_can import PandaCanSource, PandaCanConfig
from ..metrics import MetricsDatabase, MetricsSink
from ..state.store import StateSlice
from ..state.rules.power_management import (
    PowerModeRule,
    UndervoltageProtectionRule,
)
from ..state.rules.satellite_power import (
    SatelliteAccHoldRule,
    SatellitePowerRule,
)
from .satellites import (
    SatelliteJobQueue,
    SatelliteScheduler,
    SatelliteSupervisor,
)
from .satellite_registry import SatelliteRegistry
from ..api import ApiServer, StoreBridge
from .fan_policy import ChassisFanPolicy, FanPolicyConfig
from .zmq_server import ZmqServer

logger = logging.getLogger(__name__)

# Placeholder path used for the powerbox SerialPort when the device is not
# present at build time but USB hotplug is enabled. It never opens; the hotplug
# monitor retargets the port to the real /dev/serial/by-id path on appearance.
_POWERBOX_PENDING_PORT = "/dev/prius-powerbox-pending"
_GATEWAY_PENDING_PORT = "/dev/prius-gateway-pending"


@dataclass
class BackendConfig:
    """Runtime configuration for the backend service."""

    gateway_port: str = "/dev/ttyACM0"
    powerbox_port: Optional[str] = None  # second RP2040; None until firmware exists
    serial_baudrate: int = 1_000_000

    # ── USB serial auto-discovery / hotplug ─────────────────────────────────
    # The gateway and powerbox are both MicroPython boards and enumerate as
    # /dev/ttyACM* in arbitrary order. The devices live on dedicated, fixed USB
    # hub ports (powerbox=port 2, gateway=port 5), so roles are resolved purely
    # from the USB topology — NO probing. This is deterministic, survives
    # renumbering/replug, and crucially never opens/writes to the device the way
    # a "whoami" probe would (probing the MicroPython CDC link can corrupt it and
    # freeze telemetry). usb_hotplug keeps a background monitor that re-resolves
    # and retargets the serial ports when devices are (un)plugged at runtime.
    auto_discover: bool = True
    usb_hotplug: bool = True
    discovery_timeout: float = 3.0
    hotplug_interval: float = 2.0
    # Physical hub-port → role map (devices live on dedicated ports). Resolved
    # from USB topology, so it works even when a device is silent/wedged. Set to
    # None to use discovery.DEFAULT_PORT_ROLES ({2: powerbox, 5: gateway}).
    usb_port_roles: Optional[dict] = None  # None -> discovery.DEFAULT_PORT_ROLES
    usb_hub: Optional[str] = None          # restrict to a hub location e.g. "1-1"
    # Pure port-based discovery: never probe the device with "whoami". Probing
    # opens the CDC link and writes to it, which has been observed to wedge the
    # powerbox firmware. Leave False so discovery is topology-only.
    usb_probe_fallback: bool = False

    api_host: str = "0.0.0.0"
    api_port: int = 8080
    auth_token: Optional[str] = None

    db_path: str = "data/metrics.db"

    # Engine loop rate (Hz). Serial ingress is drained each tick.
    tick_hz: float = 50.0

    # ── Replay ───────────────────────────────────────────────────────────────
    # Run the engine off a recorded NDJSON log instead of the live gateway.
    # When set, the gateway serial port is NOT opened and solicited CAN is
    # disabled automatically. Compatible with files produced by TripRecorder.
    replay_file: Optional[str] = None
    replay_speed: float = 1.0   # 1.0 = realtime, 0 = as fast as possible
    replay_loop: bool = False

    # ── AVC-LAN nav emulation (comm.avc.NavEmulator, port of nav_emu.py) ─────
    # Stand in for the removed nav ECU (178) so the display keeps its nav
    # screen, sends touch/buttons to us, and the climate dialogue works. Live
    # gateway only (never in replay).
    nav_emulation: bool = True
    nav_lang: str = "02"        # B7 LL: 02 English (2026-10-14), 06 Dutch (run1)

    # ── Trip recording ───────────────────────────────────────────────────────
    # Record live (or replayed) traffic to rotating per-trip NDJSON files.
    recording: RecordingConfig = field(default_factory=RecordingConfig)

    # Metrics cadence (seconds).
    sample_interval: float = 1.0
    rollup_interval: float = 60.0
    prune_interval: float = 3600.0

    # ── Powerbox / power management ─────────────────────────────────────────
    # The powerbox computer-side runs regardless of whether a powerbox serial
    # port is attached: it reacts to powerbox telemetry/ignition once those
    # messages arrive. Set powerbox_enabled=False to disable the rules entirely.
    powerbox_enabled: bool = True
    # Flag file the prius-power systemd unit watches to switch the POCO profile.
    power_mode_flag: str = "/etc/prius/power-mode"
    # 12 V under-voltage protection thresholds (volts) + timing.
    undervoltage_threshold: float = 11.0
    undervoltage_recover: float = 11.5
    undervoltage_confirm_s: float = 5.0
    shutdown_grace_s: int = 30
    # POCO->powerbox heartbeat cadence (s). The powerbox treats the POCO as dead
    # if this stops for ~15 s and may then wake it with the power button, so keep
    # this comfortably faster than that timeout.
    powerbox_heartbeat_s: float = 2.0
    # Powerbox link staleness watchdog (s). The powerbox streams telemetry +
    # STATUS at ~1 Hz; if NO frame arrives for this long the USB-CDC link has
    # silently wedged (board still alive, but stdout no longer drains) — the
    # serial reader sees only empty reads and never raises, so `connected` would
    # otherwise stay True forever with frozen data. When tripped the watchdog
    # flips powerbox.connected -> False so the dashboard/operator sees the link
    # is dead; it auto-clears when fresh frames resume. 0 disables. Recovery
    # (USB hub-port power-cycle) is deliberately NOT automatic — see docs.
    powerbox_stale_s: float = 15.0
    # Automatic link recovery: when the staleness watchdog trips, force the
    # powerbox serial port to close+reopen. That toggles DTR, which RESETS the
    # RP2040 -> it reboots, re-enumerates USB-CDC and resumes streaming, clearing
    # a silent wedge. Since firmware 2.0 the box has no power rails, so an MCU
    # reset is electrically harmless; it is still off by default because a
    # reset storm keeps the link wedged (see powerbox_recover_max_cooldown_s).
    powerbox_auto_recover: bool = False
    # Minimum spacing between recovery attempts (s). Prevents a permanently dead
    # link from being reset in a tight loop while it re-enumerates. The spacing
    # doubles with each consecutive failed attempt (see
    # powerbox_recover_max_cooldown_s) and resets as soon as frames resume.
    powerbox_recover_cooldown_s: float = 20.0
    # Ceiling for the doubling above. A flat 20 s retry forever is actively
    # harmful: each escalation resets the parent hub, which re-enables every
    # data port and cold-boots the powerbox, which triggers the next round of
    # relay/data drift enforcement — a storm that keeps the link wedged rather
    # than recovering it (2026-08-31: 137 enumerations / 39 failures in 14 min,
    # attempt #23 and climbing). Back off instead, and let a human look.
    powerbox_recover_max_cooldown_s: float = 600.0
    # Whether a tripped under-voltage powers the POCO off locally. Since
    # firmware 2.0 the powerbox CANNOT cut power (the main POCO is only ever
    # started through its button), so this is the only way the protection
    # actually sheds the load. On by default; disable on dev machines.
    local_poweroff_on_undervoltage: bool = True
    # Gateway link staleness watchdog (s). The gateway firmware (>= 2.28.0) emits
    # a ~1 Hz GW_HB liveness heartbeat. The gateway is power-cycled with ACC for
    # power saving, so when ignition drops it stops heartbeating and disappears
    # from USB-CDC; if NO heartbeat arrives for this long we flip
    # connection.connected -> False so the dashboard reflects the (expected)
    # power-save disconnect instead of showing a frozen "connected". It
    # auto-clears when heartbeats resume after the next ACC cycle. The watchdog
    # only arms once a heartbeat has been seen, so it never falsely disconnects a
    # pre-2.28.0 gateway that does not heartbeat. 0 disables.
    gateway_stale_s: float = 8.0
    # Gateway link auto-recovery: when heartbeats have been stale for
    # gateway_recover_stale_s AND the gateway VBUS relay (ch3, CAN hub) is actually ON
    # (i.e. it *should* be alive — not an ACC power-save cut), cold power-cycle
    # its relay to reboot the wedged MCU (USB resets don't — the firmware/CDC
    # TX stays dead; verified 2026-08-09: only a VBUS cycle restored GW_HB).
    # Two-phase and non-blocking: VBUS off, then back on after
    # gateway_recover_off_s. Rate-limited by gateway_recover_cooldown_s.
    gateway_auto_recover: bool = True
    gateway_recover_stale_s: float = 30.0   # stale age before cycling (> watchdog's 8 s)
    gateway_recover_off_s: float = 5.0      # VBUS off duration
    gateway_recover_cooldown_s: float = 120.0  # base spacing; doubles per failed attempt
    gateway_recover_max_cooldown_s: float = 1800.0  # backoff cap for a truly dead gateway
    # comma.ai panda = the broadcast (unsolicited) CAN listener, USB bbaa:ddcc on
    # the same CAN hub (relay ch3) as the gateway. Frames go from its reader
    # thread straight into the decoder/store (io.panda_can). Always
    # SAFETY_SILENT: bus monitor, no ACK, no TX. panda_bus=None accepts every
    # panda bus (the car test tells which one carries the Prius HS-CAN).
    panda_enabled: bool = True
    panda_bus: Optional[int] = None
    panda_dedupe_s: float = 0.25        # identical payload repeated within this -> dropped
    panda_status_s: float = 1.0         # state mirror cadence
    # 0 = SAFETY_SILENT (always, in the car). 19 = SAFETY_NOOUTPUT: the panda
    # ACKs frames so the gateway can transmit on a two-node bench segment;
    # BENCH ONLY (BACKEND_PANDA_SAFETY_MODE=19), never with the car attached.
    panda_safety_mode: int = 0

    # Cadence for polling the gateway's USB hub-port power via uhubctl so the UI
    # can show/toggle it like a powerbox relay. 0 disables the poll.
    gateway_usb_poll_s: float = 10.0

    # ── MFD video board (Pi Zero 2W on the PPPS hub port) ────────────────────
    # ACC-follower power manager for the VGA666 video board: port power on with
    # ACC, grace period after key-off, clean SSH shutdown, then VBUS cut. All
    # knobs live in backend.mfd_power.MfdPowerConfig and map to BACKEND_MFD_*
    # env vars — see backend.__main__.
    mfd_enabled: bool = False
    mfd_config: Optional[object] = None    # MfdPowerConfig; None -> defaults

    # ── roadsense perception stack (same computer, ACC-follower) ─────────────
    # Single-computer layout (2026-11-04): roadsense runs on this POCO as
    # systemd units under roadsense.target. The manager powers the camera hub
    # (relay ch4), starts the target with ACC, stops it cleanly after the
    # key-off grace and mirrors its /v1/health (backend.roadsense_power). Knobs
    # map to BACKEND_ROADSENSE_* env vars — see backend.__main__.
    roadsense_enabled: bool = False
    roadsense_config: Optional[object] = None  # RoadsenseConfig; None -> defaults

    # ── USB hub port power (backend.port_power) ──────────────────────────────
    # Topology of hub port power control: the powerbox's socket has native PPPS
    # (uhubctl); gateway/MFD/SDR VBUS goes through powerbox relays with
    # data-off sequencing and lossy-command verification against the "rly"
    # STATUS telemetry. None -> PortPowerConfig() defaults (current car).
    port_power_config: Optional[object] = None
    # Cadence for the desired-vs-actual relay enforcement sweep. Also converges
    # after a hub reset (which cold-boots the powerbox and releases all relays).
    port_power_enforce_s: float = 15.0

    # ACC ON->OFF edges are held this long before they reach the store (every
    # ACC follower — power profile, roadsense, MFD, satellite rail, gateway
    # relay — keys off it). A key turned past ACC and back, or a bounce on the
    # sense line, must not start the key-off cascade. ACC-on is immediate.
    acc_off_debounce_s: float = 3.0

    # ── RS485 satellites (relay ch5 rail power management + twin) ────────────
    # The satellite subsystem: rail wake-lock power rule (rail on while ACC is
    # on or jobs are pending), the serialized job queue, periodic scheduler and
    # the presence/config-sync supervisor. Requires powerbox_enabled for the
    # physical rail control; without a powerbox link it still tracks presence
    # and runs jobs (rail assumed up).
    satellites_enabled: bool = True
    # Linger before the rail is actually dropped after the last wake-lock releases.
    # Absorbs bursts (queued jobs arriving back-to-back) and key-off bounce.
    satellite_linger_s: float = 10.0
    # Re-send cadence while the powerbox rs485_power mirror disagrees with the rule.
    satellite_retry_s: float = 5.0
    # Mark a satellite twin node offline after this long without traffic.
    satellite_offline_after_s: float = 15.0
    # Periodic key-off sensor wake: every interval, power the rail and let the
    # listed satellites report (their traffic refreshes the twin). 0 disables.
    # Example: interval=300 + devices=(107,) reads the light sensor every 5 min.
    satellite_poll_interval_s: float = 0.0
    satellite_poll_devices: tuple = ()
    # Payload sent to each polled device to solicit a report.
    satellite_poll_payload: str = '{"a":"status"}'

    # ── Multi-transport satellites (docs/SATELLITES.md) ──────────────────────
    # Transports the SatelliteRegistry runs. Env BACKEND_SATELLITE_TRANSPORTS
    # (comma list). "ble" needs io/ble_transport.py + prius-bled; without it
    # the registry logs once and runs without BLE.
    satellite_transports: tuple = ("rs485", "ble", "wifi")
    # Background rediscovery cadence per transport (also right after an
    # availability edge: RS485 rail confirmed up, bled connected, AP up) and
    # how long a seen device stays in ``satellites.discovered``.
    satellite_rediscover_s: float = 300.0
    satellite_discovery_ttl_s: float = 600.0
    # Keychain dir (car key + keychain.json). None -> env
    # BACKEND_SATELLITE_KEYCHAIN_DIR or /etc/prius/satellites, falling back
    # to ./satellites when not writable.
    satellite_keychain_dir: Optional[str] = None
    # Wi-Fi transport: UDP beacons + TCP NDJSON sessions. iface set ("wlan0"
    # on the phone) = only available while it carries ap_addr (AP mode).
    satellite_wifi_bind: str = "0.0.0.0"
    satellite_wifi_iface: str = ""
    satellite_wifi_ap_addr: str = "10.42.0.1"
    satellite_wifi_udp_port: int = 8787
    satellite_wifi_tcp_port: int = 8788
    # prius-bled control socket (BLE transport).
    satellite_ble_socket: str = "/run/prius/bled.sock"

    # ── Chassis fan control ──────────────────────────────────────────────────
    # 4-wire fan driven by the powerbox (fw >= 2.1: 25 kHz PWM on GP29, tach on
    # GP28); the backend only sends a speed in percent. The policy itself (two
    # power-mode profiles on a filtered GPU-led control temperature, safety
    # override, box purge) lives in backend/fan_policy.py; every number in it
    # follows from the bench campaign in docs/THERMAL_MODEL.md.
    chassis_fan_enabled: bool = True
    chassis_fan_tick_s: float = 2.0       # control loop cadence (seconds)
    fan_policy: FanPolicyConfig = field(default_factory=FanPolicyConfig)

    # ── Dashcam vision (io.dashcam_ingest, docs/DASHCAM_70MAI.md) ────────────
    # Auto-light recommendation from the 70mai live stream + sun elevation,
    # published as state.vision. None/empty = disabled (no thread, no ffmpeg).
    # Env: BACKEND_DASHCAM_URL, BACKEND_DASHCAM_INTERVAL_S, BACKEND_DASHCAM_SOURCE,
    # BACKEND_HOME_LATLON.
    # ── BEAN body bus (powerbox fw >= 2.3.0 receiver, device 203) ──────────
    # Capture logs for reverse engineering (REST bean_capture / bean_mark).
    # Relative to the backend's working directory (the repo on the phone,
    # next to logs/trips). Env BACKEND_BEAN_CAPTURE_DIR.
    bean_capture_dir: str = "logs/bean"
    bean_capture_rotate_mb: float = 20.0

    dashcam_url: Optional[str] = None
    dashcam_interval_s: float = 1.0
    dashcam_source: str = "stream"        # stream (one ffmpeg, 1 fps) | grab (session per frame)
    home_latlon: tuple = (54.35, 18.65)   # sun position fallback until a GPS fix exists

    verbose: bool = False


class BackendService:
    """Own the engine, metrics sink and API server for the headless backend."""

    # Seconds between a POWERBOX_READY banner and the relay re-assert: the
    # firmware sends the banner, then 3 s safe-boot, then I2C/PCF setup.
    PP_REASSERT_DELAY_S = 6.0

    def __init__(self, config: BackendConfig) -> None:
        self.config = config
        self._stop = threading.Event()

        self.twin: Optional[VirtualTwin] = None
        self.db: Optional[MetricsDatabase] = None
        self.sink: Optional[MetricsSink] = None
        self.bridge: Optional[StoreBridge] = None
        self.api: Optional[ApiServer] = None
        self.zmq: Optional[ZmqServer] = None
        self.power_controller: Optional[PriusPowerController] = None
        self.powerbox_commander: Optional[PowerboxCommander] = None
        self.satellite_queue: Optional[SatelliteJobQueue] = None
        self.satellite_scheduler: Optional[SatelliteScheduler] = None
        self.satellite_supervisor: Optional[SatelliteSupervisor] = None
        self.satellite_registry: Optional[SatelliteRegistry] = None
        self._satellite_sup_last: float = 0.0
        self.recorder: Optional[TripRecorder] = None
        self.diag = None            # diag.wiring.Diagnostics (all-ECU diagnostics engine)
        self._unsubscribe = None
        self._unsubscribe_powerbox = None

        # Serial port handles kept for USB hotplug retargeting.
        self._gateway_serial: Optional[SerialPort] = None
        self._powerbox_serial: Optional[SerialPort] = None
        self._usb_monitor: Optional[UsbSerialMonitor] = None

        # POCO->powerbox heartbeat (rolling counter + send cadence).
        self._pb_hb_counter: int = 0
        self._pb_hb_last: float = 0.0
        self._pb_pmlog_fetched: bool = False

        # Powerbox link staleness watchdog: latched True once a stall is
        # detected, cleared when fresh frames resume (edge-triggered logging).
        self._pb_stale: bool = False
        # Auto-recovery bookkeeping: timestamp of the last forced reconnect and a
        # running attempt counter (reset once fresh frames resume).
        self._pb_recover_last: float = 0.0
        self._pb_recover_attempts: int = 0
        # Monotonic. Every watchdog/cadence below ages against
        # time.monotonic(): the wall clock on the phone (no RTC) is stepped by
        # hours when NTP first syncs, which on 2026-11-05 made a healthy
        # powerbox/gateway look 91117 s stale and cold-cycled the CAN hub.
        self._start_time: float = time.monotonic()
        # Wall-clock stamps from the store (display) -> monotonic "last seen a
        # change" marks for the link watchdogs (see _mono_age).
        self._seen_marks: dict = {}

        # Gateway link staleness watchdog: latched True once heartbeats stop,
        # cleared when they resume (edge-triggered logging).
        self._gw_stale: bool = False

        self._poco_poll_last: float = 0.0

        # Chassis fan controller state.
        self._fan_last_tick: float = 0.0
        self._vfd_clock_last: float = 0.0
        self.fan_policy = ChassisFanPolicy(config.fan_policy)
        self._fan_last_pct: float = -1.0    # last sent speed (avoid re-sending same value)
        self._fan_last_log_pct: float = -1.0  # edge-triggered logging

        # Gateway USB hub-port power telemetry mirror (relay ch3 = CAN hub, via port_power).
        self._gateway_usb_target: Optional[str] = None  # stable by-id path of the gateway board
        self._gwusb_last_poll: float = 0.0
        self._gwusb_loc: Optional[str] = None           # cached "HUB PORT" (e.g. "1-1.4 2")

        # Gateway link auto-recovery (cold VBUS cycle via relay ch3, the CAN hub shared with the panda).
        self._gw_recover_off_at: Optional[float] = None  # VBUS-off timestamp; None = not mid-cycle
        self._gw_recover_last: float = 0.0               # last cycle start (cooldown)
        self._gw_recover_epoch: float = time.monotonic() # staleness reference when no HB seen yet (monotonic)
        self._gw_recover_attempts: int = 0               # consecutive cycles without a heartbeat
        self._gw_vbus_on_at: Optional[float] = None      # when relay ch3 VBUS was last seen coming on

        # MFD video board power manager (backend.mfd_power).
        self.mfd_power = None
        # roadsense stack manager (backend.roadsense_power).
        self.roadsense_power = None
        # On-demand dashboard camera preview (backend.camera_preview).
        self.camera_preview = None
        # Key-off "low" CPU profile deferred until roadsense has stopped
        # (modeld is pinned to cpu4-7, which "low" takes offline).
        self._low_mode_deferred = False

        # USB hub port power controllers (backend.port_power.HubPortPower).
        self.port_power = None
        # ACC-off debounce (io.powerbox.AccOffDebouncer), wired at setup.
        self._acc_debounce = None
        self._pp_enforce_last: float = 0.0
        # Pending re-assert of every wanted relay state after a powerbox boot
        # banner (the PCF wakes at 0xFF: every NO load dropped). Delayed a few
        # seconds because the banner precedes the firmware's I2C/PCF setup.
        self._pp_reassert_at: Optional[float] = None

        # AVC-LAN nav emulator (live gateway only) + status mirror cadence.
        self.nav_emulator = None
        self._nav_scheduler = None
        self._avc_tick_last: float = 0.0
        self._avc_status_last: Optional[dict] = None

        # Dashcam auto-light ingest (optional, io.dashcam_ingest).
        self.dashcam = None

        # BEAN monitor (frame ring + capture log) and its status mirror cadence.
        self.bean_monitor = None
        self._bean_tick_last: float = 0.0
        self._bean_status_last: Optional[dict] = None

        # comma.ai panda broadcast CAN source (io.panda_can) + status mirror.
        self.panda: Optional[PandaCanSource] = None
        self._panda_status_last: float = 0.0
        self._panda_last_mirror: Optional[tuple] = None

    # ── composition ────────────────────────────────────────────────────────

    def build(self) -> None:
        """Wire all components together without starting any threads."""
        cfg = self.config

        if cfg.replay_file:
            # Replay mode: drive the engine off a recorded NDJSON log. The
            # gateway serial port is not opened; solicited CAN is auto-disabled
            # by the factory for non-PRODUCTION modes.
            twin = create_virtual_twin(
                VirtualTwinConfig(
                    mode=ExecutionMode.DEVELOPMENT,
                    replay_file=cfg.replay_file,
                    playback_speed=cfg.replay_speed,
                    playback_loop=cfg.replay_loop,
                    log_commands=True,
                    verbose=cfg.verbose,
                )
            )
            logger.info(
                "Replay mode: %s (speed=%.2f, loop=%s)",
                cfg.replay_file, cfg.replay_speed, cfg.replay_loop,
            )
        else:
            gateway_port = cfg.gateway_port
            powerbox_port = cfg.powerbox_port
            if cfg.auto_discover:
                roles = discover_roles_combined(
                    port_roles=cfg.usb_port_roles,
                    hub=cfg.usb_hub,
                    probe_timeout=cfg.discovery_timeout,
                    use_whoami_fallback=cfg.usb_probe_fallback,
                )
                # Bind each role to its discovered (stable by-id) path. If a role
                # is NOT found, fall back to a pending placeholder rather than the
                # hardcoded default — otherwise the gateway would open the same
                # /dev/ttyACM0 the powerbox is on and the two readers would fight
                # over one device. The hotplug monitor retargets on appearance.
                gateway_port = roles.get(ROLE_GATEWAY)
                powerbox_port = roles.get(ROLE_POWERBOX)
                if gateway_port is None:
                    gateway_port = _GATEWAY_PENDING_PORT if cfg.usb_hotplug else cfg.gateway_port
                logger.info(
                    "USB auto-discovery: gateway=%s powerbox=%s",
                    gateway_port, powerbox_port,
                )

            # Remember a stable target for gateway USB hub-port power control.
            # Prefer the discovered by-id path; fall back to the configured port.
            # Ignore the pending placeholder (device not present at build time).
            if gateway_port and gateway_port != _GATEWAY_PENDING_PORT:
                self._gateway_usb_target = gateway_port
            else:
                self._gateway_usb_target = cfg.gateway_port

            twin = create_virtual_twin(
                VirtualTwinConfig(
                    mode=ExecutionMode.PRODUCTION,
                    serial_port=gateway_port,
                    serial_baudrate=cfg.serial_baudrate,
                    verbose=cfg.verbose,
                )
            )
            # Keep the gateway SerialPort handle (before it is wrapped in a
            # MultiInputPort) so the hotplug monitor can retarget it.
            if isinstance(twin.input_port, SerialPort):
                self._gateway_serial = twin.input_port

        # AVC-LAN nav emulation on the gateway serial port (reader-thread hook
        # so replies leave within milliseconds of the display's request).
        if self._gateway_serial is not None and cfg.nav_emulation:
            self._wire_nav_emulator(self._gateway_serial)

        # Broadcast CAN from the panda (live mode only; a replay already holds
        # the frames). Reader thread -> ingress.ingest_broadcast_can -> store.
        if cfg.panda_enabled and not cfg.replay_file:
            twin.ingress.set_broadcast_dedupe(cfg.panda_dedupe_s)
            self.panda = PandaCanSource(
                on_frame=twin.ingress.ingest_broadcast_can,
                config=PandaCanConfig(bus=cfg.panda_bus, safety_mode=cfg.panda_safety_mode),
            )
            twin.store.add_middleware(self._panda_bench_middleware)
            logger.info("Panda broadcast CAN source enabled (bus=%s, dedupe %.2fs)",
                        "any" if cfg.panda_bus is None else cfg.panda_bus, cfg.panda_dedupe_s)

        # Optional powerbox seam: merge a second serial device into the ingress,
        # offset into the reserved DEVICE_POWERBOX_BASE id range. Not used in
        # replay mode (the powerbox stream, if any, is already in the log). When
        # USB hotplug is enabled the seam is wired even if the powerbox is not
        # present yet, using a pending placeholder path that the monitor
        # retargets once the device appears.
        if cfg.powerbox_enabled and not cfg.replay_file and (powerbox_port or cfg.usb_hotplug):
            pb_path = powerbox_port or _POWERBOX_PENDING_PORT
            powerbox = SerialPort(
                SerialConfig(
                    port=pb_path, 
                    baudrate=cfg.serial_baudrate,
                    keepalive_ping='{"id":0,"d":{"a":"ping"}}\n'
                )
            )
            self._powerbox_serial = powerbox
            merged = MultiInputPort(
                [(twin.input_port, 0), (powerbox, DEVICE_POWERBOX_BASE)]
            )
            twin.ingress.set_input_port(merged)
            twin.input_port = merged
            logger.info("Powerbox port enabled on %s", pb_path)
            # Prime the USB hub cache so recovery still works after the
            # device disappears from the bus.
            import os
            try:
                real = os.path.realpath(pb_path)
                powerbox._find_parent_hub_id(os.path.basename(real))
            except Exception:
                pass

        # Powerbox computer-side: ingress parsers + power-management rules.
        if cfg.powerbox_enabled:
            self._wire_powerbox(twin)

        # RS485 satellites: rail (relay ch5) wake-lock power rules + job queue + twin.
        if cfg.satellites_enabled:
            self._wire_satellites(twin)

        # Trip recording: tap ingress/egress and write rotating per-trip logs.
        if cfg.recording.enabled:
            self._wire_recording(twin)

        # All-ECU diagnostics engine (diag_* commands, state.diagnostics.ecus,
        # scan reports next to the metrics database, auto-clear monitor).
        try:
            import os as _os
            from ..diag.wiring import wire_diagnostics
            self.diag = wire_diagnostics(
                twin,
                report_dir=_os.environ.get("PRIUS_DIAG_DIR")
                or _os.path.join(_os.path.dirname(cfg.db_path) or ".", "diag"),
                live=not cfg.replay_file,
            )
        except Exception:
            logger.exception("Diagnostics engine could not be wired")

        # Dashcam vision: optional auto-light recommendation (state.vision).
        if cfg.dashcam_url:
            self._wire_dashcam(twin)

        # (MFD power manager is wired later in build(), after the port power
        # controllers exist — its VBUS goes through powerbox relay ch2.)

        db = MetricsDatabase(cfg.db_path)
        sink = MetricsSink(
            twin.store,
            db,
            sample_interval=cfg.sample_interval,
            rollup_interval=cfg.rollup_interval,
            prune_interval=cfg.prune_interval,
        )

        self._wire_bean(twin)

        bridge = StoreBridge(twin.store)
        # Push every state change to API clients (callback runs on the engine
        # thread; the bridge hands snapshots to the asyncio loop safely).
        self._unsubscribe = twin.store.subscribe(StateSlice.ALL, bridge.on_state)
        # Provide the initial state immediately.
        bridge.on_state(twin.store.state)


        # On-demand camera preview (lease + frames; powers the camera hub
        # through the roadsense manager's preview hold when the stack is down).
        try:
            from .camera_preview import wire_camera_preview
            self.camera_preview = wire_camera_preview(self, twin)
        except Exception:
            logger.exception("Camera preview could not be wired; disabled")
            self.camera_preview = None

        # roadsense front: the backend re-publishes the stack's API (bound to
        # loopback on this phone) under /api/v1/roadsense.
        roadsense_url = None
        if cfg.roadsense_enabled:
            from .roadsense_power import RoadsenseConfig as _RsCfg
            _rs = cfg.roadsense_config or _RsCfg()
            roadsense_url = "http://%s:%d" % (_rs.api_host, _rs.api_port)
        api = ApiServer(
            bridge,
            db,
            host=cfg.api_host,
            port=cfg.api_port,
            auth_token=cfg.auth_token,
            log_level="debug" if cfg.verbose else "info",
            roadsense_url=roadsense_url,
            bean_frames=self.bean_monitor.frames_since if self.bean_monitor else None,
            camera_preview=self.camera_preview,
        )
        
        zmq_srv = ZmqServer(
            bridge=bridge,
            host=cfg.api_host,
            pub_port=8081,
            rep_port=8082,
        )

        # State: subscribe the ZMQ publisher to store updates (the server
        # rate-limits internally; serialize only when a frame is due — AVC
        # touch/energy updates dispatch many times per second). Events: every
        # bridge.push_event envelope is mirrored onto the ZMQ PUB channel.
        def _zmq_on_state(state):
            if not zmq_srv.state_due():
                return
            from ..api.serialization import serialize_state
            zmq_srv.enqueue_state(
                {"type": "state", "ts": time.time(), "state": serialize_state(state)}
            )

        self._unsubscribe_zmq = twin.store.subscribe(StateSlice.ALL, _zmq_on_state)
        bridge.add_event_sink(zmq_srv.enqueue_event)

        # AVC-LAN touch + nav/info button events ("touch", "button") go out on
        # the same event channel (contract in io/avc_ingest.py).
        twin.ingress.avc.set_event_sink(bridge.push_event)

        # Human-interface events (frontend touch/keys, satellite controls):
        # re-broadcast every InputEventAction as an "input" event to all
        # connected clients (websocket + ZMQ). Rules/middleware may also react
        # to the action itself (e.g. haptic feedback via the satellite queue).
        def _input_event_middleware(action, _store) -> None:
            if type(action).__name__ == "InputEventAction":
                logger.info("Input event: device=%s event=%s value=%r",
                            action.device, action.event, action.value)
                bridge.push_event("input", {
                    "device": action.device,
                    "event": action.event,
                    "value": action.value,
                })

        twin.store.add_middleware(_input_event_middleware)


        self.twin = twin
        self.db = db
        self.sink = sink
        self.bridge = bridge
        self.api = api
        self.zmq = zmq_srv

    def _wire_bean(self, twin: VirtualTwin) -> None:
        """BEAN monitor: frame ring for GET /api/v1/bean/frames + capture log
        (REST bean_capture / bean_mark), fed from SetBeanRxAction."""
        from .bean_monitor import BeanMonitor
        cfg = self.config
        mon = BeanMonitor(cfg.bean_capture_dir,
                          rotate_bytes=int(cfg.bean_capture_rotate_mb * 1024 * 1024))
        self.bean_monitor = mon

        def _bean_middleware(action, store) -> None:
            name = type(action).__name__
            if name == "SetBeanRxAction":
                if action.frames or action.stats:
                    mon.ingest(action.frames, action.stats)
            elif name == "BeanCaptureAction":
                if action.on:
                    mon.start(action.name)
                else:
                    mon.stop()
                self._bean_tick_last = 0.0          # mirror the status now
            elif name == "BeanMarkAction":
                mon.mark(action.text)
            elif name == "PowerboxBeanTxAction":
                mon.tx_event("tx_req", dict(action.params, op=action.op))
            elif name == "SetPowerboxBeanTxAction":
                mon.tx_event("tx", action.result)

        twin.store.add_middleware(_bean_middleware)

    def _bean_tick(self) -> None:
        """Mirror the capture-log status into bean.capture (1 Hz, on change)."""
        if self.bean_monitor is None or self.twin is None:
            return
        now = time.monotonic()
        if now - self._bean_tick_last < 1.0:
            return
        self._bean_tick_last = now
        st = self.bean_monitor.status()
        if st != self._bean_status_last:
            self._bean_status_last = st
            from ..state.actions import SetBeanCaptureAction
            self.twin.store.dispatch(SetBeanCaptureAction(st))

    def _wire_dashcam(self, twin: VirtualTwin) -> None:
        """Dashcam light ingest: worker thread grabs + decides, engine tick
        dispatches (io.dashcam_ingest). Never fatal: a missing ffmpeg or
        camera only leaves ``vision.camera_ok`` False."""
        try:
            from ..io.dashcam_ingest import DashcamLightIngest, make_source, position_from_state
            cfg = self.config
            self.dashcam = DashcamLightIngest(
                make_source(cfg.dashcam_url, cfg.dashcam_source, cfg.dashcam_interval_s),
                interval_s=max(1.0, cfg.dashcam_interval_s),
                position=lambda: position_from_state(twin.store.state),
                default_latlon=tuple(cfg.home_latlon),
            )
            logger.info("Dashcam light ingest enabled: %s", cfg.dashcam_url)
        except Exception:
            logger.exception("Dashcam light ingest could not be set up; disabled")
            self.dashcam = None

    def _dashcam_tick(self) -> None:
        """Dispatch the dashcam ingest's newest result (engine thread)."""
        if self.dashcam is None or self.twin is None:
            return
        for action in self.dashcam.drain_actions():
            self.twin.store.dispatch(action)

    def _wire_nav_emulator(self, serial_port: SerialPort) -> None:
        """Bind a NavEmulator to the gateway serial port."""
        from ..comm.avc import NavEmulator, ThreadScheduler

        def _send(device_id: int, payload: dict) -> bool:
            return serial_port.send(OutgoingCommand(
                device_id=device_id, command_type="avc_nav", payload=payload,
                include_cmd=False))

        self._nav_scheduler = ThreadScheduler(name="avc-nav")
        nav = NavEmulator(_send, self._nav_scheduler, lang=self.config.nav_lang)
        serial_port.add_rx_hook(nav.on_gateway_line)
        serial_port.add_connect_hook(nav.configure)
        self.nav_emulator = nav
        logger.info("AVC nav emulation enabled (178, lang %s)", self.config.nav_lang)

    def _avc_tick(self) -> None:
        """Mirror nav-emulator / climate-remote status into AvcState (1 Hz)."""
        now = time.monotonic()
        if now - self._avc_tick_last < 1.0 or self.twin is None:
            return
        self._avc_tick_last = now
        fields: dict = {}
        nav = self.nav_emulator
        if nav is not None:
            st = nav.status()
            fields.update(
                nav_emulation="configured" if st["configured"] else "off",
                nav_registered=bool(st["registered"]),
            )
        remote = getattr(self.twin, "climate_remote", None)
        if remote is not None:
            fields["climate_queue"] = remote.queued
        if fields and fields != self._avc_status_last:
            self._avc_status_last = fields
            from ..state.actions import SetAvcStateAction
            self.twin.store.dispatch(SetAvcStateAction(**fields))

    def _wire_powerbox(self, twin: VirtualTwin) -> None:
        """Register powerbox ingress parsers and power-management rules.

        Side effects are injected and dev-safe: the power-mode flag is only
        written if the path is writable, and the powerbox power-off command is
        logged (not sent) until a powerbox output port exists.
        """
        cfg = self.config

        from ..io.powerbox import AccOffDebouncer
        self._acc_debounce = AccOffDebouncer(cfg.acc_off_debounce_s)
        register_powerbox_ingress(twin.ingress, action_filter=self._acc_debounce.filter)

        try:
            from pathlib import Path
            flag_path = Path(cfg.power_mode_flag)
            if flag_path.exists():
                mode = flag_path.read_text().strip()
                if mode in ("low", "full"):
                    from ..state.actions import SetPowerboxPowerModeAction
                    twin.store.dispatch(SetPowerboxPowerModeAction(mode))
        except Exception:
            pass

        controller = PriusPowerController(flag_path=cfg.power_mode_flag)
        # Wire the powerbox's own serial link as the command output port so the
        # commander can actually reach the firmware (sleep/wake, heartbeat,
        # relays, fan, power buttons). None in replay / no port -> log-only.
        commander = PowerboxCommander(output_port=self._powerbox_serial)
        self.power_controller = controller
        self.powerbox_commander = commander

        # USB hub port power controllers. Relay commands go through the
        # powerbox commander; verification reads the mirrored "rly" telemetry.
        # Built even in replay mode (commander is log-only there) so the code
        # paths stay uniform.
        from .port_power import HubPortPower, PortPowerConfig
        pp_cfg = cfg.port_power_config or PortPowerConfig()
        self.port_power = HubPortPower.build(
            pp_cfg,
            send_relay=lambda ch, on: commander.set_relay(ch, on),
            get_relays=lambda: (
                self.twin.store.state.powerbox.relays if self.twin else None
            ),
            # The ``rly`` mirror is only meaningful while frames are arriving.
            # A wedged USB-CDC link freezes it, and enforcing drift against a
            # frozen mirror re-issues relay commands forever (see
            # port_power.RelayPortPower.enforce).
            telemetry_fresh=self._powerbox_telemetry_fresh,
        )

        # MFD video board (Pi Zero 2W): ACC-follower USB port power manager.
        # Its VBUS goes through powerbox relay ch2 (RelayPortPower handles the
        # data-off sequencing + lossy-command verification).
        if cfg.mfd_enabled:
            from .mfd_power import MfdPowerConfig, MfdPowerManager
            from ..state.actions import SetMfdStatusAction

            mfd_cfg = cfg.mfd_config or MfdPowerConfig()

            def _publish_mfd(status: dict) -> None:
                twin.store.dispatch(SetMfdStatusAction(
                    state=status["state"],
                    usb_power=status["usb_power"],
                    reachable=status["reachable"],
                    state_since=status.get("state_since"),
                    last_ok_ping=status.get("last_ok_ping"),
                    power_cycles=status.get("power_cycles"),
                    last_boot_s=status.get("last_boot_s"),
                ))

            self.mfd_power = MfdPowerManager(
                mfd_cfg, publish=_publish_mfd,
                controller=self.port_power.mfd,
            )
            logger.info(
                "MFD power manager wired (relay ch%d, iface=%s board=%s "
                "grace=%.0fs)",
                pp_cfg.mfd_relay_ch, mfd_cfg.iface, mfd_cfg.board_ip,
                mfd_cfg.grace_s,
            )

        # roadsense stack (same computer): ACC-follower that powers the camera
        # hub, starts roadsense.target and stops it cleanly after the key-off
        # grace; mirrors its health + the phone's thermal zones into the state.
        if cfg.roadsense_enabled:
            from .roadsense_power import RoadsenseConfig, RoadsenseManager
            from ..state.actions import SetRoadsenseStateAction

            rs_cfg = cfg.roadsense_config or RoadsenseConfig()

            def _publish_roadsense(status: dict) -> None:
                twin.store.dispatch(SetRoadsenseStateAction(
                    enabled=True,
                    state=status["state"],
                    mode=status["mode"],
                    desired=bool(status.get("desired")),
                    camera_power=status.get("camera_power"),
                    api_ok=status.get("api_ok"),
                    api_url=status.get("api_url") or "",
                    api_port=int(status.get("api_port") or 0),
                    health=status.get("health"),
                    units=status.get("units"),
                    temps=status.get("temps"),
                    hottest_zone=status.get("hottest_zone"),
                    hottest_c=status.get("hottest_c"),
                    state_since=status.get("state_since"),
                    last_api_ok=status.get("last_api_ok"),
                    restarts=int(status.get("restarts") or 0),
                    last_error=status.get("last_error"),
                ))

            self.roadsense_power = RoadsenseManager(
                rs_cfg, publish=_publish_roadsense,
                camera=self.port_power.camera if rs_cfg.camera_relay else None,
            )
            twin.store.dispatch(SetRoadsenseStateAction(
                enabled=True, state=self.roadsense_power.state, mode=rs_cfg.mode,
                api_url="http://%s:%d" % (rs_cfg.api_host, rs_cfg.api_port),
                api_port=rs_cfg.api_port))
            logger.info(
                "roadsense manager wired (target=%s api=%s:%d camera relay ch%s "
                "grace=%.0fs mode=%s)",
                rs_cfg.target, rs_cfg.api_host, rs_cfg.api_port,
                pp_cfg.camera_relay_ch if rs_cfg.camera_relay else "-",
                rs_cfg.grace_s, rs_cfg.mode,
            )

        def _powerbox_middleware(action, store) -> None:
            from ..state.actions import ActionSource
            if type(action).__name__ == "SetPowerboxConnectionAction" and \
                    getattr(action, "boot", False):
                # The firmware (re)booted: every NO relay dropped (relay VCC
                # is the RP2040's 5 V) and the PCF woke at 0xFF. Re-assert the
                # wanted states once its I2C/PCF setup is done.
                self._pp_reassert_at = time.monotonic() + self.PP_REASSERT_DELAY_S
                logger.warning(
                    "Powerbox booted (reset_cause=%s): MFD/CAN/camera relays dropped — "
                    "re-asserting wanted relay states in %.0fs",
                    getattr(action, "reset_cause", None), self.PP_REASSERT_DELAY_S)
                return
            if getattr(action, "source", None) == ActionSource.UI and self.powerbox_commander:
                if type(action).__name__ == "PowerboxButtonAction":
                    # Manual power-button press. "poco 2" is a legacy slot:
                    # there is no second POCO (single computer) and fw 2.1
                    # refuses it (ERROR POCO2_NC).
                    if int(action.poco) == 2 and not POCO2_BUTTON_CONNECTED:
                        logger.warning("POCO 2 button press ignored: no second POCO "
                                       "(single-computer layout), not wired on the powerbox")
                    else:
                        self.powerbox_commander.press_button(int(action.ms), poco=int(action.poco))
                elif type(action).__name__ == "RoadsenseModeAction":
                    if self.roadsense_power is None:
                        logger.warning("roadsense mode %r ignored: manager disabled "
                                       "(BACKEND_ROADSENSE_ENABLED=0)", action.mode)
                    else:
                        try:
                            self.roadsense_power.set_mode(str(action.mode))
                        except ValueError as exc:
                            logger.error("roadsense mode rejected: %s", exc)
                elif type(action).__name__ == "RoadsenseRestartAction":
                    if self.roadsense_power is None:
                        logger.warning("roadsense restart ignored: manager disabled")
                    else:
                        self.roadsense_power.request_restart()
                elif type(action).__name__ == "PowerboxHardRestartAction":
                    logger.warning(
                        "Powerbox HARD RESTART requested: the box cuts its own supply; "
                        "MFD Pi, CAN hub (gateway + panda) and camera hub will drop "
                        "and come back OFF until re-asserted")
                    self.powerbox_commander.hard_restart()
                elif type(action).__name__ == "SetRelayAction":
                    # Manual relay control. Route through the port power
                    # controllers (data-off sequencing + enforcement) where
                    # one exists for the channel; raw command otherwise.
                    from ..state.app_state import RELAY_CH_RESTART, RELAY_CH_RS485
                    pp = self.port_power
                    ch = int(action.channel)
                    if ch == RELAY_CH_RESTART:
                        logger.error(
                            "Refusing relay ch%d: that is the powerbox's own restart "
                            "relay, not a load (use powerbox_hard_restart)", ch)
                    elif ch == RELAY_CH_RS485 and self.config.satellites_enabled:
                        # The RS485 rail is owned by the satellite power rule
                        # (wake-locks); translate a manual toggle into a manual
                        # hold so the rule and the operator don't fight.
                        from ..state.actions import SatellitePowerHoldAction
                        store.dispatch(SatellitePowerHoldAction(
                            "manual:ui", acquire=action.on))
                    elif pp is not None and ch == pp.camera.relay_ch:
                        if self.roadsense_power is not None and self.roadsense_power.holds_camera():
                            logger.warning(
                                "Ignoring manual relay toggle for the camera hub (ch%d) — "
                                "owned by the roadsense manager while the stack runs "
                                "or a camera preview holds it (state %s; use "
                                "roadsense_mode off / stop the preview)", ch,
                                self.roadsense_power.state)
                        else:
                            pp.camera.set(action.on)
                    elif pp is not None and ch == pp.gateway.relay_ch:
                        # Keep desired-state bookkeeping consistent with the
                        # UI path for the gateway.
                        from ..state.actions import SetGatewayUsbPowerAction
                        store.dispatch(SetGatewayUsbPowerAction(
                            action.on, source=ActionSource.UI))
                    elif pp is not None and ch == pp.mfd.relay_ch:
                        if self.mfd_power is not None:
                            logger.warning(
                                "Ignoring manual relay toggle for MFD port (ch%d) — "
                                "owned by the MFD power manager", ch)
                        else:
                            # MFD manager disabled (e.g. bench work): manual
                            # control with full sequencing + enforcement.
                            pp.mfd.set(action.on)
                    else:
                        self.powerbox_commander.set_relay(ch, action.on)
                elif type(action).__name__ == "PowerboxI2cScanAction":
                    self.powerbox_commander.request_i2c_scan()
                elif type(action).__name__ == "PowerboxAccSimAction":
                    logger.warning("Powerbox ACC override requested: %s (BENCH ONLY)",
                                   action.on)
                    self.powerbox_commander.set_acc_sim(action.on)
                elif type(action).__name__ == "PowerboxBeanAction":
                    self.powerbox_commander.send_bean(action.op, **action.params)
                elif type(action).__name__ == "PowerboxBeanRxAction":
                    self.powerbox_commander.bean_rx(on=action.on, op=action.op, raw=action.raw)
                elif type(action).__name__ == "PowerboxBeanTxAction":
                    logger.warning("Powerbox BEAN TX %s %s requested", action.op, action.params)
                    self.powerbox_commander.send_bean_tx(action.op, **action.params)
                elif type(action).__name__ == "PowerboxBeanTxEnableAction":
                    logger.warning("Powerbox BEAN TX guard %s requested",
                                   "ARM (60 s)" if action.on else "disarm")
                    self.powerbox_commander.bean_tx_enable(action.on)

        twin.store.add_middleware(_powerbox_middleware)

        def _system_middleware(action, store) -> None:
            from ..state.actions import ActionSource
            if getattr(action, "source", None) != ActionSource.UI:
                return
            if type(action).__name__ != "SetGatewayUsbPowerAction":
                return
            # Gateway VBUS goes through powerbox relay ch3 (CAN hub: gateway + panda) (data-off sequencing
            # + verification inside RelayPortPower). The desired state recorded
            # here is kept converged by _port_power_enforce_tick.
            if self.port_power is None:
                logger.error("Cannot toggle gateway USB power: no port power controllers")
                return
            try:
                self.port_power.gateway.set(bool(action.on))
            except Exception:
                logger.exception("Gateway port power toggle failed")

        twin.store.add_middleware(_system_middleware)

        def request_shutdown(reason: str) -> None:
            # Tell the box first so it stops its wake ladder (it goes to
            # "sleep" until ACC rises), then halt ourselves: since firmware
            # 2.0 nothing else can shed the POCO's load.
            commander.request_power_off(reason=reason, grace_s=cfg.shutdown_grace_s)
            if cfg.replay_file:
                # Replayed telemetry must never halt a developer's machine.
                logger.warning("Under-voltage in replay mode — local poweroff suppressed (%s)", reason)
            elif cfg.local_poweroff_on_undervoltage:
                logger.warning("Triggering local poweroff: %s", reason)
                try:
                    os.system("systemctl poweroff")  # noqa: S605 - controlled command
                except Exception:
                    logger.exception("Local poweroff failed")
            else:
                logger.warning("Local poweroff disabled — POCO keeps running on %s", reason)

        twin.rules_engine.register(
            PowerModeRule(apply_mode=controller.set_for_ignition,
                          hold_low=self._roadsense_holds_low)
        )

        # Persisted UI overrides win over the CLI defaults; mirror the active
        # values into the state so the dashboard can display and edit them.
        from ..persistence import SettingsManager
        from ..state.actions import SetUndervoltageConfigAction
        power_settings = SettingsManager().settings.power
        uv_threshold = power_settings.undervoltage_threshold or cfg.undervoltage_threshold
        uv_recover = power_settings.undervoltage_recover or cfg.undervoltage_recover

        twin.rules_engine.register(
            UndervoltageProtectionRule(
                request_shutdown=request_shutdown,
                threshold=uv_threshold,
                recover_threshold=uv_recover,
                confirm_seconds=cfg.undervoltage_confirm_s,
                grace_seconds=cfg.shutdown_grace_s,
            )
        )
        twin.store.dispatch(SetUndervoltageConfigAction(uv_threshold, uv_recover))

        def _uv_config_middleware(action, store) -> None:
            from ..state.actions import ActionSource
            if getattr(action, "source", None) != ActionSource.UI:
                return
            if type(action).__name__ != "SetUndervoltageConfigAction":
                return
            try:
                sm = SettingsManager()
                sm.settings.power.undervoltage_threshold = float(action.threshold)
                sm.settings.power.undervoltage_recover = float(action.recover)
                sm.save()
                logger.info(
                    "Undervoltage thresholds set from UI: trip %.2fV / recover %.2fV (persisted)",
                    action.threshold, action.recover,
                )
            except Exception:
                logger.exception("Failed to persist undervoltage thresholds")

        twin.store.add_middleware(_uv_config_middleware)

        logger.info(
            "Powerbox computer-side wired (undervoltage<%.1fV, flag=%s)",
            uv_threshold, cfg.power_mode_flag,
        )

    def _wire_satellites(self, twin: VirtualTwin) -> None:
        """Wire the satellite subsystem: registry (transports, keychain, auth,
        per-transport queues), RS485 rail power rules, twin.

        Power model (RS485, unchanged): the rail (relay ch5) is on iff at least
        one wake-lock holder is held — ``acc`` (ignition on), ``queue`` (RS485
        jobs pending/running) or ``manual:*``. The RS485 queue serializes all
        RS485 work, so overlapping jobs share one rail power-up with no
        flapping. BLE/Wi-Fi satellites have their own queues that hold no
        wake-lock (docs/SATELLITES.md §5). Dev-safe: without a powerbox link
        the rail command is log-only and RS485 jobs run assuming the rail is up.
        """
        import json as _json
        cfg = self.config
        store = twin.store

        # Persisted per-satellite options -> auto re-push after satellite boot.
        desired_configs = {}
        try:
            from ..persistence import SettingsManager
            desired_configs = SettingsManager().settings.satellites.desired_configs()
        except Exception:
            logger.exception("Failed to load persisted satellite configs")

        poll_payload = None
        try:
            poll_payload = _json.loads(cfg.satellite_poll_payload)
        except ValueError:
            logger.error("Bad satellite_poll_payload %r — poll disabled",
                         cfg.satellite_poll_payload)
        try:
            poll_devices = tuple(int(d) for d in cfg.satellite_poll_devices)
        except (TypeError, ValueError):
            logger.error("Bad satellite_poll_devices %r", cfg.satellite_poll_devices)
            poll_devices = ()

        # BLE/Wi-Fi satellite payloads reach the same per-device handlers the
        # gateway ingress uses for RS485 satellites (when one is registered).
        def _on_message(sat_id: int, payload: dict) -> None:
            handlers = getattr(twin.ingress, "_satellite_handlers", {}) or {}
            handler = handlers.get(sat_id)
            if handler is None:
                logger.debug("Satellite %d message (no handler): %s", sat_id, payload)
                return
            actions = handler(payload) or []
            for a in actions:
                store.dispatch(a)

        registry = SatelliteRegistry(
            store,
            output_port=twin.output_port,
            transports_enabled=tuple(cfg.satellite_transports),
            keychain_dir=cfg.satellite_keychain_dir,
            rediscover_s=cfg.satellite_rediscover_s,
            discovery_ttl_s=cfg.satellite_discovery_ttl_s,
            offline_after_s=cfg.satellite_offline_after_s,
            desired_configs=desired_configs,
            poll_devices=poll_devices,
            poll_payload=poll_payload,
            wifi_options=dict(
                bind=cfg.satellite_wifi_bind,
                udp_port=cfg.satellite_wifi_udp_port,
                tcp_port=cfg.satellite_wifi_tcp_port,
                iface=cfg.satellite_wifi_iface,
                ap_addr=cfg.satellite_wifi_ap_addr,
            ),
            ble_socket_path=cfg.satellite_ble_socket,
            on_message=_on_message,
        )
        queue = registry.queue
        scheduler = registry.scheduler
        supervisor = registry.supervisor

        # Rules: ACC wake-lock + holder-set -> physical rail (relay ch5).
        def _set_rail(on: bool) -> bool:
            if self.powerbox_commander is None:
                logger.info("RS485 rail -> %s (no powerbox commander, log-only)", on)
                return False
            return self.powerbox_commander.set_rs485_power(on)

        # Gateway (CAN/AVC + RS485 master) is bonded to the same wake-lock
        # logic: needed with ACC (CAN/AVC) and for satellite jobs (RS485).
        # Dispatch drives _system_middleware (uhubctl toggle) and sets the
        # desired state that _gateway_usb_power_tick keeps enforcing.
        def _set_gateway(on: bool) -> None:
            from ..state.actions import SetGatewayUsbPowerAction, ActionSource
            store.dispatch(SetGatewayUsbPowerAction(on, source=ActionSource.UI))

        twin.rules_engine.register(SatelliteAccHoldRule())
        twin.rules_engine.register(SatellitePowerRule(
            set_rail=_set_rail,
            linger_s=cfg.satellite_linger_s,
            retry_s=cfg.satellite_retry_s,
            set_gateway=_set_gateway,
        ))

        # Route EnqueueSatelliteCommandAction (API/events) into the registry
        # (RS485 ids -> the rail queue exactly as before; BLE/Wi-Fi ids -> their
        # transport queue) and operator registry commands into the registry.
        def _satellite_middleware(action, _store) -> None:
            kind = type(action).__name__
            if kind == "EnqueueSatelliteCommandAction":
                registry.send(action.device_id, action.payload,
                              priority=action.priority, name=action.name)
            elif kind == "SatelliteRegistryCommandAction":
                registry.handle_command(action.op, action.params)
        store.add_middleware(_satellite_middleware)

        # Optional periodic key-off sensor wake ("every 5 min read sensors").
        if cfg.satellite_poll_interval_s > 0 and poll_devices and poll_payload is not None:
            from .satellites import PeriodicJobSpec, PRIORITY_SCHEDULED, SatelliteJob

            rs485_devices = tuple(d for d in poll_devices if 100 <= d <= 199)

            def _poll_factory() -> SatelliteJob:
                def start(ctx) -> None:
                    for dev in rs485_devices:
                        ctx.send(dev, dict(poll_payload))
                return SatelliteJob(
                    name="sched:poll", start=start,
                    priority=PRIORITY_SCHEDULED,
                )

            if rs485_devices:
                scheduler.add(PeriodicJobSpec(
                    name="sched:poll",
                    interval_s=cfg.satellite_poll_interval_s,
                    factory=_poll_factory,
                ))
            # BLE/Wi-Fi ids in the poll list: one plain command job per cycle
            # on their own queue (no rail involved).
            for dev in poll_devices:
                if 100 <= dev <= 199:
                    continue
                transport = registry.transport_of(dev)
                if transport not in registry.queues:
                    continue
                scheduler.add(PeriodicJobSpec(
                    name=f"sched:poll:{dev}",
                    interval_s=cfg.satellite_poll_interval_s,
                    factory=(lambda d=dev: SatelliteJob(
                        name=f"sched:poll:{d}",
                        start=lambda ctx, d=d: ctx.send(d, dict(poll_payload)),
                        priority=PRIORITY_SCHEDULED, requires_online=(d,))),
                    transport=transport,
                ))

        registry.start()
        self.satellite_registry = registry
        self.satellite_queue = queue
        self.satellite_scheduler = scheduler
        self.satellite_supervisor = supervisor
        logger.info(
            "Satellite subsystem wired (transports=%s, linger=%.0fs, offline_after=%.0fs, "
            "poll=%.0fs %s, configs=%s, keychain=%s)",
            ",".join(sorted(registry.transports)) or "none",
            cfg.satellite_linger_s, cfg.satellite_offline_after_s,
            cfg.satellite_poll_interval_s, cfg.satellite_poll_devices,
            sorted(desired_configs) or "none", registry.keychain.directory,
        )

    def _vfd_clock_tick(self) -> None:
        """Sync the VFD satellite's RTC (drives its idle clock screen).

        Sends a "K" time-sync message once a minute; the satellite keeps
        time itself between syncs, and a reboot re-syncs within a minute.
        """
        if self.twin is None:
            return
        now = time.monotonic()
        if now - self._vfd_clock_last < 60.0:
            return
        self._vfd_clock_last = now
        lt = time.localtime()
        self.twin.egress.send_command(OutgoingCommand(
            device_id=DEVICE_VFD,
            command_type="K",
            payload={
                "t": "K",
                "y": lt.tm_year, "mo": lt.tm_mon, "d": lt.tm_mday,
                "h": lt.tm_hour, "mi": lt.tm_min, "s": lt.tm_sec,
            },
        ))

    def _satellite_tick(self) -> None:
        """Advance the satellite registry (transports, auth, queues, scheduler,
        ~1 Hz supervisor) from the engine loop."""
        if self.satellite_registry is None:
            return
        self.satellite_registry.tick()

    def _wire_recording(self, twin: VirtualTwin) -> None:
        """Tap ingress/egress into a rotating per-trip recorder."""
        recorder = TripRecorder(self.config.recording)
        self.recorder = recorder

        # Record everything the ingress decodes (IN) and egress sends (OUT).
        twin.ingress.add_message_log_callback(recorder.log_incoming)
        twin.egress.set_message_log_callback(recorder.log_outgoing)

        # Bound trips by the powerbox ignition (ACC) when configured/available.
        if self.config.recording.use_ignition:
            def _on_powerbox(state) -> None:
                recorder.on_ignition(state.powerbox.acc_on)

            self._unsubscribe_powerbox = twin.store.subscribe(
                StateSlice.POWERBOX, _on_powerbox
            )

        logger.info(
            "Trip recording wired (dir=%s, segmentation=%s)",
            self.config.recording.directory, self.config.recording.segmentation,
        )

    # ── USB hotplug ──────────────────────────────────────────────────────────

    def _held_ports(self) -> set:
        """Paths currently held open by connected serial ports (don't reprobe)."""
        held = set()
        for sp in (self._gateway_serial, self._powerbox_serial):
            if sp is not None and sp.is_connected():
                held.add(sp.config.port)
        return held

    def _on_usb_change(self, added: set, removed: set, current: list) -> None:
        """Re-discover roles among newly-added devices and retarget serials.

        Only newly-added devices are probed, so we never fight a port a running
        SerialPort already holds open. by-id paths are stable across replug, so
        an already-connected role needs no action.
        """
        if not added:
            return
        roles = discover_roles_combined(
            port_roles=self.config.usb_port_roles,
            hub=self.config.usb_hub,
            candidates=sorted(added),
            skip=self._held_ports(),
            probe_timeout=self.config.discovery_timeout,
            use_whoami_fallback=self.config.usb_probe_fallback,
        )
        gw = roles.get(ROLE_GATEWAY)
        pb = roles.get(ROLE_POWERBOX)
        if gw and self._gateway_serial is not None and not self._gateway_serial.is_connected():
            self._gateway_serial.retarget(gw)
        if pb and self._powerbox_serial is not None and not self._powerbox_serial.is_connected():
            self._powerbox_serial.retarget(pb)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start everything and run the engine loop until stop() is called."""
        if self.twin is None:
            self.build()
        assert self.twin and self.sink and self.api and self.bridge

        logger.info("Starting backend service")
        if not self.twin.start():
            logger.warning("Virtual twin failed to start (gateway not connected?)")
        if self.panda is not None:
            if self.panda.start():
                self.twin.store.dispatch(SetPandaStateAction(panda_enabled=True))
            else:
                logger.error("Panda broadcast CAN source failed to start (libusb-1.0 missing?)")
                self.panda = None
        if self.recorder is not None:
            self.recorder.start()
        self.sink.start()
        self.api.start()
        self.zmq.start()
        if self.dashcam is not None:
            try:
                self.dashcam.start()
            except Exception:
                logger.exception("Dashcam light ingest failed to start")

        # USB hotplug: re-discover + retarget serial ports on plug/unplug.
        if (
            self.config.usb_hotplug
            and not self.config.replay_file
            and (self._gateway_serial is not None or self._powerbox_serial is not None)
        ):
            monitor = UsbSerialMonitor(
                self._on_usb_change, interval=self.config.hotplug_interval
            )
            monitor.prime(enumerate_candidates())
            monitor.start()
            self._usb_monitor = monitor

        try:
            while not self._stop.is_set():
                loop_start = time.monotonic()
                try:
                    self.twin.update()
                    self.bridge.drain_commands()
                    self._acc_debounce_tick()
                    self._powerbox_heartbeat_tick()
                    self._powerbox_watchdog_tick()
                    self._gateway_watchdog_tick()
                    self._gateway_recover_tick()
                    self._gateway_usb_power_tick()
                    self._port_power_enforce_tick()
                    self._mfd_power_tick()
                    self._camera_preview_tick()
                    self._roadsense_power_tick()
                    self._poco_power_tick()
                    self._chassis_fan_tick()
                    self._vfd_clock_tick()
                    self._satellite_tick()
                    self._avc_tick()
                    self._bean_tick()
                    self._panda_tick()
                    self._dashcam_tick()
                    if self.diag is not None:
                        self.diag.tick()
                    if self.recorder is not None:
                        self.recorder.tick()
                except Exception:
                    logger.exception("Engine loop iteration failed")
                
                period = 1.0 / max(1.0, self.config.tick_hz)
                elapsed = time.monotonic() - loop_start
                if elapsed < period:
                    self._stop.wait(period - elapsed)
        finally:
            self._shutdown()

    def stop(self) -> None:
        """Signal the engine loop to exit (safe to call from any thread)."""
        self._stop.set()

    def _mfd_power_tick(self) -> None:
        """Advance the MFD video-board power manager (ACC follower)."""
        if self.mfd_power is None or self.twin is None:
            return
        acc = bool(self.twin.store.state.powerbox.acc_on)
        try:
            self.mfd_power.tick(acc)
        except Exception:
            logger.exception("MFD power tick failed")

    def _camera_preview_tick(self) -> None:
        """Apply camera preview leases (before the roadsense tick, which acts
        on the preview hold)."""
        if self.camera_preview is None:
            return
        try:
            self.camera_preview.tick()
        except Exception:
            logger.exception("camera preview tick failed")

    def _roadsense_power_tick(self) -> None:
        """Advance the roadsense stack manager (ACC follower) and finish a
        deferred key-off "low" CPU profile once the stack is down."""
        if self.roadsense_power is None or self.twin is None:
            return
        acc = bool(self.twin.store.state.powerbox.acc_on)
        try:
            self.roadsense_power.tick(acc)
        except Exception:
            logger.exception("roadsense tick failed")
        # The stack needs the Gold cluster (modeld/signd/envd pin cpu4-7, which
        # the "low" profile takes offline): whenever it is meant to run, hold
        # the "full" profile — also with the key off (operator mode "on").
        if (self.roadsense_power.is_active() and self.power_controller is not None
                and self.twin.store.state.powerbox.power_mode != "full"):
            try:
                from ..state.actions import SetPowerboxPowerModeAction
                self.power_controller.set_mode("full")
                self.twin.store.dispatch(SetPowerboxPowerModeAction("full"))
                logger.info("POCO power profile -> full (roadsense %s)", self.roadsense_power.state)
                if not acc:
                    self._low_mode_deferred = True
            except Exception:
                logger.exception("Full power profile for roadsense failed")
        if self._low_mode_deferred and self.roadsense_power.is_off():
            self._low_mode_deferred = False
            if not acc and self.power_controller is not None:
                try:
                    from ..state.actions import SetPowerboxPowerModeAction
                    self.power_controller.set_for_ignition(False)
                    self.twin.store.dispatch(SetPowerboxPowerModeAction("low"))
                    logger.info("POCO power profile -> low (deferred until roadsense stopped)")
                except Exception:
                    logger.exception("Deferred low power profile failed")

    def _roadsense_holds_low(self) -> bool:
        """PowerModeRule hook: keep the Gold cluster online while roadsense runs.

        Returns True (and arms the deferred switch) when the stack is still up
        at key-off; :meth:`_roadsense_power_tick` applies "low" once it is off.
        """
        if self.roadsense_power is None or self.roadsense_power.is_off():
            return False
        self._low_mode_deferred = True
        logger.info("Key off: 'low' CPU profile deferred until roadsense has stopped "
                    "(state=%s)", self.roadsense_power.state)
        return True

    def _acc_debounce_tick(self) -> None:
        """Release a held ACC-off once the debounce window has elapsed."""
        if self._acc_debounce is None or self.twin is None:
            return
        try:
            action = self._acc_debounce.due()
        except Exception:
            logger.exception("ACC debounce tick failed")
            return
        if action is not None:
            self.twin.store.dispatch(action)

    def _powerbox_heartbeat_tick(self) -> None:
        """Send the POCO->powerbox heartbeat (rolling counter) on cadence.

        Lets the powerbox know the POCO/backend is alive so it does not press the
        power button to "wake" a healthy POCO. No-op when powerbox is disabled or
        no commander/output port is wired.
        """
        cfg = self.config
        if not cfg.powerbox_enabled or self.powerbox_commander is None:
            return
        interval = cfg.powerbox_heartbeat_s
        if interval <= 0:
            return
        now = time.monotonic()
        if (now - self._pb_hb_last) < interval:
            return
        self._pb_hb_last = now
        self._pb_hb_counter = (self._pb_hb_counter + 1) & 0xFF
        self.powerbox_commander.send_heartbeat(self._pb_hb_counter)
        # One-shot: fetch the firmware power-event log shortly after the link is
        # up. After a cold boot the POCO/backend was OFF while the powerbox did
        # its wake sequence — this pulls that timeline into the journal so the
        # cold-boot behaviour is finally observable.
        if not self._pb_pmlog_fetched:
            self._pb_pmlog_fetched = True
            self.powerbox_commander.request_pmlog()

    # ── monotonic ageing of wall-clock state stamps ───────────────────────

    def _mono_seen(self, key: str, wall_stamp: Optional[float]) -> float:
        """Monotonic time at which ``wall_stamp`` (a ``time.time()`` value the
        reducers store for display) was last seen to CHANGE; the backend start
        when it never changed. Watchdogs age against this, never against the
        wall clock: the phone has no RTC and NTP steps the clock by hours."""
        mark = self._seen_marks.get(key)
        if mark is None:
            # First observation: a live stamp counts as fresh now, an empty
            # one ages from backend start.
            mark = (wall_stamp or None, time.monotonic() if wall_stamp else self._start_time)
            self._seen_marks[key] = mark
        elif wall_stamp and wall_stamp != mark[0]:
            mark = (wall_stamp, time.monotonic())
            self._seen_marks[key] = mark
        return mark[1]

    def _mono_age(self, key: str, wall_stamp: Optional[float]) -> float:
        return time.monotonic() - self._mono_seen(key, wall_stamp)

    # ── panda bench (gateway tx -> panda rx on an isolated segment) ──────

    def _panda_bench_middleware(self, action, _store) -> None:
        name = type(action).__name__
        if name == "GatewayCanTxAction":
            if self.twin is None:
                return
            if action.bench:
                self.twin.ingress.bench_mark_tx(action.can_id, action.data)
            ok = self.twin.output_port.send(OutgoingCommand(
                device_id=DEVICE_CAN, command_type="tx",
                payload={"a": "tx", "i": "0x%03X" % action.can_id, "d": list(action.data)}))
            if not ok:
                logger.warning("gateway_can_tx 0x%03X: gateway port not connected", action.can_id)
        elif name == "PandaBenchAction":
            if self.twin is not None and action.reset:
                self.twin.ingress.bench_reset()
        elif name == "DtcCommandAction":
            if self.twin is None:
                return
            ing, out = self.twin.ingress, self.twin.output_port
            if not out.is_connected():
                logger.warning("dtc %s: gateway port not connected", action.op)
                return
            logger.info("DTC command from UI: %s", action.op)
            if action.op == "clear":
                ing.clear_dtcs(output_port=out)
            else:
                ing.start_dtc_scan(output_port=out, mode=0x07 if action.op == "scan_pending" else 0x03)

    # ── panda status mirror ──────────────────────────────────────────────

    def _panda_tick(self) -> None:
        """Mirror the panda link/counters into ConnectionState (~1 Hz)."""
        if self.panda is None or self.twin is None:
            return
        now = time.monotonic()
        if now - self._panda_status_last < self.config.panda_status_s:
            return
        self._panda_status_last = now
        st = self.panda.status()
        ist = self.twin.ingress.stats
        fields = dict(
            panda_enabled=True,
            panda_connected=st.connected,
            panda_serial=st.serial or None,
            panda_fw=st.fw_version or None,
            panda_safety_mode=st.safety_mode,
            panda_power_save=st.power_save,
            panda_frames=st.frames,
            panda_fps=round(self.panda.fps, 1) if st.connected else None,
            panda_bus_counts=dict(st.bus_counts) if st.bus_counts else None,
            panda_last_frame_time=st.last_frame_wall or None,
            panda_last_error=st.last_error or None,
            panda_decoded=ist.broadcast_decoded,
            panda_dispatched=ist.broadcast_dispatched,
            panda_fw_format=st.packet_format or None,
            panda_can_rx_errs=st.can_rx_errs,
            panda_bench=self.twin.ingress.bench_results(),
        )
        bench = fields["panda_bench"]
        key = (st.connected, st.frames, st.last_error, ist.broadcast_dispatched,
               bench["n"], bench["lost"], bench["pending"])
        if key == self._panda_last_mirror:
            return
        self._panda_last_mirror = key
        self.twin.store.dispatch(SetPandaStateAction(**fields))

    def _powerbox_watchdog_tick(self) -> None:
        """Flip powerbox.connected -> False when the link goes silent, and
        (optionally) force a link reset to recover it.

        The powerbox streams telemetry + STATUS at ~1 Hz. The serial reader only
        reports a disconnect on an OSError/SerialException; a USB-CDC *wedge*
        (board alive, host stops receiving) produces neither — ``readline()``
        just returns empty forever. Without this watchdog ``connected`` stays
        True and the last frame (voltage, acc_on, OUT rails…) is shown forever as
        if live. Here we watch ``last_update_time``: if no frame for
        ``powerbox_stale_s`` we mark the link disconnected (the reducers restore
        ``connected=True`` automatically when frames resume). Edge-triggered so it
        logs once per stall.

        Recovery: when ``powerbox_auto_recover`` is enabled we additionally force
        the serial port to close+reopen.  If the RP2040 has disappeared from the
        USB bus entirely (failed re-enumeration), ``force_reconnect`` escalates to
        resetting the parent USB hub, which power-cycles all ports and forces
        re-enumeration.
        """
        cfg = self.config
        if not cfg.powerbox_enabled or self.twin is None:
            return
        stale_s = cfg.powerbox_stale_s
        if stale_s <= 0:
            return
        pb = self.twin.store.state.powerbox
        age = self._mono_age("powerbox_frame", pb.last_update_time)
        if age > stale_s:
            if not self._pb_stale:
                self._pb_stale = True
                logger.warning(
                    "Powerbox link STALE: no frame for %.1fs (>%.1fs). USB-CDC "
                    "likely wedged (board alive, host not receiving); marking "
                    "disconnected.",
                    age, stale_s,
                )
                if pb.connected:
                    self.twin.store.dispatch(
                        SetPowerboxConnectionAction(connected=False)
                    )
                # Forget the logged identity so the first IDENT/READY after the
                # link recovers logs at INFO again ("link came back").
                reset_powerbox_identity_log()
            self._maybe_recover_powerbox(pb, age)
        elif self._pb_stale:
            self._pb_stale = False
            self._pb_recover_attempts = 0
            logger.info(
                "Powerbox link RECOVERED: fresh frame after stall (age %.1fs).",
                age,
            )

    def _gateway_watchdog_tick(self) -> None:
        """Flip connection.connected -> False when the gateway stops heartbeating.

        The gateway firmware (>= 2.28.0) emits a ~1 Hz GW_HB liveness heartbeat
        (see io/ingress._handle_system_message). The gateway is intentionally
        power-cycled with ACC (ignition) for power saving, so when ignition drops
        it stops heartbeating and vanishes from USB-CDC. Without this the UI would
        keep showing the last "connected=True" forever. We watch
        ``last_heartbeat_time``: if no heartbeat for ``gateway_stale_s`` we mark
        the gateway disconnected; the ingress restores ``connected=True``
        automatically when heartbeats resume after the next ACC cycle. The
        watchdog only arms once at least one heartbeat has been seen, so a
        pre-2.28.0 gateway (no heartbeat) is never falsely disconnected.
        Edge-triggered logging — one line per stall/recovery.
        """
        cfg = self.config
        if self.twin is None:
            return
        stale_s = cfg.gateway_stale_s
        if stale_s <= 0:
            return
        conn = self.twin.store.state.connection
        last = conn.last_heartbeat_time
        if not last:
            return  # no heartbeat ever seen — don't arm (old fw / not present)
        age = self._mono_age("gateway_hb", last)
        if age > stale_s:
            if not self._gw_stale:
                self._gw_stale = True
                logger.warning(
                    "Gateway link STALE: no heartbeat for %.1fs (>%.1fs). "
                    "Key-off VBUS cut or a wedged MCU; marking disconnected.",
                    age, stale_s,
                )
                if conn.connected:
                    self.twin.store.dispatch(
                        SetConnectionStateAction(connected=False)
                    )
        elif self._gw_stale:
            self._gw_stale = False
            logger.info(
                "Gateway link RECOVERED: heartbeat resumed (age %.1fs).", age,
            )

    def _gateway_recover_tick(self) -> None:
        """Auto-recover a wedged gateway by cold-cycling its VBUS relay (ch3, the CAN hub it shares with the panda).

        The gateway CDC link occasionally wedges: the board stays enumerated
        (USB resets re-attach ttyACM but don't reboot the MCU) yet GW_HB
        heartbeats never resume. The only known fix is a VBUS cut, which
        cold-boots the gateway MCU (verified manually 2026-08-09).

        Trip conditions (all must hold):
        - heartbeats stale for >= gateway_recover_stale_s (well past the 8 s
          UI watchdog, so ACC transitions / brief hiccups don't cycle power).
          If no heartbeat was EVER seen this boot (gateway already wedged
          before the backend started), staleness is measured from backend
          start instead;
        - the gateway VBUS relay is actually ON per the powerbox "rly" mirror
          (an ACC power-save cut legitimately silences the gateway — skip);
        - nobody has deliberately requested the port off (desired != False);
        - the backoff window since the previous cycle has elapsed (base
          cooldown doubles per consecutive failed attempt, capped at
          gateway_recover_max_cooldown_s; a resumed heartbeat resets it).

        The cycle is two-phase and non-blocking: VBUS off now, back on after
        gateway_recover_off_s on a later tick. RelayPortPower handles the hub
        data-off sequencing on both edges, and desired-state enforcement
        converges the ON command if the lossy CDC link drops it.
        """
        cfg = self.config
        if not cfg.gateway_auto_recover or self.port_power is None or self.twin is None:
            return
        gw = self.port_power.gateway
        now = time.monotonic()

        # VBUS edge: a board that has just been (re)powered needs its boot time
        # before silence means "wedged". Key-on turns relay ch3 back on after
        # minutes of key-off silence; measured from the last heartbeat that
        # looked like a 737 s stall and cold-cycled a healthy, still-booting
        # gateway 2 s after power-up (2026-11-04). Staleness is therefore
        # measured from the LATER of the last heartbeat and the VBUS-on edge.
        if gw.read() is True:
            if self._gw_vbus_on_at is None:
                self._gw_vbus_on_at = now
        else:
            self._gw_vbus_on_at = None

        # Phase 2: mid-cycle — re-power once the off window has elapsed.
        if self._gw_recover_off_at is not None:
            if now - self._gw_recover_off_at >= cfg.gateway_recover_off_s:
                self._gw_recover_off_at = None
                logger.warning("Gateway recovery: VBUS back ON (relay ch%d)", gw.relay_ch)
                gw.set(True)
            return

        conn = self.twin.store.state.connection
        last = conn.last_heartbeat_time
        hb_mono = self._mono_seen("gateway_hb", last) if last else 0.0
        if last:
            if not self._gw_stale:
                return
            since = hb_mono
        else:
            # No heartbeat ever seen this boot: either the gateway was already
            # wedged before we started (recoverable) or it runs pre-2.28.0
            # firmware with no GW_HB (a cycle is harmless — it reboots into
            # the same silence and the cooldown caps the rate). Measure
            # staleness from backend start.
            since = self._gw_recover_epoch
        if self._gw_vbus_on_at is not None and self._gw_vbus_on_at > since:
            since = self._gw_vbus_on_at
        age = now - since
        if age < cfg.gateway_recover_stale_s:
            return
        # A heartbeat after the last cycle proves recovery worked — reset backoff.
        if last and hb_mono > self._gw_recover_last:
            self._gw_recover_attempts = 0
        cooldown = min(
            cfg.gateway_recover_cooldown_s * (2 ** self._gw_recover_attempts),
            cfg.gateway_recover_max_cooldown_s,
        )
        if now - self._gw_recover_last < cooldown:
            return
        if gw.desired is False:
            return  # operator/rule wants the port off — don't fight it
        if gw.read() is not True:
            return  # VBUS already off (ACC power-save) or state unknown
        self._gw_recover_last = now
        self._gw_recover_attempts += 1
        logger.warning(
            "Gateway link stale %.0fs with VBUS on — cold power-cycling relay "
            "ch%d to reboot the wedged MCU (attempt %d, off %.0fs, next retry "
            "in >=%.0fs)",
            age, gw.relay_ch, self._gw_recover_attempts,
            cfg.gateway_recover_off_s,
            min(cfg.gateway_recover_cooldown_s * (2 ** self._gw_recover_attempts),
                cfg.gateway_recover_max_cooldown_s),
        )
        gw.set(False)
        self._gw_recover_off_at = now

    def _gateway_usb_power_tick(self) -> None:
        """Mirror the gateway's actual VBUS state into the store.

        Ground truth now comes for free from the powerbox STATUS "rly"
        telemetry (relay ch3 = CAN hub VBUS) — no uhubctl subprocess polling.
        Dispatches an INTERNAL-sourced SetGatewayUsbPowerAction when the mirror
        drifts; INTERNAL does NOT re-trigger _system_middleware, it only
        reflects state for the UI. Desired-state enforcement itself lives in
        _port_power_enforce_tick (RelayPortPower.enforce)."""
        cfg = self.config
        if self.twin is None or self.port_power is None:
            return
        interval = getattr(cfg, "gateway_usb_poll_s", 10.0)
        if interval <= 0:
            return
        now = time.monotonic()
        if (now - self._gwusb_last_poll) < interval:
            return
        self._gwusb_last_poll = now

        powered = self.port_power.gateway.read()
        if powered is None:
            return
        conn_state = self.twin.store.state.connection
        if conn_state.gateway_usb_power != powered:
            from ..state.actions import SetGatewayUsbPowerAction, ActionSource
            self.twin.store.dispatch(
                SetGatewayUsbPowerAction(powered, source=ActionSource.INTERNAL)
            )

    def _port_power_enforce_tick(self) -> None:
        """Converge relay-backed ports to their desired states.

        RelayPortPower records desired state on every set(); this sweep
        re-issues the data+VBUS sequence whenever the powerbox "rly" telemetry
        disagrees. This is what makes relay commands reliable over the lossy
        CDC link, and what restores port states after a hub reset (which
        cold-boots the powerbox and releases every relay)."""
        cfg = self.config
        if self.port_power is None:
            return
        # Every tick: finish pending OFF sequences (data-off right after the
        # device has dropped off the bus — see RelayPortPower.tick).
        self.port_power.tick_all()
        now = time.monotonic()
        if self._pp_reassert_at is not None and now >= self._pp_reassert_at:
            # Post-boot re-assert (POWERBOX_READY seen): push every wanted
            # state unconditionally, then let the normal drift sweep verify.
            self._pp_reassert_at = None
            self._pp_enforce_last = now
            n = self.port_power.reassert_all()
            logger.info("Powerbox back: re-asserted %d relay port state(s)", n)
            return
        interval = getattr(cfg, "port_power_enforce_s", 15.0)
        if interval <= 0:
            return
        if (now - self._pp_enforce_last) < interval:
            return
        self._pp_enforce_last = now
        self.port_power.enforce_all()

    # ── POCO thermal zone mapping ────────────────────────────────────────────
    # Zones on the Poco F1 (SDM845 / beryllium):
    #   cpu0..cpu7-thermal, cluster0/1-thermal  → CPU cores / clusters
    #   gpu-top-thermal, gpu-bottom-thermal      → GPU
    #   qcom-battery                             → battery
    #   aoss*, mem, wlan, camera, video, modem   → SoC peripherals
    # We track the hottest CPU/cluster, GPU, and battery independently.
    _THERMAL_CPU_PREFIXES = ("cpu", "cluster")
    _THERMAL_GPU_PREFIXES = ("gpu",)
    _THERMAL_BATTERY_NAMES = ("qcom-battery",)

    @staticmethod
    def _read_poco_thermals() -> tuple:
        """Read all sysfs thermal zones and return (max_cpu, max_gpu, battery) in °C.

        Returns (None, None, None) on systems without thermal zones.
        """
        import glob
        cpu_max = None
        gpu_max = None
        battery = None
        try:
            for zone_dir in glob.glob("/sys/class/thermal/thermal_zone*"):
                try:
                    with open(zone_dir + "/type", "r") as f:
                        zone_type = f.read().strip()
                    with open(zone_dir + "/temp", "r") as f:
                        temp_c = float(f.read().strip()) / 1000.0
                except (OSError, ValueError):
                    continue
                if any(zone_type.startswith(p) for p in BackendService._THERMAL_CPU_PREFIXES):
                    cpu_max = max(cpu_max, temp_c) if cpu_max is not None else temp_c
                elif any(zone_type.startswith(p) for p in BackendService._THERMAL_GPU_PREFIXES):
                    gpu_max = max(gpu_max, temp_c) if gpu_max is not None else temp_c
                elif zone_type in BackendService._THERMAL_BATTERY_NAMES:
                    battery = temp_c
        except Exception:
            pass
        return (cpu_max, gpu_max, battery)

    def _poco_power_tick(self) -> None:
        """Poll POCO's internal battery + thermal telemetry from sysfs (~1 Hz)."""
        now = time.monotonic()
        if now - self._poco_poll_last < 1.0:
            return
        self._poco_poll_last = now

        power_w = None
        try:
            with open("/sys/class/power_supply/qcom-battery/voltage_now", "r") as f:
                v_now = float(f.read().strip()) / 1_000_000.0  # uV to V
            with open("/sys/class/power_supply/qcom-battery/current_now", "r") as f:
                i_now = float(f.read().strip()) / 1_000_000.0  # uA to A
            power_w = abs(v_now * i_now)
        except (OSError, ValueError):
            pass

        cpu_temp, gpu_temp, batt_temp = self._read_poco_thermals()

        if self.twin and self.twin.store:
            from ..state.actions import SetPocoTelemetryAction
            self.twin.store.dispatch(SetPocoTelemetryAction(
                poco_power_w=power_w,
                poco_core_temp=cpu_temp,
                poco_gpu_temp=gpu_temp,
            ))

    def _chassis_fan_tick(self) -> None:
        """Chassis fan controller tick: feed the readings to the policy
        (:mod:`backend.fan_policy`) and send the resulting duty to the powerbox.

        Inputs: POCO die (max CPU, max GPU) from :meth:`_poco_power_tick`, the
        outside-of-box ambient (BMP2 @0x76; AHT20 fallback), the box-inside air
        (BMP1 @0x77; AHT20 fallback), the power mode and the manual override
        (``set_fan`` / ``fan_auto``).  The policy's control temperature is
        published as ``poco_ema_temp`` for the dashboard.
        """
        cfg = self.config
        if not cfg.chassis_fan_enabled or not cfg.powerbox_enabled:
            return
        if self.powerbox_commander is None:
            return
        now = time.monotonic()
        if now - self._fan_last_tick < cfg.chassis_fan_tick_s:
            return
        self._fan_last_tick = now

        if self.twin is None:
            return
        pb = self.twin.store.state.powerbox
        override = getattr(pb, "fan_override_pct", None)
        if override is None and not pb.connected:
            return

        decision = self.fan_policy.step(
            now,
            cpu_c=pb.poco_core_temp,
            gpu_c=pb.poco_gpu_temp,
            ambient_c=pb.bmp2_t if pb.bmp2_t is not None else pb.aht_t,
            box_in_c=pb.bmp_t if pb.bmp_t is not None else pb.aht_t,
            box_out_c=pb.bmp2_t,
            power_mode=pb.power_mode,
            override_pct=override,
        )
        self._set_fan_pct(decision.duty_pct)
        self._publish_poco_ema_temp(decision.control_c, decision.duty_pct)

    def _publish_poco_ema_temp(
        self, ema_temp: Optional[float], fan_duty_pct: Optional[float] = None
    ) -> None:
        """Publish the simulated heatsink (EMA) temperature to the store.

        Called every fan tick regardless of fan state so the dashboard's
        "Simulated Temp" always reflects the latest value.
        """
        if not (self.twin and self.twin.store):
            return
        from ..state.actions import SetPocoTelemetryAction
        self.twin.store.dispatch(SetPocoTelemetryAction(
            poco_ema_temp=ema_temp,
            fan_duty_pct=fan_duty_pct,
        ))

    def _set_fan_pct(self, pct: float) -> None:
        """Send the fan speed (percent) to the powerbox, de-duplicating
        unchanged values (1 % deadband; on/off edges always sent)."""
        pct = max(0.0, min(100.0, float(pct)))
        if self._fan_last_pct >= 0:
            if abs(pct - self._fan_last_pct) < 1.0 and (pct == 0) == (self._fan_last_pct == 0):
                return
        self._fan_last_pct = pct
        duty_pct = round(pct, 1)
        # Edge-triggered logging: log on meaningful changes (>5% or on/off).
        if abs(duty_pct - self._fan_last_log_pct) > 5.0 or \
                (duty_pct == 0) != (self._fan_last_log_pct == 0):
            self._fan_last_log_pct = duty_pct
            pb = self.twin.store.state.powerbox if self.twin else None
            poco_temps = [t for t in (getattr(pb, 'poco_core_temp', None),
                                      getattr(pb, 'poco_gpu_temp', None))
                          if t is not None] if pb else []
            poco_t = max(poco_temps) if poco_temps else None
            box_t = getattr(pb, 'bmp_t', None) if pb else None
            out_t = getattr(pb, 'bmp2_t', None) if pb else None
            _f = lambda v: "%.1f°C" % v if v is not None else "N/A"
            logger.info(
                "Chassis fan → %.0f%% (poco=%s box=%s outside=%s mode=%s)",
                duty_pct,
                _f(poco_t), _f(box_t), _f(out_t),
                getattr(pb, 'power_mode', '?') if pb else '?',
            )
        if self.powerbox_commander:
            self.powerbox_commander.set_fan(pct)

    def _powerbox_telemetry_fresh(self) -> bool:
        """True while the mirrored powerbox STATUS fields can be trusted.

        Same criterion as the staleness watchdog: no frame for
        ``powerbox_stale_s`` means the USB-CDC link is wedged, so every mirrored
        field — ``relays`` included — is frozen at its last value. Port power
        enforcement consults this before treating the ``rly`` mirror as truth.
        """
        if self.twin is None:
            return True  # replay / no twin: nothing better to go on
        stale_s = self.config.powerbox_stale_s
        if stale_s <= 0:
            return True
        last = self.twin.store.state.powerbox.last_update_time
        return self._mono_age("powerbox_frame", last) <= stale_s

    def _maybe_recover_powerbox(self, pb, age: float) -> None:
        """Force a powerbox serial reset to clear a wedged link, if enabled.

        Rate-limited by ``powerbox_recover_cooldown_s``. No-op when no powerbox
        serial handle is wired (e.g. replay mode).
        """
        cfg = self.config
        if not cfg.powerbox_auto_recover:
            return
        if self._powerbox_serial is None:
            return
            
        now = time.monotonic()
        if self._pb_recover_attempts < 0:
            self._pb_recover_attempts = 0
        # Exponential backoff: 20s, 40s, 80s … capped. Resets to the base
        # spacing the moment frames resume (see _powerbox_watchdog_tick).
        cooldown = cfg.powerbox_recover_cooldown_s * (2 ** self._pb_recover_attempts)
        cooldown = min(cooldown, cfg.powerbox_recover_max_cooldown_s)
        if (now - self._pb_recover_last) < cooldown:
            return
        self._pb_recover_last = now
        self._pb_recover_attempts += 1
        logger.warning(
            "Powerbox auto-recovery: forcing link reset (attempt #%d, link "
            "stale %.1fs, next attempt in >=%.0fs). Ladder: USBDEVFS_RESET "
            "first (no MCU reboot, relays kept), parent-hub reset on "
            "escalation.",
            self._pb_recover_attempts, age,
            min(cfg.powerbox_recover_cooldown_s * (2 ** self._pb_recover_attempts),
                cfg.powerbox_recover_max_cooldown_s),
        )
        try:
            self._powerbox_serial.force_reconnect(attempt=self._pb_recover_attempts)
            self._fan_last_pct = -1.0
        except Exception:
            logger.exception("Powerbox force_reconnect failed")

    def _shutdown(self) -> None:
        logger.info("Shutting down backend service")
        if self.panda is not None:
            try:
                self.panda.stop()
            except Exception:
                logger.exception("Error stopping panda CAN source")
        if self._usb_monitor is not None:
            try:
                self._usb_monitor.stop()
            except Exception:
                logger.exception("Error stopping USB monitor")
        if self.dashcam is not None:
            try:
                self.dashcam.stop()
            except Exception:
                logger.exception("Error stopping dashcam ingest")
        if self.camera_preview is not None:
            try:
                self.camera_preview.shutdown()
            except Exception:
                logger.exception("Error releasing the camera preview")
        if self.api is not None:
            self.api.stop()
        if self.zmq:
            self.zmq.stop()
        if self.recorder is not None:
            try:
                self.recorder.stop()
            except Exception:
                logger.exception("Error stopping trip recorder")
        if self.satellite_registry is not None:
            try:
                self.satellite_registry.stop()
            except Exception:
                logger.exception("Error stopping satellite transports")
        if self.sink is not None:
            self.sink.stop()
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
        if self._unsubscribe_powerbox is not None:
            try:
                self._unsubscribe_powerbox()
            except Exception:
                pass
        if self.nav_emulator is not None:
            # Like nav_emu.py at exit: clear the 178 ACK + auto-reply slots so
            # the gateway does not impersonate a nav nobody answers for.
            try:
                self.nav_emulator.enabled = False
                self.nav_emulator.deconfigure()
                time.sleep(0.1)   # let the serial writer flush
            except Exception:
                logger.exception("Error deconfiguring nav emulator")
        if self._nav_scheduler is not None:
            self._nav_scheduler.stop()
        if self.twin is not None:
            try:
                self.twin.stop()
            except Exception:
                logger.exception("Error stopping virtual twin")
        logger.info("Backend service stopped")
