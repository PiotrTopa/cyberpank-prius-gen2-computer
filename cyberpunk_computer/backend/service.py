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
from ..state.actions import SetPowerboxConnectionAction, SetConnectionStateAction
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
    command_job,
)
from ..api import ApiServer, StoreBridge
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
    # a silent wedge. Default OFF: resetting the MCU is only safe once OUT1
    # survives an MCU reset (self-latch hardware). Enable on the target once that
    # hardware mod is in place.
    powerbox_auto_recover: bool = False
    # Minimum spacing between recovery attempts (s). Prevents a permanently dead
    # link from being reset in a tight loop while it re-enumerates.
    powerbox_recover_cooldown_s: float = 20.0
    # Whether a tripped under-voltage also powers the POCO off locally. Off by
    # default; the powerbox is expected to cut the rail. Enable on the target.
    local_poweroff_on_undervoltage: bool = False
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
    # gateway_recover_stale_s AND the gateway VBUS relay (ch4) is actually ON
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

    # Cadence for polling the gateway's USB hub-port power via uhubctl so the UI
    # can show/toggle it like the powerbox OUT rails. 0 disables the poll.
    gateway_usb_poll_s: float = 10.0

    # ── MFD video board (Pi Zero 2W on the PPPS hub port) ────────────────────
    # ACC-follower power manager for the VGA666 video board: port power on with
    # ACC, grace period after key-off, clean SSH shutdown, then VBUS cut. All
    # knobs live in backend.mfd_power.MfdPowerConfig and map to BACKEND_MFD_*
    # env vars — see backend.__main__.
    mfd_enabled: bool = False
    mfd_config: Optional[object] = None    # MfdPowerConfig; None -> defaults

    # ── USB hub port power (backend.port_power) ──────────────────────────────
    # Topology of hub port power control: the powerbox's socket has native PPPS
    # (uhubctl); gateway/MFD/SDR VBUS goes through powerbox relays with
    # data-off sequencing and lossy-command verification against the "rly"
    # STATUS telemetry. None -> PortPowerConfig() defaults (current car).
    port_power_config: Optional[object] = None
    # Cadence for the desired-vs-actual relay enforcement sweep. Also converges
    # after a hub reset (which cold-boots the powerbox and releases all relays).
    port_power_enforce_s: float = 15.0

    # ── RS485 satellites (OUT2 rail power management + twin) ─────────────────
    # The satellite subsystem: OUT2 wake-lock power rule (rail on while ACC is
    # on or jobs are pending), the serialized job queue, periodic scheduler and
    # the presence/config-sync supervisor. Requires powerbox_enabled for the
    # physical rail control; without a powerbox link it still tracks presence
    # and runs jobs (rail assumed up).
    satellites_enabled: bool = True
    # Linger before OUT2 is actually dropped after the last wake-lock releases.
    # Absorbs bursts (queued jobs arriving back-to-back) and key-off bounce.
    satellite_linger_s: float = 10.0
    # Re-send cadence while the powerbox out2 mirror disagrees with the rule.
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

    # ── Chassis fan control ──────────────────────────────────────────────────
    # PWM fan on powerbox GPIO 14. Two cooperating controllers, final duty is
    # the max of both:
    #   1. POCO controller — delta between POCO die temp (max CPU/GPU) and the
    #      outside-of-box ambient (BMP2 @0x76; falls back to the in-box AHT20
    #      when BMP2 is absent, matching the historical behaviour).
    #   2. Box-purge controller — delta between box-inside air (BMP1 @0x77,
    #      AHT20 fallback) and outside ambient (BMP2). Purges heat trapped in
    #      the box so passively-cooled boards (MFD Pi Zero 2W, RP2040s) don't
    #      cook even when the POCO itself is idle/cool.
    chassis_fan_enabled: bool = True
    chassis_fan_pin: int = 14             # powerbox GPIO pin driving the fan
    # PWM frequency. The chassis fan is a 2-wire BLDC: its internal commutation
    # electronics lose power in the PWM off-gaps above a few hundred Hz — at
    # 500 Hz it buzzes and barely spins, ≥2 kHz it stalls entirely (coil buzz
    # only), 25 kHz dead silent AND dead still. Low-frequency PWM is the
    # correct drive for it: the rotor+electronics ride through slow gaps.
    # Characterized on-car 2026-08-10: 15 Hz = quietest (no grind, no pulsing,
    # smooth down to 20% duty); 30 Hz good; 100 Hz audible grind.
    chassis_fan_freq: int = 15            # PWM frequency in Hz
    chassis_fan_tick_s: float = 2.0       # control loop cadence (seconds)
    # "full" power-mode profile (ACC on, heavy load)
    fan_full_start_temp: float = 50.0     # start fanning when poco_max > this (°C)
    fan_full_stop_temp: float = 45.0      # stop fanning when poco_max < this (°C)
    fan_full_start_delta: float = 20.0    # AND delta_t > this (°C)
    fan_full_stop_delta: float = 15.0     # OR delta_t < this (°C)
    fan_full_max_pct: float = 100.0       # max duty cycle (%)
    fan_full_ramp_range: float = 30.0     # ramp from 50C to 80C
    # "low" power-mode profile (ACC off, idle)
    fan_low_start_temp: float = 50.0
    fan_low_stop_temp: float = 45.0
    fan_low_start_delta: float = 20.0
    fan_low_stop_delta: float = 15.0
    fan_low_max_pct: float = 100.0        # user requested 100% at 80C
    fan_low_ramp_range: float = 30.0      # ramp from 50C to 80C
    # Safety: absolute POCO temp override (fan at 100% regardless of delta)
    fan_safety_temp: float = 80.0         # °C
    # Fallback: no ambient temp at all — use absolute POCO thresholds
    fan_fallback_start_temp: float = 50.0 # °C
    fan_fallback_stop_temp: float = 45.0  # °C
    # Box-purge controller: run the fan when the box interior is significantly
    # hotter than the cabin (BMP1 inside vs BMP2 outside). Requires BMP2.
    fan_box_start_delta: float = 8.0      # start purging when inside-outside > this (°C)
    fan_box_stop_delta: float = 5.0       # stop when delta falls below this (°C)
    fan_box_min_temp: float = 35.0        # AND box inside > this (°C) — no purge when box is cold
    fan_box_stop_temp: float = 32.0       # release latch when box inside < this (°C)
    fan_box_max_pct: float = 60.0         # purge is a background job — cap the noise
    fan_box_ramp_range: float = 10.0      # ramp duty over delta from stop_delta upward

    # Asymmetric EMA filter for the simulated heatsink temperature. The raw die
    # sensor (max CPU/GPU) spikes fast under load, but the physical heatsink has
    # a much larger thermal mass. Measured on-device (POCO F1 chassis heatsink):
    # heating time constant ~65 s, passive cooldown tail ~60 s. With a 2 s tick,
    # alpha = tick / tau, so ~0.03 makes the EMA track the real heatsink instead
    # of the noisy die temperature.
    fan_ema_alpha_up: float = 0.03        # rise: tau ~= 2s/0.03 ~= 65s (measured ~65s)
    fan_ema_alpha_down: float = 0.03      # fall: tau ~= 2s/0.03 ~= 65s (measured ~60s)

    verbose: bool = False


