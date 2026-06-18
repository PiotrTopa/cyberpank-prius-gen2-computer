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
from ..metrics import MetricsDatabase, MetricsSink
from ..state.store import StateSlice
from ..state.rules.power_management import (
    PowerModeRule,
    UndervoltageProtectionRule,
)
from ..api import ApiServer, StoreBridge

logger = logging.getLogger(__name__)


@dataclass
class BackendConfig:
    """Runtime configuration for the backend service."""

    gateway_port: str = "/dev/ttyACM0"
    powerbox_port: Optional[str] = None  # second RP2040; None until firmware exists
    serial_baudrate: int = 1_000_000

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
    # Whether a tripped under-voltage also powers the POCO off locally. Off by
    # default; the powerbox is expected to cut the rail. Enable on the target.
    local_poweroff_on_undervoltage: bool = False

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
            twin = create_virtual_twin(
                VirtualTwinConfig(
                    mode=ExecutionMode.PRODUCTION,
                    serial_port=cfg.gateway_port,
                    serial_baudrate=cfg.serial_baudrate,
                    verbose=cfg.verbose,
                )
            )

        # Optional powerbox seam: merge a second serial device into the ingress,
        # offset into the reserved DEVICE_POWERBOX_BASE id range. Disabled until a
        # powerbox port is provided (firmware/protocol are future work). Not used
        # in replay mode (the powerbox stream, if any, is already in the log).
        if cfg.powerbox_port and not cfg.replay_file:
            powerbox = SerialPort(
                SerialConfig(port=cfg.powerbox_port, baudrate=cfg.serial_baudrate)
            )
            merged = MultiInputPort(
                [(twin.input_port, 0), (powerbox, DEVICE_POWERBOX_BASE)]
            )
            twin.ingress.set_input_port(merged)
            twin.input_port = merged
            logger.info("Powerbox port enabled on %s", cfg.powerbox_port)

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
        # No powerbox output port yet (firmware pending) -> log-only commander.
        commander = PowerboxCommander(output_port=None)
        self.power_controller = controller
        self.powerbox_commander = commander

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

        period = 1.0 / max(1.0, self.config.tick_hz)
        try:
            while not self._stop.is_set():
                loop_start = time.time()
                try:
                    self.twin.update()
                    self.bridge.drain_commands()
                    if self.recorder is not None:
                        self.recorder.tick()
                except Exception:
                    logger.exception("Engine loop iteration failed")
                elapsed = time.time() - loop_start
                if elapsed < period:
                    self._stop.wait(period - elapsed)
        finally:
            self._shutdown()

    def stop(self) -> None:
        """Signal the engine loop to exit (safe to call from any thread)."""
        self._stop.set()

    def _shutdown(self) -> None:
        logger.info("Shutting down backend service")
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
