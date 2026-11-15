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


def _build_mfd():
    """MFD video-board power manager config from BACKEND_MFD_* env vars.

    Returns ``(enabled, MfdPowerConfig | None)``. Everything is env-driven so
    moving the board to another hub port / address is a config-file edit:

        BACKEND_MFD_ENABLED=1
        BACKEND_MFD_HUB=1-1               # uhubctl hub location
        BACKEND_MFD_PORT=5                # PPPS (switchable VBUS) port
        BACKEND_MFD_IFACE=usb0            # gadget netdev on the host
        BACKEND_MFD_HOST_CIDR=192.168.100.1/24
        BACKEND_MFD_IP=192.168.100.2      # the Pi's static IP
        BACKEND_MFD_SSH_USER=piotr
        BACKEND_MFD_GRACE_S=1200          # key-off grace before shutdown
        BACKEND_MFD_BOOT_TIMEOUT_S=180
        BACKEND_MFD_SHUTDOWN_WAIT_S=45
        BACKEND_MFD_BEAN_WAKE=entry       # early start from the BEAN bus:
                                          # entry (unlocked / door open) | any | off
        BACKEND_MFD_DARK_GRACE=1          # dark standby on the Pi during the grace
    """
    enabled = os.environ.get("BACKEND_MFD_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if not enabled:
        return False, None
    from .mfd_power import MfdPowerConfig
    env = os.environ.get
    return True, MfdPowerConfig(
        hub=env("BACKEND_MFD_HUB", "1-1"),
        port=int(env("BACKEND_MFD_PORT", "5")),
        iface=env("BACKEND_MFD_IFACE", "usb0"),
        host_cidr=env("BACKEND_MFD_HOST_CIDR", "192.168.100.1/24"),
        board_ip=env("BACKEND_MFD_IP", "192.168.100.2"),
        ssh_user=env("BACKEND_MFD_SSH_USER", "piotr"),
        grace_s=float(env("BACKEND_MFD_GRACE_S", "1200")),
        dark_grace=env("BACKEND_MFD_DARK_GRACE", "1").strip().lower() in ("1", "true", "yes", "on"),
        boot_timeout_s=float(env("BACKEND_MFD_BOOT_TIMEOUT_S", "180")),
        shutdown_wait_s=float(env("BACKEND_MFD_SHUTDOWN_WAIT_S", "45")),
        bean_wake=env("BACKEND_MFD_BEAN_WAKE", "entry").strip().lower(),
    )


def _build_roadsense():
    """roadsense stack manager config from BACKEND_ROADSENSE_* env vars.

    Returns ``(enabled, RoadsenseConfig | None)``. Single-computer layout: the
    stack runs on this POCO under ``roadsense.target`` (roadsense +
    roadsense-signs reference deployment) and follows ACC::

        BACKEND_ROADSENSE_ENABLED=1
        BACKEND_ROADSENSE_TARGET=roadsense.target
        BACKEND_ROADSENSE_UNITS=roadsense-camerad-dvr.service,roadsense-modeld.service,...
        BACKEND_ROADSENSE_API_HOST=127.0.0.1
        BACKEND_ROADSENSE_API_PORT=8090      # ROADSENSE_API_PORT on the phone (8080 is ours)
        BACKEND_ROADSENSE_CAMERA_RELAY=1     # power the camera hub (relay ch4) with the stack
        BACKEND_ROADSENSE_CAMERA_SETTLE_S=8
        BACKEND_ROADSENSE_GRACE_S=30         # key-off grace before the stop
        BACKEND_ACC_OFF_DEBOUNCE_S=3         # hold an ACC-off this long before anything acts on it
        BACKEND_ROADSENSE_START_TIMEOUT_S=120
        BACKEND_ROADSENSE_STOP_TIMEOUT_S=60
        BACKEND_ROADSENSE_API_DEAD_RESTART_S=180   # 0 = never auto-restart
        BACKEND_ROADSENSE_MODE=auto          # auto | on | off (initial operator mode)
    """
    enabled = os.environ.get("BACKEND_ROADSENSE_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on")
    if not enabled:
        return False, None
    from .roadsense_power import DEFAULT_UNITS, RoadsenseConfig
    env = os.environ.get
    units_raw = (env("BACKEND_ROADSENSE_UNITS") or "").strip()
    units = tuple(u.strip() for u in units_raw.split(",") if u.strip()) or DEFAULT_UNITS
    return True, RoadsenseConfig(
        target=env("BACKEND_ROADSENSE_TARGET", "roadsense.target"),
        units=units,
        api_host=env("BACKEND_ROADSENSE_API_HOST", "127.0.0.1"),
        api_port=int(env("BACKEND_ROADSENSE_API_PORT", "8090")),
        camera_relay=env("BACKEND_ROADSENSE_CAMERA_RELAY", "1").strip().lower() in (
            "1", "true", "yes", "on"),
        camera_settle_s=float(env("BACKEND_ROADSENSE_CAMERA_SETTLE_S", "8")),
        grace_s=float(env("BACKEND_ROADSENSE_GRACE_S", "30")),
        start_timeout_s=float(env("BACKEND_ROADSENSE_START_TIMEOUT_S", "120")),
        stop_timeout_s=float(env("BACKEND_ROADSENSE_STOP_TIMEOUT_S", "60")),
        api_dead_restart_s=float(env("BACKEND_ROADSENSE_API_DEAD_RESTART_S", "180")),
        mode=env("BACKEND_ROADSENSE_MODE", "auto").strip().lower() or "auto",
    )


def _build_roadsense_scene():
    """Road scene feed for the MFD (backend.roadsense_scene) from env vars::

        BACKEND_ROADSENSE_SCENE=1          # 0 = no roadsense_scene events (needs BACKEND_ROADSENSE_ENABLED)
        BACKEND_ROADSENSE_SCENE_HZ=10      # publish cap, 5..10 per the MFD contract
    """
    from .roadsense_scene import SceneFeedConfig
    env = os.environ.get
    return SceneFeedConfig(
        enabled=_env_flag("BACKEND_ROADSENSE_SCENE", "1"),
        max_hz=max(1.0, min(10.0, float(env("BACKEND_ROADSENSE_SCENE_HZ") or "10"))),
    )


def _env_flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or "").strip().lower() in ("1", "true", "yes", "on")