class BackendService:
    """Own the engine, metrics sink and API server for the headless backend."""

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
        self._satellite_sup_last: float = 0.0
        self.recorder: Optional[TripRecorder] = None
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
        self._start_time: float = time.time()

        # Gateway link staleness watchdog: latched True once heartbeats stop,
        # cleared when they resume (edge-triggered logging).
        self._gw_stale: bool = False

        self._poco_poll_last: float = 0.0

        # Chassis fan controller state.
        self._fan_last_tick: float = 0.0
        self._vfd_clock_last: float = 0.0
        self._fan_active: bool = False      # hysteresis latch
        self._fan_box_active: bool = False  # box-purge hysteresis latch
        self._fan_last_duty: int = -1       # last sent duty (avoid re-sending same value)
        self._fan_last_freq: int = -1       # last sent PWM frequency (Hz)
        self._fan_last_log_pct: float = -1.0  # edge-triggered logging
        self._fan_ema_temp: Optional[float] = None  # Simulated heatsink temp

        # Gateway USB hub-port power telemetry mirror (relay ch4 via port_power).
        self._gateway_usb_target: Optional[str] = None  # stable by-id path of the gateway board
        self._gwusb_last_poll: float = 0.0
        self._gwusb_loc: Optional[str] = None           # cached "HUB PORT" (e.g. "1-1.4 2")

        # Gateway link auto-recovery (cold VBUS cycle via relay ch4).
        self._gw_recover_off_at: Optional[float] = None  # VBUS-off timestamp; None = not mid-cycle
        self._gw_recover_last: float = 0.0               # last cycle start (cooldown)
        self._gw_recover_epoch: float = time.time()      # staleness reference when no HB seen yet
        self._gw_recover_attempts: int = 0               # consecutive cycles without a heartbeat

        # MFD video board power manager (backend.mfd_power).
        self.mfd_power = None

        # USB hub port power controllers (backend.port_power.HubPortPower).
        self.port_power = None
        self._pp_enforce_last: float = 0.0

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

        # RS485 satellites: OUT2 wake-lock power rules + job queue + twin.
        if cfg.satellites_enabled:
            self._wire_satellites(twin)

        # Trip recording: tap ingress/egress and write rotating per-trip logs.
        if cfg.recording.enabled:
            self._wire_recording(twin)

        # (MFD power manager is wired later in build(), after the port power
        # controllers exist — its VBUS goes through powerbox relay ch3.)

        db = MetricsDatabase(cfg.db_path)
        sink = MetricsSink(
            twin.store,
            db,
            sample_interval=cfg.sample_interval,
            rollup_interval=cfg.rollup_interval,
            prune_interval=cfg.prune_interval,
        )

        bridge = StoreBridge(twin.store)
        # Push every state change to API clients (callback runs on the engine
        # thread; the bridge hands snapshots to the asyncio loop safely).
        self._unsubscribe = twin.store.subscribe(StateSlice.ALL, bridge.on_state)
        # Provide the initial state immediately.
        bridge.on_state(twin.store.state)


        api = ApiServer(
            bridge,
            db,
            host=cfg.api_host,
            port=cfg.api_port,
            auth_token=cfg.auth_token,
            log_level="debug" if cfg.verbose else "info",
        )
        
        zmq_srv = ZmqServer(
            bridge=bridge,
            host=cfg.api_host,
            pub_port=8081,
            rep_port=8082,
        )

        # State: subscribe the ZMQ publisher to store updates (the server
        # rate-limits internally). Events: every bridge.push_event envelope is
        # mirrored onto the ZMQ PUB channel via the event sink.
        def _zmq_on_state(state):
            from ..api.serialization import serialize_state
            zmq_srv.enqueue_state(
                {"type": "state", "ts": time.time(), "state": serialize_state(state)}
            )

        self._unsubscribe_zmq = twin.store.subscribe(StateSlice.ALL, _zmq_on_state)
        bridge.add_event_sink(zmq_srv.enqueue_event)

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

    def _wire_powerbox(self, twin: VirtualTwin) -> None:
        """Register powerbox ingress parsers and power-management rules.

        Side effects are injected and dev-safe: the power-mode flag is only
        written if the path is writable, and the powerbox power-off command is
        logged (not sent) until a powerbox output port exists.
        """
        cfg = self.config

        register_powerbox_ingress(twin.ingress)

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
        # commander can actually reach the firmware (shutdown, heartbeat, OUT2/3,
        # power button). None in replay / when no powerbox port exists -> log-only.
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
        )

        # MFD video board (Pi Zero 2W): ACC-follower USB port power manager.
        # Its VBUS goes through powerbox relay ch3 (RelayPortPower handles the
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

        def _powerbox_middleware(action, store) -> None:
            from ..state.actions import ActionSource
            if getattr(action, "source", None) == ActionSource.UI and self.powerbox_commander:
                if type(action).__name__ == "SetOutAction":
                    if action.channel == 2 and self.config.satellites_enabled:
                        # OUT2 is owned by the satellite power rule (wake-locks);
                        # translate a manual UI toggle into a manual hold so the
                        # rule and the operator don't fight over the rail.
                        from ..state.actions import SatellitePowerHoldAction
                        store.dispatch(SatellitePowerHoldAction(
                            "manual:ui", acquire=action.on))
                    else:
                        self.powerbox_commander.set_out(action.channel, action.on)
                elif type(action).__name__ == "SetRelayAction":
                    # Manual USB-port relay control. Route through the port
                    # power controllers (data-off sequencing + enforcement)
                    # where one exists for the channel; raw command otherwise.
                    pp = self.port_power
                    ch = int(action.channel)
                    if pp is not None and ch == pp.sdr.relay_ch:
                        pp.sdr.set(action.on)
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
                elif type(action).__name__ == "SetReadyModeAction":
                    if action.on:
                        self.powerbox_commander.press_button(3000)
                    else:
                        self.powerbox_commander.press_button(10000)
                elif type(action).__name__ == "PowerboxI2cScanAction":
                    self.powerbox_commander.request_i2c_scan()

        twin.store.add_middleware(_powerbox_middleware)

        def _system_middleware(action, store) -> None:
            from ..state.actions import ActionSource
            if getattr(action, "source", None) != ActionSource.UI:
                return
            if type(action).__name__ != "SetGatewayUsbPowerAction":
                return
            # Gateway VBUS goes through powerbox relay ch4 (data-off sequencing
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
            commander.request_power_off(reason=reason, grace_s=cfg.shutdown_grace_s)
            if cfg.local_poweroff_on_undervoltage:
                logger.warning("Triggering local poweroff: %s", reason)
                try:
                    os.system("systemctl poweroff")  # noqa: S605 - controlled command
                except Exception:
                    logger.exception("Local poweroff failed")

        twin.rules_engine.register(
            PowerModeRule(apply_mode=controller.set_for_ignition)
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
        """Wire the RS485 satellite subsystem: power rules, job queue, twin.

        Power model: the OUT2 rail is on iff at least one wake-lock holder is
        held — ``acc`` (ignition on), ``queue`` (jobs pending/running) or
        ``manual:*``. The queue serializes ALL satellite work (scheduled reads,
        event commands, config pushes), so overlapping jobs share one rail
        power-up with no flapping. Dev-safe: without a powerbox link the rail
        command is log-only and jobs run assuming the rail is up.
        """
        cfg = self.config
        store = twin.store

        queue = SatelliteJobQueue(store, output_port=twin.output_port)
        scheduler = SatelliteScheduler(queue, store)

        # Persisted per-satellite options -> auto re-push after satellite boot.
        desired_configs = {}
        try:
            from ..persistence import SettingsManager
            desired_configs = SettingsManager().settings.satellites.desired_configs()
        except Exception:
            logger.exception("Failed to load persisted satellite configs")
        supervisor = SatelliteSupervisor(
            queue, store,
            desired_configs=desired_configs,
            offline_after_s=cfg.satellite_offline_after_s,
        )
        supervisor.seed()

        # Rules: ACC wake-lock + holder-set -> physical OUT2 (via commander).
        def _set_out2(on: bool) -> bool:
            if self.powerbox_commander is None:
                logger.info("OUT2 -> %s (no powerbox commander, log-only)", on)
                return False
            return self.powerbox_commander.set_out(2, on)

        # Gateway (CAN/AVC + RS485 master) is bonded to the same wake-lock
        # logic: needed with ACC (CAN/AVC) and for satellite jobs (RS485).
        # Dispatch drives _system_middleware (uhubctl toggle) and sets the
        # desired state that _gateway_usb_power_tick keeps enforcing.
        def _set_gateway(on: bool) -> None:
            from ..state.actions import SetGatewayUsbPowerAction, ActionSource
            store.dispatch(SetGatewayUsbPowerAction(on, source=ActionSource.UI))

        twin.rules_engine.register(SatelliteAccHoldRule())
        twin.rules_engine.register(SatellitePowerRule(
            set_out2=_set_out2,
            linger_s=cfg.satellite_linger_s,
            retry_s=cfg.satellite_retry_s,
            set_gateway=_set_gateway,
        ))

        # Route EnqueueSatelliteCommandAction (API/events) into the queue.
        def _satellite_middleware(action, _store) -> None:
            if type(action).__name__ == "EnqueueSatelliteCommandAction":
                queue.submit(command_job(
                    action.device_id, action.payload,
                    name=action.name, priority=action.priority,
                ))
        store.add_middleware(_satellite_middleware)

        # Optional periodic key-off sensor wake ("every 5 min read sensors").
        if cfg.satellite_poll_interval_s > 0 and cfg.satellite_poll_devices:
            import json as _json
            from .satellites import PeriodicJobSpec, PRIORITY_SCHEDULED, SatelliteJob

            try:
                poll_payload = _json.loads(cfg.satellite_poll_payload)
            except ValueError:
                logger.error("Bad satellite_poll_payload %r — poll disabled",
                             cfg.satellite_poll_payload)
                poll_payload = None

            if poll_payload is not None:
                devices = tuple(int(d) for d in cfg.satellite_poll_devices)

                def _poll_factory() -> SatelliteJob:
                    def start(ctx) -> None:
                        for dev in devices:
                            ctx.send(dev, dict(poll_payload))
                    return SatelliteJob(
                        name="sched:poll", start=start,
                        priority=PRIORITY_SCHEDULED,
                    )

                scheduler.add(PeriodicJobSpec(
                    name="sched:poll",
                    interval_s=cfg.satellite_poll_interval_s,
                    factory=_poll_factory,
                ))

        self.satellite_queue = queue
        self.satellite_scheduler = scheduler
        self.satellite_supervisor = supervisor
        logger.info(
            "Satellite subsystem wired (linger=%.0fs, offline_after=%.0fs, "
            "poll=%.0fs %s, configs=%s)",
            cfg.satellite_linger_s, cfg.satellite_offline_after_s,
            cfg.satellite_poll_interval_s, cfg.satellite_poll_devices,
            sorted(desired_configs) or "none",
        )

    def _vfd_clock_tick(self) -> None:
        """Sync the VFD satellite's RTC (drives its idle clock screen).

        Sends a "K" time-sync message once a minute; the satellite keeps
        time itself between syncs, and a reboot re-syncs within a minute.
        """
        if self.twin is None:
            return
        now = time.time()
        if now - self._vfd_clock_last < 60.0:
            return
        self._vfd_clock_last = now
        lt = time.localtime(now)
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
        """Advance the satellite queue/scheduler/supervisor (engine loop)."""
        if self.satellite_queue is None:
            return
        self.satellite_queue.tick()
        self.satellite_scheduler.tick()
        now = time.time()
        # Supervisor (presence aging + config sync) needs only ~1 Hz.
        if (now - self._satellite_sup_last) >= 1.0:
            self._satellite_sup_last = now
            self.satellite_supervisor.tick()

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
        if self.recorder is not None:
            self.recorder.start()
        self.sink.start()
        self.api.start()
        self.zmq.start()

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
                loop_start = time.time()
                try:
                    self.twin.update()
                    self.bridge.drain_commands()
                    self._powerbox_heartbeat_tick()
                    self._powerbox_watchdog_tick()
                    self._gateway_watchdog_tick()
                    self._gateway_recover_tick()
                    self._gateway_usb_power_tick()
                    self._port_power_enforce_tick()
                    self._mfd_power_tick()
                    self._poco_power_tick()
                    self._chassis_fan_tick()
                    self._vfd_clock_tick()
                    self._satellite_tick()
                    if self.recorder is not None:
                        self.recorder.tick()
                except Exception:
                    logger.exception("Engine loop iteration failed")
                
                period = 1.0 / max(1.0, self.config.tick_hz)
                elapsed = time.time() - loop_start
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
        now = time.time()
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
        last = pb.last_update_time
        if last <= 0:
            last = self._start_time
        age = time.time() - last
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
        age = time.time() - last
        if age > stale_s:
            if not self._gw_stale:
                self._gw_stale = True
                logger.warning(
                    "Gateway link STALE: no heartbeat for %.1fs (>%.1fs). "
                    "Likely an ACC power-save disconnect; marking disconnected.",
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
        """Auto-recover a wedged gateway by cold-cycling its VBUS relay (ch4).

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
        now = time.time()

        # Phase 2: mid-cycle — re-power once the off window has elapsed.
        if self._gw_recover_off_at is not None:
            if now - self._gw_recover_off_at >= cfg.gateway_recover_off_s:
                self._gw_recover_off_at = None
                logger.warning("Gateway recovery: VBUS back ON (relay ch%d)", gw.relay_ch)
                gw.set(True)
            return

        conn = self.twin.store.state.connection
        last = conn.last_heartbeat_time
        if last:
            if not self._gw_stale:
                return
            age = now - last
        else:
            # No heartbeat ever seen this boot: either the gateway was already
            # wedged before we started (recoverable) or it runs pre-2.28.0
            # firmware with no GW_HB (a cycle is harmless — it reboots into
            # the same silence and the cooldown caps the rate). Measure
            # staleness from backend start.
            age = now - self._gw_recover_epoch
        if age < cfg.gateway_recover_stale_s:
            return
        # A heartbeat after the last cycle proves recovery worked — reset backoff.
        if last and last > self._gw_recover_last:
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
        telemetry (relay ch4 = gateway VBUS) — no uhubctl subprocess polling.
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
        now = time.time()
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
        interval = getattr(cfg, "port_power_enforce_s", 15.0)
        if interval <= 0:
            return
        now = time.time()
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
        now = time.time()
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
        """Intelligent chassis fan controller.

        Drives a PWM fan on powerbox GPIO ``chassis_fan_pin``. Two cooperating
        controllers share the fan; the final duty is the max of both:

        1. POCO: differential between the POCO die (max CPU/GPU, EMA-filtered)
           and the outside-of-box ambient (BMP2 @0x76, AHT20 fallback). Two
           profiles (full/low) selected by the current power mode.
        2. Box purge: differential between box-inside air (BMP1 @0x77, AHT20
           fallback) and outside ambient (BMP2) — cools the passively-cooled
           boards in the box (MFD Pi Zero 2W, RP2040s).

        Both controllers use hysteresis to prevent oscillation.
        """
        cfg = self.config
        if not cfg.chassis_fan_enabled or not cfg.powerbox_enabled:
            return
        if self.powerbox_commander is None:
            return
        now = time.time()
        if now - self._fan_last_tick < cfg.chassis_fan_tick_s:
            return
        self._fan_last_tick = now

        if self.twin is None:
            return
        pb = self.twin.store.state.powerbox

        # Manual override: pin the fan to a fixed duty regardless of temperature.
        # Set via the `set_fan` API command (SetFanOverrideAction); cleared with
        # `fan_auto`. Bypasses the automatic controller entirely. An optional
        # frequency override rides along for driver/noise characterization.
        override = getattr(pb, "fan_override_pct", None)
        if override is not None:
            pct = max(0.0, min(100.0, float(override)))
            self._set_fan_duty(int(pct / 100.0 * 65535),
                               freq=getattr(pb, "fan_override_freq", None))
            temps = [t for t in (pb.poco_core_temp, pb.poco_gpu_temp) if t is not None]
            if temps:
                self._publish_poco_ema_temp(max(temps), pct)
            return

        if not pb.connected:
            return

        # ── Controller 1: POCO die vs outside ambient ────────────────────────
        # Ambient reference is the outside-of-box BMP2 (@0x76); fall back to
        # the in-box AHT20 (historical behaviour) when BMP2 is absent.
        ambient_temp = pb.bmp2_t if pb.bmp2_t is not None else pb.aht_t

        poco_duty_pct = 0.0
        poco_temps = [t for t in (pb.poco_core_temp, pb.poco_gpu_temp) if t is not None]
        if poco_temps:
            poco_max_raw = max(poco_temps)

            # Safety override: absolute raw temperature too high (ignore EMA delay).
            if poco_max_raw >= cfg.fan_safety_temp:
                self._set_fan_duty(65535)  # 100%
                self._fan_ema_temp = poco_max_raw  # Keep EMA updated
                self._publish_poco_ema_temp(self._fan_ema_temp, 100.0)
                return

            # Asymmetric EMA (Simulated Heatsink Temperature)
            if self._fan_ema_temp is None:
                self._fan_ema_temp = poco_max_raw
            else:
                if poco_max_raw > self._fan_ema_temp:
                    self._fan_ema_temp += cfg.fan_ema_alpha_up * (poco_max_raw - self._fan_ema_temp)
                else:
                    self._fan_ema_temp += cfg.fan_ema_alpha_down * (poco_max_raw - self._fan_ema_temp)

            poco_max = self._fan_ema_temp

            # Select profile based on power mode.
            is_full = pb.power_mode == "full"
            if is_full:
                start_temp = cfg.fan_full_start_temp
                stop_temp = cfg.fan_full_stop_temp
                start_delta = cfg.fan_full_start_delta
                stop_delta = cfg.fan_full_stop_delta
                max_pct = cfg.fan_full_max_pct
                ramp_range = cfg.fan_full_ramp_range
            else:
                start_temp = cfg.fan_low_start_temp
                stop_temp = cfg.fan_low_stop_temp
                start_delta = cfg.fan_low_start_delta
                stop_delta = cfg.fan_low_stop_delta
                max_pct = cfg.fan_low_max_pct
                ramp_range = cfg.fan_low_ramp_range

            if ambient_temp is not None:
                # Normal mode: differential + absolute-based.
                delta_t = poco_max - ambient_temp
            else:
                # Fallback: no ambient sensor — absolute POCO temp, ignore delta constraints
                delta_t = 999.0
                start_delta = 0.0
                stop_delta = 0.0
                start_temp = cfg.fan_fallback_start_temp
                stop_temp = cfg.fan_fallback_stop_temp

            # Hysteresis: once active, stay active until temp/delta drops below stop thresholds.
            if self._fan_active:
                if poco_max < stop_temp or delta_t < stop_delta:
                    self._fan_active = False
            else:
                if poco_max > start_temp and delta_t >= start_delta:
                    self._fan_active = True

            if self._fan_active:
                # Linear ramp from start_temp to start_temp+ramp_range based on absolute temp.
                t = (poco_max - start_temp) / max(ramp_range, 0.1)
                poco_duty_pct = max(0.0, min(max_pct, t * max_pct))
        else:
            # No POCO thermal data — the POCO controller stands down, but the
            # box-purge controller below can still run the fan.
            self._fan_ema_temp = None
            self._fan_active = False

        # ── Controller 2: box purge (inside vs outside the box) ─────────────
        # Protects passively-cooled hardware inside the box (MFD Pi Zero 2W,
        # RP2040s) when the box air runs hot relative to the cabin.
        box_duty_pct = 0.0
        box_inside = pb.bmp_t if pb.bmp_t is not None else pb.aht_t
        box_outside = pb.bmp2_t
        box_delta: Optional[float] = None
        if box_inside is not None and box_outside is not None:
            box_delta = box_inside - box_outside
            if self._fan_box_active:
                if box_delta < cfg.fan_box_stop_delta or box_inside < cfg.fan_box_stop_temp:
                    self._fan_box_active = False
            else:
                if box_delta >= cfg.fan_box_start_delta and box_inside >= cfg.fan_box_min_temp:
                    self._fan_box_active = True
            if self._fan_box_active:
                t = (box_delta - cfg.fan_box_stop_delta) / max(cfg.fan_box_ramp_range, 0.1)
                box_duty_pct = max(0.0, min(cfg.fan_box_max_pct, t * cfg.fan_box_max_pct))
        else:
            self._fan_box_active = False

        # ── Merge: the fan serves whichever controller wants more airflow ───
        duty_pct = max(poco_duty_pct, box_duty_pct)
        # Minimum duty when active: 15% (fan needs a minimum to spin up).
        if 0.0 < duty_pct < 15.0:
            duty_pct = 15.0
        duty_raw = int(duty_pct / 100.0 * 65535)
        self._set_fan_duty(duty_raw)

        # Publish duty (and EMA temp when available) for dashboard visibility.
        if self.twin and self.twin.store:
            from ..state.actions import SetPocoTelemetryAction
            self.twin.store.dispatch(SetPocoTelemetryAction(
                fan_duty_pct=duty_pct,
                poco_ema_temp=self._fan_ema_temp
            ))

    def _publish_poco_ema_temp(
        self, ema_temp: Optional[float], fan_duty_pct: Optional[float] = None
    ) -> None:
        """Publish the simulated heatsink (EMA) temperature to the store.

        Called every fan tick regardless of fan state so the dashboard's
        "Simulated Temp" always reflects the latest value.
        """
        if ema_temp is None or not (self.twin and self.twin.store):
            return
        from ..state.actions import SetPocoTelemetryAction
        self.twin.store.dispatch(SetPocoTelemetryAction(
            poco_ema_temp=ema_temp,
            fan_duty_pct=fan_duty_pct,
        ))

    def _set_fan_duty(self, duty_raw: int, freq: Optional[int] = None) -> None:
        """Send the fan duty to the powerbox, de-duplicating unchanged values.

        ``freq`` overrides the configured PWM frequency (manual tuning); a
        frequency change always forces a re-send even if the duty is unchanged.
        """
        eff_freq = int(freq) if freq else self.config.chassis_fan_freq
        freq_changed = eff_freq != self._fan_last_freq
        self._fan_last_freq = eff_freq
        # Add a 1% (approx 655 units) deadband to prevent serial spam from EMA noise.
        # Always send if turning exactly ON or exactly OFF, or on a freq change.
        if self._fan_last_duty != -1 and not freq_changed:
            if abs(duty_raw - self._fan_last_duty) < 655 and (duty_raw == 0) == (self._fan_last_duty == 0):
                return
                
        self._fan_last_duty = duty_raw
        duty_pct = round(duty_raw / 65535.0 * 100.0, 1)
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
            self.powerbox_commander.set_fan(self.config.chassis_fan_pin, duty_raw, eff_freq)

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
            
        now = time.time()
        if (now - self._pb_recover_last) < cfg.powerbox_recover_cooldown_s:
            return
        self._pb_recover_last = now
        if self._pb_recover_attempts < 0:
            self._pb_recover_attempts = 0
        self._pb_recover_attempts += 1
        logger.warning(
            "Powerbox auto-recovery: forcing link reset (attempt #%d, link "
            "stale %.1fs). Ladder: USBDEVFS_RESET first (no MCU reboot, relays "
            "kept), parent-hub reset on escalation.",
            self._pb_recover_attempts, age,
        )
        try:
            self._powerbox_serial.force_reconnect(attempt=self._pb_recover_attempts)
            self._fan_last_duty = -1
        except Exception:
            logger.exception("Powerbox force_reconnect failed")

    def _shutdown(self) -> None:
        logger.info("Shutting down backend service")
        if self._usb_monitor is not None:
            try:
                self._usb_monitor.stop()
            except Exception:
                logger.exception("Error stopping USB monitor")
        if self.api is not None:
            self.api.stop()
        if self.zmq:
            self.zmq.stop()
        if self.recorder is not None:
            try:
                self.recorder.stop()
            except Exception:
                logger.exception("Error stopping trip recorder")
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
        if self.twin is not None:
            try:
                self.twin.stop()
            except Exception:
                logger.exception("Error stopping virtual twin")
        logger.info("Backend service stopped")
