"""
Headless backend entry point.

    python -m cyberpunk_computer.backend [options]

Runs the engine + metrics + network API with no UI. The pygame frontend connects
over the network API (see cyberpunk_computer.api).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal

from .service import BackendConfig, BackendService
from ..io import RecordingConfig, RotationPolicy


def parse_port_roles(roles_str: str | None) -> dict[int, str] | None:
    if not roles_str:
        return None
    roles = {}
    for part in roles_str.split(","):
        if ":" in part:
            port, role = part.split(":", 1)
            roles[int(port.strip())] = role.strip()
    return roles


def _build_recording(args: argparse.Namespace) -> RecordingConfig:
    include = {tok.strip().lower() for tok in (args.record_include or "").split(",") if tok.strip()}
    if not include or "all" in include:
        inc_system = inc_can = inc_avc = inc_sat = inc_pbox = inc_out = True
    else:
        inc_system = "system" in include
        inc_can = "can" in include
        inc_avc = "avc" in include
        inc_sat = "satellite" in include or "sat" in include
        inc_pbox = "powerbox" in include or "pbox" in include
        inc_out = "outgoing" in include or "out" in include
    return RecordingConfig(
        enabled=args.record,
        directory=args.record_dir,
        segmentation=args.record_segmentation,
        idle_timeout_s=args.record_idle_timeout,
        min_trip_seconds=args.record_min_trip,
        max_file_mb=args.record_max_file_mb,
        use_ignition=not args.record_no_ignition,
        include_system=inc_system,
        include_can=inc_can,
        include_avc=inc_avc,
        include_satellite=inc_sat,
        include_powerbox=inc_pbox,
        include_outgoing=inc_out,
        rotation=RotationPolicy(
            max_files=args.record_max_files,
            max_total_mb=args.record_max_total_mb,
            max_age_days=args.record_max_age_days,
        ),
    )


def _build_config(args: argparse.Namespace) -> BackendConfig:
    return BackendConfig(
        gateway_port=args.gateway_port,
        powerbox_port=args.powerbox_port,
        auto_discover=not args.no_auto_discover,
        usb_hotplug=not args.no_usb_hotplug,
        usb_hub=args.usb_hub or os.environ.get("BACKEND_HUB"),
        usb_port_roles=parse_port_roles(args.usb_port_roles or os.environ.get("BACKEND_USB_PORT_ROLES")),
        serial_baudrate=args.baudrate,
        api_host=args.api_host,
        api_port=args.api_port,
        auth_token=args.auth_token or os.environ.get("BACKEND_AUTH_TOKEN"),
        db_path=args.db,
        tick_hz=args.tick_hz,
        replay_file=args.replay,
        replay_speed=args.replay_speed,
        replay_loop=args.replay_loop,
        recording=_build_recording(args),
        powerbox_enabled=not args.no_powerbox,
        power_mode_flag=args.power_mode_flag,
        undervoltage_threshold=args.undervoltage_threshold,
        undervoltage_recover=args.undervoltage_recover,
        undervoltage_confirm_s=args.undervoltage_confirm,
        shutdown_grace_s=args.shutdown_grace,
        local_poweroff_on_undervoltage=args.local_poweroff,
        powerbox_auto_recover=args.powerbox_auto_recover,
        chassis_fan_freq=args.chassis_fan_freq,
        fan_full_start_delta=args.fan_full_start_delta,
        fan_full_stop_delta=args.fan_full_stop_delta,
        fan_full_max_pct=args.fan_full_max_pct,
        fan_full_ramp_range=args.fan_full_ramp_range,
        fan_safety_temp=args.fan_safety_temp,
        fan_ema_alpha_up=args.fan_ema_alpha_up,
        fan_ema_alpha_down=args.fan_ema_alpha_down,
        verbose=args.verbose,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cyberpunk_computer.backend",
        description="Headless CyberPunk Prius backend (engine + metrics + API).",
    )
    parser.add_argument("--gateway-port", default="/dev/ttyACM0", help="gateway serial port")
    parser.add_argument(
        "--powerbox-port", default=None, help="powerbox serial port (optional)"
    )
    parser.add_argument(
        "--no-auto-discover",
        action="store_true",
        help="disable USB role auto-discovery; use the explicit --gateway-port/"
             "--powerbox-port paths instead",
    )
    parser.add_argument(
        "--no-usb-hotplug",
        action="store_true",
        help="disable the background USB hotplug monitor",
    )
    parser.add_argument(
        "--usb-hub",
        default=None,
        help="restrict discovery to a specific USB hub (e.g. 1-1). Or use BACKEND_HUB.",
    )
    parser.add_argument(
        "--usb-port-roles",
        default=None,
        help="comma-separated port:role mapping (e.g. 1:powerbox,2:gateway). Or use BACKEND_USB_PORT_ROLES.",
    )
    parser.add_argument("--baudrate", type=int, default=1_000_000, help="serial baudrate")
    parser.add_argument("--api-host", default="0.0.0.0", help="API bind host")
    parser.add_argument("--api-port", type=int, default=8080, help="API bind port")
    parser.add_argument(
        "--auth-token",
        default=None,
        help="bearer token for the API (or set BACKEND_AUTH_TOKEN)",
    )
    parser.add_argument("--db", default="data/metrics.db", help="metrics SQLite path")
    parser.add_argument("--tick-hz", type=float, default=50.0, help="engine loop rate")
    parser.add_argument(
        "--chassis-fan-freq", type=int, default=25000, help="PWM frequency for the chassis fan (Hz)"
    )

    # Replay (run off a recorded NDJSON log instead of the live gateway)
    replay = parser.add_argument_group("replay")
    replay.add_argument(
        "--replay", default=None,
        help="replay a recorded NDJSON log instead of opening the gateway serial port",
    )
    replay.add_argument(
        "--replay-speed", type=float, default=1.0,
        help="replay speed multiplier (1.0 realtime, 0 as-fast-as-possible)",
    )
    replay.add_argument(
        "--replay-loop", action="store_true", help="loop the replay file",
    )

    # Trip recording (rotating per-trip NDJSON logs)
    rec = parser.add_argument_group("recording")
    rec.add_argument(
        "--record", action="store_true",
        help="record traffic to rotating per-trip NDJSON logs",
    )
    rec.add_argument(
        "--record-dir", default="logs/trips", help="directory for trip logs",
    )
    rec.add_argument(
        "--record-segmentation", default="trip",
        choices=["trip", "session", "continuous"],
        help="how to split logs into files",
    )
    rec.add_argument(
        "--record-include", default="all",
        help="comma list of what to log: all|system,can,avc,satellite,powerbox,outgoing",
    )
    rec.add_argument(
        "--record-idle-timeout", type=float, default=120.0,
        help="seconds of silence that ends a trip (trip mode)",
    )
    rec.add_argument(
        "--record-min-trip", type=float, default=15.0,
        help="discard trips shorter than this many seconds",
    )
    rec.add_argument(
        "--record-max-file-mb", type=float, default=64.0,
        help="split the current file at this size (0 = no split)",
    )
    rec.add_argument(
        "--record-no-ignition", action="store_true",
        help="do not use powerbox ACC to bound trips (idle gaps only)",
    )
    rec.add_argument(
        "--record-max-files", type=int, default=60,
        help="rotation: keep at most N trip files (0 = unlimited)",
    )
    rec.add_argument(
        "--record-max-total-mb", type=float, default=512.0,
        help="rotation: cap total size of the trip dir in MB (0 = unlimited)",
    )
    rec.add_argument(
        "--record-max-age-days", type=float, default=30.0,
        help="rotation: delete trip files older than N days (0 = unlimited)",
    )

    parser.add_argument(
        "--no-powerbox",
        action="store_true",
        help="disable powerbox computer-side (power mode + under-voltage rules)",
    )
    parser.add_argument(
        "--power-mode-flag",
        default="/etc/prius/power-mode",
        help="flag file the prius-power unit watches (full/low)",
    )
    parser.add_argument(
        "--undervoltage-threshold",
        type=float,
        default=11.0,
        help="12V trip threshold in volts",
    )
    parser.add_argument(
        "--undervoltage-recover",
        type=float,
        default=11.5,
        help="12V recovery threshold in volts (hysteresis)",
    )
    parser.add_argument(
        "--undervoltage-confirm",
        type=float,
        default=5.0,
        help="seconds below threshold before tripping",
    )
    parser.add_argument(
        "--shutdown-grace",
        type=int,
        default=30,
        help="grace seconds given to the OS before the powerbox cuts the rail",
    )
    parser.add_argument(
        "--local-poweroff",
        action="store_true",
        help="also run 'systemctl poweroff' locally when under-voltage trips",
    )
    parser.add_argument(
        "--powerbox-auto-recover",
        action="store_true",
        help="enable powerbox auto-recovery (force MCU reset via DTR toggle)",
    )
    
    # Chassis Fan Configuration
    fan = parser.add_argument_group("chassis fan")
    fan.add_argument("--fan-full-start-delta", type=float, default=5.0, help="Delta T (°C) to start fan (full mode)")
    fan.add_argument("--fan-full-stop-delta", type=float, default=3.0, help="Delta T (°C) to stop fan (full mode)")
    fan.add_argument("--fan-full-max-pct", type=float, default=100.0, help="Max fan duty pct (full mode)")
    fan.add_argument("--fan-full-ramp-range", type=float, default=10.0, help="Delta T range to ramp from 0 to max (full mode)")
    fan.add_argument("--fan-safety-temp", type=float, default=70.0, help="Absolute POCO temp to override and force 100% duty (°C)")
    fan.add_argument("--fan-ema-alpha-up", type=float, default=0.1, help="EMA filter fast-rise coefficient (default 0.1)")
    fan.add_argument("--fan-ema-alpha-down", type=float, default=0.01, help="EMA filter slow-decay coefficient (default 0.01)")

    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    service = BackendService(_build_config(args))

    def _handle_signal(signum, _frame):
        logging.getLogger(__name__).info("Received signal %s, stopping", signum)
        service.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    service.run()


if __name__ == "__main__":
    main()