def _build_port_power():
    """Hub socket topology from env (None -> PortPowerConfig defaults)::

        BACKEND_HUB=1-1                  # main hub location (also restricts discovery)
        BACKEND_PP_POWERBOX_PORT=5       # main-hub port of the powerbox (native PPPS)
        BACKEND_PP_GATEWAY_PORT=2        # main-hub port of the CAN hub (relay ch3)
        BACKEND_PP_MFD_PORT=3            # main-hub port of the MFD Pi (relay ch2)
        BACKEND_PP_CAMERA_PORT=4         # main-hub port of the camera hub (relay ch4)

    Only keys that are set override the defaults; nothing set -> ``None``.
    """
    keys = {
        "powerbox_port": "BACKEND_PP_POWERBOX_PORT",
        "gateway_data_port": "BACKEND_PP_GATEWAY_PORT",
        "mfd_data_port": "BACKEND_PP_MFD_PORT",
        "camera_data_port": "BACKEND_PP_CAMERA_PORT",
    }
    kw = {}
    for field_name, var in keys.items():
        raw = (os.environ.get(var) or "").strip()
        if raw:
            kw[field_name] = int(raw)
    hub = (os.environ.get("BACKEND_HUB") or "").strip()
    if not kw and not hub:
        return None
    from .port_power import PortPowerConfig
    if hub:
        kw["hub"] = hub
    return PortPowerConfig(**kw)


def _build_bean() -> dict:
    """BEAN capture log location (env BACKEND_BEAN_CAPTURE_DIR, default
    logs/bean under the working directory, next to logs/trips)."""
    d = (os.environ.get("BACKEND_BEAN_CAPTURE_DIR") or "").strip()
    return {"bean_capture_dir": d} if d else {}


