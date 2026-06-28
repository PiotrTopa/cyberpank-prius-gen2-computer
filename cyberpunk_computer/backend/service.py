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
    ExecutionMode,
    MultiInputPort,
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
from ..api import ApiServer, StoreBridge

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
    # hub ports (powerbox=port 1, gateway=port 2), so roles are resolved purely
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
    # None to use discovery.DEFAULT_PORT_ROLES ({1: powerbox, 2: gateway}).
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
    powerbox_stale_s: float = 6.0
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
        self.power_controller: Optional[PriusPowerController] = None
        self.powerbox_commander: Optional[PowerboxCommander] = None
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

        # Trip recording: tap ingress/egress and write rotating per-trip logs.
        if cfg.recording.enabled:
            self._wire_recording(twin)

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

        self.twin = twin
        self.db = db
        self.sink = sink
        self.bridge = bridge
        self.api = api

    def _wire_powerbox(self, twin: VirtualTwin) -> None:
        """Register powerbox ingress parsers and power-management rules.

        Side effects are injected and dev-safe: the power-mode flag is only
        written if the path is writable, and the powerbox power-off command is
        logged (not sent) until a powerbox output port exists.
        """
        cfg = self.config

        register_powerbox_ingress(twin.ingress)

        controller = PriusPowerController(flag_path=cfg.power_mode_flag)
        # Wire the powerbox's own serial link as the command output port so the
        # commander can actually reach the firmware (shutdown, heartbeat, OUT2/3,
        # power button). None in replay / when no powerbox port exists -> log-only.
        commander = PowerboxCommander(output_port=self._powerbox_serial)
        self.power_controller = controller
        self.powerbox_commander = commander

        def _powerbox_middleware(action, store) -> None:
            from ..state.actions import ActionSource
            if getattr(action, "source", None) == ActionSource.UI and self.powerbox_commander:
                if type(action).__name__ == "SetOutAction":
                    self.powerbox_commander.set_out(action.channel, action.on)
                elif type(action).__name__ == "SetReadyModeAction":
                    if action.on:
                        self.powerbox_commander.press_button(3000)
                    else:
                        self.powerbox_commander.press_button(10000)

        twin.store.add_middleware(_powerbox_middleware)

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
        twin.rules_engine.register(
            UndervoltageProtectionRule(
                request_shutdown=request_shutdown,
                threshold=cfg.undervoltage_threshold,
                recover_threshold=cfg.undervoltage_recover,
                confirm_seconds=cfg.undervoltage_confirm_s,
                grace_seconds=cfg.shutdown_grace_s,
            )
        )
        logger.info(
            "Powerbox computer-side wired (undervoltage<%.1fV, flag=%s)",
            cfg.undervoltage_threshold, cfg.power_mode_flag,
        )

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
                    self._poco_power_tick()
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

    def _poco_power_tick(self) -> None:
        """Poll POCO's internal battery telemetry from sysfs (~1 Hz)."""
        now = time.time()
        if now - self._poco_poll_last < 1.0:
            return
        self._poco_poll_last = now
        try:
            with open("/sys/class/power_supply/qcom-battery/voltage_now", "r") as f:
                v_now = float(f.read().strip()) / 1_000_000.0  # uV to V
            with open("/sys/class/power_supply/qcom-battery/current_now", "r") as f:
                i_now = float(f.read().strip()) / 1_000_000.0  # uA to A
            # Calculate power (W). Current might be negative when charging/discharging.
            # We want absolute power consumption, or just power flow (negative = charging battery).
            # The POCO power stack is mostly stable ~4V. We'll use absolute to represent the power magnitude,
            # or just let it be signed so user sees charging vs discharging.
            power_w = v_now * i_now
            if self.twin and self.twin.store:
                from ..state.actions import SetPocoTelemetryAction
                self.twin.store.dispatch(SetPocoTelemetryAction(poco_power_w=abs(power_w)))
        except (OSError, ValueError):
            pass

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
            "Powerbox auto-recovery: forcing serial reset (attempt #%d, link "
            "stale %.1fs). DTR toggle will reset the MCU to clear the wedge.",
            self._pb_recover_attempts, age,
        )
        try:
            self._powerbox_serial.force_reconnect(attempt=self._pb_recover_attempts)
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