def _build_dashcam() -> dict:
    """Dashcam vision config from env (io.dashcam_ingest; disabled by default):

        BACKEND_DASHCAM_URL=rtsp://192.168.0.1/liveRTSP/av4   # empty = off
        BACKEND_DASHCAM_INTERVAL_S=1
        BACKEND_DASHCAM_SOURCE=stream     # stream (one ffmpeg) | grab (session per frame)
        BACKEND_HOME_LATLON=54.35,18.65   # sun position until a GPS fix exists
    """
    from ..io.dashcam_ingest import parse_latlon
    try:
        interval = float(os.environ.get("BACKEND_DASHCAM_INTERVAL_S") or 1.0)
    except ValueError:
        interval = 1.0
    return dict(
        dashcam_url=(os.environ.get("BACKEND_DASHCAM_URL") or "").strip() or None,
        dashcam_interval_s=interval,
        dashcam_source=(os.environ.get("BACKEND_DASHCAM_SOURCE") or "stream").strip().lower(),
        home_latlon=parse_latlon(os.environ.get("BACKEND_HOME_LATLON")),
    )


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


def _build_fan_policy(args: argparse.Namespace):
    """Chassis fan policy: the defaults live in backend/fan_policy.py (derived
    in docs/THERMAL_MODEL.md); each --fan-* flag overrides one number."""
    from dataclasses import replace
    from .fan_policy import FanPolicyConfig
    cfg = FanPolicyConfig()

    def pick(prefix):
        keys = ("on", "off", "full", "min_pct", "max_pct")
        names = ("on_c", "off_c", "full_c", "min_pct", "max_pct")
        vals = ((n, getattr(args, "fan_%s_%s" % (prefix, k))) for n, k in zip(names, keys))
        return {n: v for n, v in vals if v is not None}

    top = {k: v for k, v in (("safety_ctrl_c", args.fan_safety_temp), ("tau_s", args.fan_tau))
           if v is not None}
    return replace(cfg, full=replace(cfg.full, **pick("full")), low=replace(cfg.low, **pick("low")), **top)


def _build_satellites() -> dict:
    """Multi-transport satellite config from env (docs/SATELLITES.md):

        BACKEND_SATELLITE_TRANSPORTS=rs485,ble,wifi   # comma list (default all three)
        BACKEND_SATELLITE_REDISCOVER_S=300
        BACKEND_SATELLITE_DISCOVERY_TTL_S=600
        BACKEND_SATELLITE_KEYCHAIN_DIR=/etc/prius/satellites   # read by the keychain module
        BACKEND_SATELLITE_WIFI_BIND=0.0.0.0
        BACKEND_SATELLITE_WIFI_IFACE=wlan0            # empty = always available (dev)
        BACKEND_SATELLITE_WIFI_AP_ADDR=10.42.0.1
        BACKEND_SATELLITE_BLE_SOCKET=/run/prius/bled.sock
    """
    env = os.environ.get
    out: dict = {}
    raw = env("BACKEND_SATELLITE_TRANSPORTS")
    if raw is not None:
        out["satellite_transports"] = tuple(
            t.strip().lower() for t in raw.split(",") if t.strip())
    for key, var in (("satellite_rediscover_s", "BACKEND_SATELLITE_REDISCOVER_S"),
                     ("satellite_discovery_ttl_s", "BACKEND_SATELLITE_DISCOVERY_TTL_S")):
        val = (env(var) or "").strip()
        if val:
            try:
                out[key] = float(val)
            except ValueError:
                logging.getLogger(__name__).error("Bad %s=%r ignored", var, val)
    for key, var in (("satellite_wifi_bind", "BACKEND_SATELLITE_WIFI_BIND"),
                     ("satellite_wifi_iface", "BACKEND_SATELLITE_WIFI_IFACE"),
                     ("satellite_wifi_ap_addr", "BACKEND_SATELLITE_WIFI_AP_ADDR"),
                     ("satellite_ble_socket", "BACKEND_SATELLITE_BLE_SOCKET")):
        val = env(var)
        if val is not None:
            out[key] = val.strip()
    kc = (env("BACKEND_SATELLITE_KEYCHAIN_DIR") or "").strip()
    if kc:
        out["satellite_keychain_dir"] = kc
    return out


def _build_ntp(mfd_enabled: bool) -> dict:
    """SNTP for the MFD Pi (backend/ntp_server.py):

        BACKEND_NTP_SERVE=1                    # default: on when the MFD manager is
        BACKEND_NTP_ALLOW=192.168.100.0/24     # clients answered
    """
    raw = (os.environ.get("BACKEND_NTP_SERVE") or "").strip().lower()
    serve = mfd_enabled if not raw else raw in ("1", "true", "yes", "on")
    return {"ntp_serve": serve,
            "ntp_allow": (os.environ.get("BACKEND_NTP_ALLOW") or "192.168.100.0/24").strip()}


def _build_config(args: argparse.Namespace) -> BackendConfig:
    mfd_enabled, mfd_config = _build_mfd()
    roadsense_enabled, roadsense_config = _build_roadsense()
    return BackendConfig(
        **_build_ntp(mfd_enabled),
        **_build_dashcam(),
        **_build_bean(),
        **_build_satellites(),
        gateway_port=args.gateway_port,
        powerbox_port=args.powerbox_port,
        auto_discover=not args.no_auto_discover,
        usb_hotplug=not args.no_usb_hotplug,
        usb_hub=args.usb_hub or os.environ.get("BACKEND_HUB"),
        usb_port_roles=parse_port_roles(args.usb_port_roles or os.environ.get("BACKEND_USB_PORT_ROLES")),
        # whoami probing for roles the hub-port map does not cover (e.g. the
        # gateway behind the CAN hub, one hub level down).
        usb_probe_fallback=_env_flag("BACKEND_USB_PROBE_FALLBACK"),
        port_power_config=_build_port_power(),
        serial_baudrate=args.baudrate,
        api_host=args.api_host,
        api_port=args.api_port,
        auth_token=args.auth_token or os.environ.get("BACKEND_AUTH_TOKEN"),
        db_path=args.db,
        tick_hz=args.tick_hz,
        replay_file=args.replay,
        replay_speed=args.replay_speed,
        replay_loop=args.replay_loop,
        nav_emulation=not args.no_nav_emu,
        nav_lang=args.nav_lang,
        recording=_build_recording(args),
        powerbox_enabled=not args.no_powerbox,
        power_mode_flag=args.power_mode_flag,
        undervoltage_threshold=args.undervoltage_threshold,
        undervoltage_recover=args.undervoltage_recover,
        undervoltage_confirm_s=args.undervoltage_confirm,
        shutdown_grace_s=args.shutdown_grace,
        local_poweroff_on_undervoltage=not args.no_local_poweroff,
        powerbox_auto_recover=args.powerbox_auto_recover,
        powerbox_stale_s=args.powerbox_stale_s,
        fan_policy=_build_fan_policy(args),
        mfd_enabled=mfd_enabled,
        mfd_config=mfd_config,
        roadsense_enabled=roadsense_enabled,
        roadsense_config=roadsense_config,
        roadsense_scene_config=_build_roadsense_scene(),
        acc_off_debounce_s=float(os.environ.get("BACKEND_ACC_OFF_DEBOUNCE_S", "3")),
        panda_enabled=(not args.no_panda) and _env_flag("BACKEND_PANDA_ENABLED", "1"),
        panda_bus=args.panda_bus if args.panda_bus is not None
        else (int(os.environ["BACKEND_PANDA_BUS"]) if (os.environ.get("BACKEND_PANDA_BUS") or "").strip() else None),
        panda_dedupe_s=args.panda_dedupe_s if args.panda_dedupe_s is not None
        else float(os.environ.get("BACKEND_PANDA_DEDUPE_S") or 0.25),
        # 0 = SILENT (default). 19 = NOOUTPUT (ACKs): bench only, see service.py.
        panda_safety_mode=int(os.environ.get("BACKEND_PANDA_SAFETY_MODE") or 0),
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
        help="comma-separated port:role mapping (e.g. 2:powerbox,3:gateway). Or use BACKEND_USB_PORT_ROLES.",
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

    # comma.ai panda: broadcast (unsolicited) CAN listener, SAFETY_SILENT.
    panda = parser.add_argument_group("panda (broadcast CAN)")
    panda.add_argument("--no-panda", action="store_true",
                       help="do not read broadcast CAN from the comma.ai panda "
                            "(or BACKEND_PANDA_ENABLED=0)")
    panda.add_argument("--panda-bus", type=int, default=None,
                       help="accept frames from this panda bus only (default: any; "
                            "or BACKEND_PANDA_BUS)")
    panda.add_argument("--panda-dedupe-s", type=float, default=None,
                       help="drop an identical payload repeated within this many "
                            "seconds (default 0.25; or BACKEND_PANDA_DEDUPE_S)")

    # Replay (run off a recorded NDJSON log instead of the live gateway)
    replay = parser.add_argument_group("replay")
    replay.add_argument(
        "--replay", default=None,
        help="replay a recorded NDJSON log instead of opening the gateway serial port "
             "(backend/TripRecorder logs or gateway-v3 captures from avc_capture.py / "
             "nav_emu.py: obj/raw + ht per line, paced by ht)",
    )
    replay.add_argument(
        "--replay-speed", type=float, default=1.0,
        help="replay speed multiplier (1.0 realtime, 0 as-fast-as-possible)",
    )
    replay.add_argument(
        "--replay-loop", action="store_true", help="loop the replay file",
    )

    # AVC-LAN nav ECU emulation (comm.avc.NavEmulator)
    avc = parser.add_argument_group("AVC-LAN")
    avc.add_argument(
        "--no-nav-emu", action="store_true",
        help="do not emulate the nav ECU (178) on the AVC-LAN (e.g. real nav connected)",
    )
    avc.add_argument(
        "--nav-lang", default="02",
        help="display UI language the emulated nav sets (B7 LL): 02 English, 06 Dutch",
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
        default=10.5,
        help="12V trip threshold in volts",
    )
    parser.add_argument(
        "--undervoltage-recover",
        type=float,
        default=11.0,
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
        help="grace seconds reported to the powerbox with the shutdown notice "
             "(it stops waking the POCO for at least this long)",
    )
    parser.add_argument(
        "--no-local-poweroff",
        action="store_true",
        help="do NOT run 'systemctl poweroff' locally when under-voltage trips "
             "(dev machines; on the car it is the only way to shed the load)",
    )
    parser.add_argument(
        "--powerbox-stale-s",
        type=float,
        default=15.0,
        help="seconds without a powerbox frame before the link is declared "
             "wedged and recovery starts (telemetry streams at 1 Hz; lower = "
             "faster wedge detection, shorter telemetry blackouts)",
    )
    parser.add_argument(
        "--powerbox-auto-recover",
        action="store_true",
        help="enable powerbox auto-recovery (force MCU reset via DTR toggle)",
    )
    
    # Chassis fan policy overrides (defaults: backend/fan_policy.py, docs/THERMAL_MODEL.md)
    fan = parser.add_argument_group("chassis fan", "override one number of the fan policy; "
                                    "unset = the measured default in backend/fan_policy.py")
    for prof in ("full", "low"):
        fan.add_argument("--fan-%s-on" % prof, dest="fan_%s_on" % prof, type=float, default=None,
                         help="control °C that engages the fan (%s profile)" % prof)
        fan.add_argument("--fan-%s-off" % prof, dest="fan_%s_off" % prof, type=float, default=None,
                         help="control °C that releases the fan (%s profile)" % prof)
        fan.add_argument("--fan-%s-full" % prof, dest="fan_%s_full" % prof, type=float, default=None,
                         help="control °C at which duty reaches max (%s profile)" % prof)
        fan.add_argument("--fan-%s-min-pct" % prof, dest="fan_%s_min_pct" % prof, type=float, default=None,
                         help="duty floor while engaged (%s profile)" % prof)
        fan.add_argument("--fan-%s-max-pct" % prof, dest="fan_%s_max_pct" % prof, type=float, default=None,
                         help="duty ceiling (%s profile)" % prof)
    fan.add_argument("--fan-safety-temp", type=float, default=None,
                     help="filtered control °C that forces 100 %%")
    fan.add_argument("--fan-tau", type=float, default=None,
                     help="low-pass time constant of the control temperature in s")

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
