"""
Powerbox integration — computer side of the second RP2040 ("powerbox").

The powerbox is the power-management board between the Prius 12 V system and the
POCO board computer. Over its own USB-CDC NDJSON link (merged into the ingress by
:class:`MultiInputPort` with an id offset of :data:`DEVICE_POWERBOX_BASE`) it
reports:

    device 200 (DEVICE_POWERBOX_BASE)   system / status / command acks
    device 201 (DEVICE_POWERBOX_POWER)  INA219 telemetry: voltage / current / power
    device 202 (DEVICE_POWERBOX_EVENTS) ignition (ACC) + constant battery line
    device 203 (DEVICE_POWERBOX_BEAN)   BEAN body-bus receiver: frames + counters (fw >= 2.3.0)

This module provides:

    * :func:`parse_powerbox_message` — decode a raw powerbox payload to Actions;
    * :func:`register_powerbox_ingress` — wire those parsers into an IngressController;
    * :func:`build_power_off_command` — tells the powerbox the POCO is shutting
      itself down (the box then sleeps instead of waking it; it cannot cut power
      since firmware 2.0);
    * :class:`PowerboxCommander` — sends powerbox commands over an OutputPort;
    * :class:`PriusPowerController` — applies the POCO power profile (full/low) by
      writing the ``/etc/prius/power-mode`` flag the ``prius-power`` unit watches.

The wire formats match powerbox firmware 2.1 (2026-11-03: board relay N =
channel N, ch1 = the box's own NC restart relay exposed only as
``hard_restart``, ``rly`` read back from the PCF8574 with ``null`` for ch1,
``rc``/``up`` boot diagnostics; fw 2.2.3 adds the ``bean`` transceiver bench
command) and stay tolerant of key/unit variants. No
transport is required for the parsers/rules to be unit-tested.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Callable, List, Optional

from .ports import (
    DEVICE_POWERBOX_BASE,
    DEVICE_POWERBOX_BEAN,
    DEVICE_POWERBOX_EVENTS,
    DEVICE_POWERBOX_POWER,
    OutgoingCommand,
)
from ..state.actions import (
    Action,
    SetPowerboxBeanAction,
    SetPowerboxBeanTxAction,
    SetPowerboxConnectionAction,
    SetPowerboxIgnitionAction,
    SetPowerboxPowerStatusAction,
    SetPowerboxTelemetryAction,
    SetBeanRxAction,
)
from ..state.app_state import (  # noqa: F401
    RELAY_CH_CAM_HUB,
    RELAY_CH_CAN_HUB,
    RELAY_CH_MFD,
    RELAY_CH_RESTART,
    RELAY_CH_RS485,
    RELAY_CHANNELS,
)

# Power-button hold times (ms) for the POCO F1 boards, measured on the bench
# 2026-10-26: 3 s does NOT boot a board from off, 6 s does; ~35 s force-resets
# a frozen SoC. A few hundred ms is a plain key press (logind ignores it).
BUTTON_POWER_ON_MS = 6000
BUTTON_FORCE_MS = 35000
BUTTON_TAP_MS = 300

# There is no second POCO (single-computer layout since 2026-11-04: this POCO
# runs the backend AND roadsense). Firmware 2.1 keeps the "poco 2" command
# slots but refuses them (ERROR POCO2_NC, POCO2_BTN_PIN = None); the backend
# treats them as inert.
POCO2_BUTTON_CONNECTED = False

if TYPE_CHECKING:  # avoid import cycles at runtime
    from .ingress import IngressController
    from .ports import OutputPort

logger = logging.getLogger(__name__)

# Last (role, version) we logged at INFO for the powerbox identity. The firmware
# re-announces IDENT every IDENT_HEARTBEAT_MS (~10 s) so the backend can identify
# a device that connected mid-stream; without de-duping we would log "Powerbox
# identified" at INFO every 10 s and bury real events (watchdog/recovery). We log
# at INFO only on first-seen or when role/version changes, DEBUG otherwise.
_last_identity_logged: Optional[tuple] = None


def reset_identity_log() -> None:
    """Forget the last-logged powerbox identity.

    Called when the link is marked disconnected (staleness watchdog) so the next
    IDENT/READY after recovery logs at INFO again — a genuine "link came back"
    event — instead of being de-duped to DEBUG.
    """
    global _last_identity_logged
    _last_identity_logged = None


# ─────────────────────────────────────────────────────────────────────────────
# Inbound: raw powerbox payload -> Actions
# ─────────────────────────────────────────────────────────────────────────────

def _coerce_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "on", "yes"):
            return True
        if low in ("0", "false", "off", "no"):
            return False
    return None


def parse_power_telemetry(data: dict) -> List[Action]:
    """Decode device 201 INA219 and env telemetry.

    Accepted keys (SI units preferred, milli-units tolerated):
        v / volt / voltage     bus voltage in volts   (or mv / mvolt in millivolts)
        i / cur / current      current in amps         (or ma / mcur in milliamps)
        p / pwr / power         power in watts          (or mw / mpwr in milliwatts)
        bmp_t, bmp_p           BMP280 @0x77 box-inside temperature (C), pressure (Pa)
        bmp2_t, bmp2_p         BMP280 @0x76 outside-box temperature (C), pressure (Pa)
        aht_t, aht_h           AHT20 box-inside temperature (C), relative humidity (%)
        fan_pct, fan_rpm       chassis fan: applied speed (%) and tach RPM (fw >= 2.0)
    """
    voltage = _coerce_float(data.get("v", data.get("voltage", data.get("volt"))))
    if voltage is None:
        mv = _coerce_float(data.get("mv", data.get("mvolt")))
        if mv is not None:
            voltage = mv / 1000.0

    current = _coerce_float(data.get("i", data.get("current", data.get("cur"))))
    if current is None:
        ma = _coerce_float(data.get("ma", data.get("mcur")))
        if ma is not None:
            current = ma / 1000.0

    power = _coerce_float(data.get("p", data.get("power", data.get("pwr"))))
    if power is None:
        mw = _coerce_float(data.get("mw", data.get("mpwr")))
        if mw is not None:
            power = mw / 1000.0

    bmp_t = _coerce_float(data.get("bmp_t"))
    bmp_p = _coerce_float(data.get("bmp_p"))
    bmp2_t = _coerce_float(data.get("bmp2_t"))
    bmp2_p = _coerce_float(data.get("bmp2_p"))
    aht_t = _coerce_float(data.get("aht_t"))
    aht_h = _coerce_float(data.get("aht_h"))
    energy_mah = _coerce_float(data.get("mah"))
    fan_pct = _coerce_float(data.get("fan_pct"))
    fan_rpm = _coerce_float(data.get("fan_rpm"))

    actions: List[Action] = []
    if not (voltage is None and current is None and power is None and bmp_t is None
            and bmp2_t is None and aht_t is None and energy_mah is None):
        actions.append(SetPowerboxTelemetryAction(
            voltage=voltage, current=current, power=power,
            bmp_t=bmp_t, bmp_p=bmp_p, bmp2_t=bmp2_t, bmp2_p=bmp2_p,
            aht_t=aht_t, aht_h=aht_h, energy_mah=energy_mah
        ))
    if fan_pct is not None or fan_rpm is not None:
        actions.append(SetPowerboxPowerStatusAction(
            fan_pct=fan_pct,
            fan_rpm=int(fan_rpm) if fan_rpm is not None else None,
        ))
    return actions


def parse_power_event(data: dict) -> List[Action]:
    """Decode device 202 ignition / power events.

    Accepted forms:
        {"acc": true, "batt": true}
        {"event": "ignition_on"}  / "ignition_off" / "acc_on" / "acc_off"
    """
    acc = _coerce_bool(data.get("acc", data.get(" acc")))
    batt = _coerce_bool(data.get("batt", data.get("battery")))

    event = data.get("event")
    if isinstance(event, str):
        ev = event.strip().lower()
        if ev in ("ignition_on", "acc_on", "on"):
            acc = True
        elif ev in ("ignition_off", "acc_off", "off"):
            acc = False
        elif ev in ("battery_lost", "batt_off"):
            batt = False
        elif ev in ("battery_present", "batt_on"):
            batt = True

    if acc is None and batt is None:
        return []
    return [
        SetPowerboxIgnitionAction(
            acc_on=bool(acc) if acc is not None else False,
            batt_present=bool(batt) if batt is not None else True,
        )
    ]


def parse_powerbox_system(data: dict) -> List[Action]:
    """Decode device 200 system/status messages (ready banner, IDENT, STATUS, acks)."""
    msg = str(data.get("msg", "")).upper()
    if "POWERBOX_READY" in msg or "IDENT" in msg or "READY" in msg:
        ver = data.get("ver", "unknown")
        role = data.get("role", "powerbox")
        is_boot = "POWERBOX_READY" in msg  # genuine boot banner (firmware (re)booted)
        kind = "ready" if is_boot else ("identified" if "IDENT" in msg else "ready")
        # Edge-triggered logging. A POWERBOX_READY boot banner always logs at INFO
        # (it marks a fresh firmware boot — a meaningful event). The routine ~10 s
        # IDENT re-announce only logs at INFO on first-seen or role/version change,
        # DEBUG otherwise, so it does not bury watchdog/recovery events.
        global _last_identity_logged
        identity = (role, ver)
        rc = data.get("rc")
        try:
            rc = int(rc) if rc is not None else None
        except (TypeError, ValueError):
            rc = None
        if is_boot:
            # fw >= 2.1 reports machine.reset_cause(): 1 = power-on (incl.
            # brown-out and hard_restart), 3 = watchdog. Every NO relay
            # dropped with this boot — the service re-asserts them.
            logger.warning("Powerbox BOOTED: role=%s v%s reset_cause=%s — relay loads dropped",
                           role, ver, rc)
            _last_identity_logged = identity
        elif identity != _last_identity_logged:
            _last_identity_logged = identity
            logger.info("Powerbox %s: role=%s v%s", kind, role, ver)
        else:
            logger.debug("Powerbox %s (re-announce): role=%s v%s", kind, role, ver)
        return [SetPowerboxConnectionAction(connected=True, boot=is_boot, reset_cause=rc)]

    if msg == "STATUS":
        # Periodic power-management heartbeat (fw >= 2.0): POCO liveness (main
        # from its heartbeat, secondary as we reported it), rolling counter,
        # state machine, fan, relay board. Any missing key stays None.
        def _b(key):
            return _coerce_bool(data[key]) if key in data else None
        hb = data.get("hb")
        try:
            hb = int(hb) if hb is not None else None
        except (TypeError, ValueError):
            hb = None
        pm = data.get("pm")
        # Relay board states: "rly": [ch1..ch8] of 0/1/null, 1 = load powered,
        # null = unknown / not a load (ch1 = restart relay, fw >= 2.1). Read
        # back from the PCF8574 by the firmware — ground truth for
        # RelayPortPower verification and the RS485 rail. Omitted by the
        # firmware when the read-back failed (rly_err then counts up).
        relays = None
        rly = data.get("rly")
        if isinstance(rly, (list, tuple)) and rly:
            relays = tuple(_coerce_bool(v) for v in rly)
        fan_rpm = _coerce_float(data.get("rpm"))

        def _i(key):
            try:
                return int(data[key]) if data.get(key) is not None else None
            except (TypeError, ValueError):
                return None
        return [SetPowerboxPowerStatusAction(
            poco_alive=_b("poco"),
            poco2_alive=_b("p2"),
            pm_state=str(pm) if pm is not None else None,
            hb=hb,
            relays=relays,
            fan_pct=_coerce_float(data.get("fan")),
            fan_rpm=int(fan_rpm) if fan_rpm is not None else None,
            reset_cause=_i("rc"),
            uptime_s=_i("up"),
            relay_fix_count=_i("rly_fix"),
            relay_err_count=_i("rly_err"),
            # fw >= 2.2.5 sends "acc_sim":1 only while the bench override is
            # active; its absence means no override.
            acc_sim=bool(_coerce_bool(data.get("acc_sim"))),
        )]

    if msg == "PMLOG":
        # Firmware power-event timeline (fw >= 1.15.0): boot / button presses /
        # shutdown / sleep with uptime + voltage. The single most useful
        # cold-boot diagnostic — it's the record of what the firmware did while
        # the POCO (and this logger) were OFF. Log each event on its own line.
        events = data.get("events") or []
        now_ms = data.get("now_ms")
        logger.info("Powerbox PMLOG: %d events, fw=%s, now_ms=%s",
                    len(events), data.get("ver"), now_ms)
        for ev in events:
            try:
                age_s = (int(now_ms) - int(ev.get("t_ms", 0))) / 1000.0 \
                    if now_ms is not None else None
            except (TypeError, ValueError):
                age_s = None
            logger.info("Powerbox PMLOG   %s: %s%s",
                        ev.get("ev"),
                        {k: v for k, v in ev.items() if k not in ("ev", "t_ms")},
                        (" (%.1fs ago)" % age_s) if age_s is not None else "")
        return []

    if msg == "PMEVENT":
        # Live echo of a single power event as it happens (fw >= 1.15.0).
        logger.info("Powerbox PMEVENT: %s",
                    {k: v for k, v in data.items() if k != "msg"})
        return []

    if msg == "HARD_RESTART":
        logger.warning("Powerbox HARD_RESTART: cutting its own supply now (uptime %ss); "
                       "MFD/CAN/camera relays drop, box back in ~5 s", data.get("up"))
        return []

    if msg in ("POCO_BTN", "INTERRUPTED"):
        # Power-button actuation (wake ladder / manual press) and REPL drops.
        # Invaluable for cold-boot debugging: shows EXACTLY when and how long
        # the firmware pressed the POCO power button.
        logger.info("Powerbox %s: %s", msg,
                    {k: v for k, v in data.items() if k != "msg"})
        return []

    if msg in ("I2C_OK", "I2C_SCAN", "SENSOR_OK", "IDLE", "FAN", "FAN_ON", "WDT_ON"):
        # Boot/diagnostic messages (bus scan results, per-chip detection,
        # applied fan PWM readback). Surface them in the journal — invaluable
        # for bench wiring / PWM characterization work.
        logger.info("Powerbox %s: %s", msg,
                    {k: v for k, v in data.items() if k != "msg"})
        return []

    if msg in ("SHUTDOWN", "SLEEP"):
        # SHUTDOWN: the firmware's under-voltage backstop fired (or it echoes
        # our "off") — the POCO must halt itself. SLEEP: the box stopped waking
        # the POCO until ACC rises or a "wake" command. Mirror the state.
        logger.warning("Powerbox %s: %s", msg, data.get("reason", ""))
        return [SetPowerboxPowerStatusAction(pm_state=msg.lower())]

    if msg == "ERROR":
        code = str(data.get("code", ""))
        logger.warning("Powerbox ERROR %s: %s", code, data.get("detail", ""))
        if code == "BEAN_RX":
            return [SetBeanRxAction(error=data.get("detail", ""), recv_ts=time.time())]
        if code in ("BEAN", "BEAN_ARGS", "BEAN_TX_DISABLED"):
            # BEAN bench op refused / failed: surface it in powerbox.bean so a
            # REST caller polling the state sees why nothing came back.
            return [SetPowerboxBeanAction({"error": code,
                                           "detail": data.get("detail", "")})]
        return []

    ack = data.get("ack")
    if ack == "acc_sim":
        v = data.get("v")
        logger.warning("Powerbox ACC override %s (real ACC pin %s) — BENCH ONLY",
                       "cleared" if v is None else ("ON" if v else "OFF"),
                       data.get("pin"))
        return [SetPowerboxPowerStatusAction(acc_sim=v is not None)]
    if ack == "bean_rx":
        # BEAN receiver control ack (fw >= 2.3.0): on/off + capture mode, or
        # a stats/raw request (the data itself follows on device 203).
        result = {k: v for k, v in data.items() if k != "ack"}
        logger.info("Powerbox BEAN receiver: %s", result)
        if "on" in result:
            return [SetBeanRxAction(ack=result, recv_ts=time.time())]
        return []
    if ack == "bean_tx_enable":
        logger.warning("Powerbox BEAN TX guard %s",
                       "ARMED for %ss" % data.get("s") if data.get("v") else "disarmed")
        return [SetPowerboxBeanAction({"tx_enabled": bool(data.get("v")), "s": data.get("s")})]
    if ack == "bean_tx":
        # BEAN transmitter reply (fw >= 2.4.0): edge test, frame send or a
        # guard change; refusals carry "err" + "detail".
        result = {k: v for k, v in data.items() if k != "ack"}
        log = logger.warning if result.get("op") in ("edge", "send", "allow", "bench") \
            else logger.info
        log("Powerbox BEAN TX %s: %s", result.get("op"), result)
        return [SetPowerboxBeanTxAction(result)]
    if ack == "bean":
        # BEAN transceiver bench result (fw >= 2.2.3). The firmware also
        # echoes it as a PMEVENT (ev "bean"), which is what gets journalled.
        result = {k: v for k, v in data.items() if k != "ack"}
        logger.debug("Powerbox bean ack: %s", result)
        return [SetPowerboxBeanAction(result)]
    if ack:
        logger.info("Powerbox ack: %s", ack)
    return []


_bean_raw: list = []
_bean_clock = None          # comm.bean.BeanClock, created on first use


def bean_clock():
    """The process-wide BEAN frame-clock estimator (see comm.bean.BeanClock)."""
    global _bean_clock
    if _bean_clock is None:
        from ..comm.bean import BeanClock
        _bean_clock = BeanClock()
    return _bean_clock


def parse_bean_message(data: dict, recv_ts: Optional[float] = None) -> List[Action]:
    """Decode a device 203 BEAN receiver line (fw >= 2.3.0).

    ``{"t":ms,"bf":[[t_ms,pri,"DST","MES","DATA",rsp,code],..],"st":{..}}``
    or a raw run-length dump chunk ``{"raw":[us..],"ri":k,"rn":N}`` (+ =
    dominant, - = recessive); the chunks are joined and published once
    complete (powerbox.bean_rx.raw).

    Frame times: the firmware's frame clock is not on its ``t`` time base
    (fw 2.4.0), so frames are timed by the lower-envelope estimator
    :func:`bean_clock` unless the ``t`` based age is plausible."""
    from ..comm.bean import frames_from_batch
    global _bean_raw
    now = time.time() if recv_ts is None else recv_ts
    if "raw" in data:
        chunk = data.get("raw") or []
        if data.get("ri", 0) == 0:
            _bean_raw = []
        _bean_raw.extend(chunk)
        rn = data.get("rn")
        if rn is not None and len(_bean_raw) >= int(rn):
            raw, _bean_raw = _bean_raw, []
            logger.info("Powerbox BEAN raw runs (%d): %s", len(raw), raw)
            return [SetBeanRxAction(raw=raw, recv_ts=now)]
        return []
    frames = frames_from_batch(data.get("bf") or [], data.get("t"), now, clock=bean_clock())
    stats = data.get("st") if isinstance(data.get("st"), dict) else None
    if not frames and stats is None:
        return []
    return [SetBeanRxAction(frames=frames, stats=stats, recv_ts=now)]


def parse_powerbox_message(device_id: int, data: dict) -> List[Action]:
    """Route a powerbox payload to the right parser by (offset) device id."""
    if device_id == DEVICE_POWERBOX_BEAN:
        return parse_bean_message(data)
    if device_id == DEVICE_POWERBOX_POWER:
        return parse_power_telemetry(data)
    if device_id == DEVICE_POWERBOX_EVENTS:
        return parse_power_event(data)
    if device_id == DEVICE_POWERBOX_BASE:
        return parse_powerbox_system(data)
    return []


class AccOffDebouncer:
    """Hold an ACC ON->OFF edge for ``hold_s`` before it reaches the store.

    Everything downstream (power profile, roadsense, MFD, satellite rail,
    gateway relay) follows ``powerbox.acc_on``; a key turned past ACC and back,
    or a bounce on the sense line, must not start the whole key-off cascade.
    ACC-on always passes immediately. While an OFF is held, frames are passed
    through with ``acc_on=True`` so the battery flag keeps flowing. The held
    OFF is released by :meth:`due` (called from the engine tick).
    """

    def __init__(self, hold_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.hold_s = max(0.0, float(hold_s))
        self._clock = clock
        self._last_acc: Optional[bool] = None   # last value that reached the store
        self._pending_since: Optional[float] = None
        self._pending_batt: bool = True

    @property
    def pending(self) -> bool:
        return self._pending_since is not None

    def filter(self, actions: List[Action]) -> List[Action]:
        out: List[Action] = []
        for a in actions:
            if not isinstance(a, SetPowerboxIgnitionAction):
                out.append(a)
                continue
            if a.acc_on:
                if self._pending_since is not None:
                    logger.info("ACC-off glitch ignored: back on after %.1fs",
                                self._clock() - self._pending_since)
                self._pending_since = None
                self._last_acc = True
                out.append(a)
            elif self._last_acc is not True or self.hold_s <= 0:
                # Not an ON->OFF edge (boot with the key off, repeats of an
                # OFF already applied) or debounce disabled: pass through.
                self._last_acc = False
                out.append(a)
            else:
                if self._pending_since is None:
                    self._pending_since = self._clock()
                    logger.info("ACC off seen: holding %.1fs before acting", self.hold_s)
                self._pending_batt = bool(a.batt_present)
                out.append(SetPowerboxIgnitionAction(
                    acc_on=True, batt_present=self._pending_batt))
        return out

    def due(self) -> Optional[Action]:
        """The held ACC-off once ``hold_s`` has elapsed, else None."""
        if self._pending_since is None:
            return None
        if self._clock() - self._pending_since < self.hold_s:
            return None
        self._pending_since = None
        self._last_acc = False
        logger.info("ACC off confirmed after %.1fs hold", self.hold_s)
        return SetPowerboxIgnitionAction(acc_on=False, batt_present=self._pending_batt)


def register_powerbox_ingress(
    ingress: "IngressController",
    action_filter: Optional[Callable[[List[Action]], List[Action]]] = None,
) -> None:
    """Register satellite handlers for the powerbox device id range.

    ``action_filter`` (e.g. :meth:`AccOffDebouncer.filter`) post-processes the
    actions of every powerbox frame before they reach the store.
    """
    def _handler(dev: int):
        if action_filter is None:
            return lambda d: parse_powerbox_message(dev, d)
        return lambda d: action_filter(parse_powerbox_message(dev, d))

    ingress.register_satellite_handler(DEVICE_POWERBOX_BASE, _handler(DEVICE_POWERBOX_BASE))
    ingress.register_satellite_handler(DEVICE_POWERBOX_POWER, _handler(DEVICE_POWERBOX_POWER))
    ingress.register_satellite_handler(DEVICE_POWERBOX_EVENTS, _handler(DEVICE_POWERBOX_EVENTS))
    ingress.register_satellite_handler(DEVICE_POWERBOX_BEAN, _handler(DEVICE_POWERBOX_BEAN))
    logger.info("Powerbox ingress handlers registered (devices %d-%d)",
                DEVICE_POWERBOX_BASE, DEVICE_POWERBOX_BEAN)


# ─────────────────────────────────────────────────────────────────────────────
# Outbound: computer -> powerbox commands
# ─────────────────────────────────────────────────────────────────────────────

# The powerbox listens on its LOCAL system channel (id 0). Commands are sent on
# the powerbox's own OutputPort, so the wire id is the powerbox-local id — NOT the
# computer-side DEVICE_POWERBOX_BASE offset (that offset only applies to ingress,
# where MultiInputPort maps the powerbox's id 0 -> 200).
POWERBOX_LOCAL_SYSTEM = 0


def build_power_off_command(reason: str = "undervoltage", grace_s: int = 30) -> OutgoingCommand:
    """Tell the powerbox the POCO is shutting itself down.

    Since firmware 2.0 the box cannot cut power: on "off" it stops waking the
    POCO (state "shutdown", then "sleep" once the heartbeat is gone or
    ``grace_s`` elapsed) until ACC rises again or a "wake" command arrives.
    The caller is responsible for the actual OS halt.
    """
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "off", "reason": reason, "grace_s": int(grace_s)},
        priority=100,
    )


def build_heartbeat_command(counter: int) -> OutgoingCommand:
    """Send the POCO->powerbox heartbeat with a rolling counter (0-255)."""
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "hb", "n": int(counter) & 0xFF},
        priority=10,
    )


def build_wake_command(poco: int = 1) -> OutgoingCommand:
    """Leave the powerbox "sleep" state and re-arm the main POCO wake ladder
    (``poco=1``), or press the secondary's power-on hold (``poco=2``)."""
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "wake", "poco": 2 if int(poco) == 2 else 1},
        priority=60,
    )


def build_poco2_alive_command(alive: bool) -> OutgoingCommand:
    """Report the secondary POCO's liveness to the powerbox (echoed in STATUS
    ``p2``; the box never presses the secondary on its own)."""
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "p2", "alive": bool(alive)},
        priority=10,
    )


def build_relay_command(channel: int, on: bool) -> OutgoingCommand:
    """Set a relay-board LOAD channel (ch 2-8 on the PCF8574 expander).

    Channel map (fw >= 2.1, board relay N = channel N): ch2 = MFD Pi, ch3 =
    CAN hub (gateway + comma.ai panda), ch4 = camera hub (dashcam), ch5 =
    RS485 satellite power, ch6 = secondary POCO hub (reserved), ch7/8 spare.
    ch1 is the powerbox's own NC restart relay: refused here (ValueError) and
    by the firmware; see :func:`build_hard_restart_command`. The link is
    lossy — callers must verify against the ``rly`` STATUS telemetry and
    retry (see backend.port_power.RelayPortPower).
    """
    channel = int(channel)
    if channel == RELAY_CH_RESTART:
        raise ValueError("relay ch%d is the powerbox restart relay, not a load; "
                         "use build_hard_restart_command()" % channel)
    if not 1 <= channel <= RELAY_CHANNELS:
        raise ValueError("relay channel out of range: %d" % channel)
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "relay", "ch": channel, "on": bool(on)},
        priority=50,
    )


def build_hard_restart_command() -> OutgoingCommand:
    """Hard-restart the powerbox via its NC self-supply relay (fw >= 2.1).

    The firmware acks ``hard_restart``, then energises relay 1 and loses its
    own power; USB drops ~0.4 s later and the next STATUS arrives ~5 s after
    the command. Relay VCC is the RP2040's 5 V, so every NO relay (MFD Pi,
    CAN hub, camera hub) drops too and comes back OFF — the service
    re-asserts wanted states on the POWERBOX_READY banner.
    """
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "hard_restart"},
        priority=100,
    )


def build_i2c_scan_command() -> OutgoingCommand:
    """Ask the firmware for a live I2C bus rescan (fw >= 1.9.1).

    The reply is an ID_SYSTEM ``I2C_SCAN`` message with the ACKing addresses,
    logged to the journal by :func:`parse_powerbox_system`. Diagnostic only —
    device binding still happens at firmware boot.
    """
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "i2c_scan"},
        priority=50,
    )


def build_pmlog_command() -> OutgoingCommand:
    """Ask the firmware to dump its power-event ring buffer (fw >= 1.15.0).

    The reply is an ID_SYSTEM ``PMLOG`` message carrying the boot / button /
    shutdown / sleep timeline the host could not observe live (it was OFF
    during the cold boot). Logged by :func:`parse_powerbox_system`.
    """
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "getlog"},
        priority=15,
    )


def build_button_command(ms: int = BUTTON_POWER_ON_MS, poco: int = 1) -> OutgoingCommand:
    """Pulse a POCO power button: ``poco`` 1 = main (GP27), 2 = secondary
    (not wired in fw 2.1 — the firmware answers ERROR POCO2_NC and does not
    press; see POCO2_BUTTON_CONNECTED). BUTTON_POWER_ON_MS boots an off
    phone, BUTTON_FORCE_MS resets a frozen one."""
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "button", "poco": 2 if int(poco) == 2 else 1, "ms": int(ms)},
        priority=60,
    )


def build_fan_command(pct: float) -> OutgoingCommand:
    """Set the chassis fan speed in percent (fw >= 2.1: 25 kHz 4-wire PWM on
    GP29, NOT inverted — GPIO duty = fan duty; 0 % stops the fan)."""
    pct = max(0, min(100, int(round(float(pct)))))
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "fan", "pct": pct},
        priority=20,
    )


# BEAN transceiver bench ops (fw >= 2.2.3; GP7 TX, GP6 RX inverted). Same caps
# as the firmware (BEAN_* in powerbox/main.py), which also refuses anything
# outside them with ERROR BEAN_ARGS. op -> {field: (default, lo, hi)}.
BEAN_OPS = {
    "status": {},
    "tx": {"v": (1, 0, 1), "ms": (1, 1, 500)},
    "pulse": {"us": (100, 1, 2000)},
    "loop": {"n": (20, 1, 200)},
    "square": {"hz": (5000, 100, 20000), "ms": (200, 1, 1000)},
}


def validate_bean_params(op: str, params: Optional[dict] = None) -> dict:
    """Check a BEAN bench op and its optional fields against the firmware
    caps. Returns the explicit fields (ints; omitted ones are left to the
    firmware defaults). Raises ValueError on an unknown op, an unknown field
    or a value out of range."""
    op = str(op).strip().lower()
    if op not in BEAN_OPS:
        raise ValueError("bean op must be one of %s" % ", ".join(BEAN_OPS))
    spec = BEAN_OPS[op]
    out = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if key not in spec:
            raise ValueError("bean op %s takes no '%s' (fields: %s)"
                             % (op, key, ", ".join(spec) or "none"))
        if isinstance(value, bool):
            raise ValueError("bean '%s' must be an integer" % key)
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            raise ValueError("bean '%s' must be an integer" % key)
        if isinstance(value, float) and value != ivalue:
            raise ValueError("bean '%s' must be an integer" % key)
        _, lo, hi = spec[key]
        if not lo <= ivalue <= hi:
            raise ValueError("bean '%s' must be between %d and %d" % (key, lo, hi))
        out[key] = ivalue
    return out


def build_bean_command(op: str, **kw) -> OutgoingCommand:
    """BEAN transceiver bench op: ``{"a":"bean","op":op,...}`` (validated by
    :func:`validate_bean_params`; ValueError on bad input). The firmware
    answers ``{"ack":"bean","op":op,...results}`` (mirrored into
    ``powerbox.bean``) or ``ERROR BEAN_ARGS``."""
    fields = validate_bean_params(op, kw)
    payload = {"a": "bean", "op": str(op).strip().lower()}
    payload.update(fields)
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload=payload,
        priority=50,
    )


def build_acc_sim_command(on: Optional[bool]) -> OutgoingCommand:
    """BENCH ONLY: override the powerbox ACC input (fw >= 2.2.5): True / False
    simulate key-on / key-off everywhere the firmware reports ACC, None clears.
    RAM only on the board; it auto-expires after 30 min."""
    return OutgoingCommand(
        device_id=POWERBOX_LOCAL_SYSTEM,
        command_type="power",
        payload={"a": "acc_sim", "v": None if on is None else (1 if on else 0)},
        priority=50,
    )


# BEAN transmitter (fw >= 2.4.0, powerbox/bean_tx.py). Same argument checks as
# the firmware; the firmware alone decides whether anything is sent (arming,
# per-boot frames flag, edge test, receiver health, block list, budgets).
BEAN_TX_OPS = ("status", "edge", "allow", "bench", "sp", "send")
BEAN_TX_SP = (25, 75)
BEAN_TX_RETRIES_MAX = 3


def _bean_tx_int(name: str, value, lo: int, hi: int) -> int:
    if isinstance(value, bool) or isinstance(value, str) or value is None:
        raise ValueError("bean_tx '%s' must be an integer" % name)
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise ValueError("bean_tx '%s' must be an integer" % name)
    if ivalue != value or not lo <= ivalue <= hi:
        raise ValueError("bean_tx '%s' must be an integer %d..%d" % (name, lo, hi))
    return ivalue


def _bean_tx_byte(name: str, value) -> str:
    """Bus byte as two hex digits (accepts "FE", "fe", 254)."""
    if isinstance(value, bool) or value is None:
        raise ValueError("bean_tx '%s' must be a byte (hex string or int)" % name)
    if isinstance(value, str):
        text = value.strip()
        if not 1 <= len(text) <= 2:
            raise ValueError("bean_tx '%s' must be 1-2 hex digits" % name)
        try:
            ivalue = int(text, 16)
        except ValueError:
            raise ValueError("bean_tx '%s' must be 1-2 hex digits" % name)
    else:
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            raise ValueError("bean_tx '%s' must be a byte (hex string or int)" % name)
        if ivalue != value:
            raise ValueError("bean_tx '%s' must be a byte (hex string or int)" % name)
    if not 0 <= ivalue <= 255:
        raise ValueError("bean_tx '%s' must be 00..FF" % name)
    return "%02X" % ivalue


def _bean_tx_flag(name: str, value) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if value in (0, 1):
        return int(value)
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "on", "yes"):
        return 1
    if isinstance(value, str) and value.strip().lower() in ("0", "false", "off", "no"):
        return 0
    raise ValueError("bean_tx '%s' must be a boolean" % name)


def validate_bean_tx_params(op: str, params: Optional[dict] = None) -> dict:
    """Check a BEAN transmitter op and its fields; returns the wire fields.
    Raises ValueError on an unknown op, an unknown or missing field or a
    value out of range.

    status                                   guards, last edge test, budget
    edge   [wake]                            one dominant pulse of one bit time
    allow  v                                 per-boot "frames allowed" flag
    bench  v                                 per-boot bench override
    sp     pct (25..75)                      readback sample point
    send   pri (0..15) dst mes data [retries 0..3] [wake]
    """
    op = str(op).strip().lower()
    if op not in BEAN_TX_OPS:
        raise ValueError("bean_tx op must be one of %s" % ", ".join(BEAN_TX_OPS))
    p = {k: v for k, v in (params or {}).items() if v is not None}
    fields = {"status": (), "edge": ("wake",), "allow": ("v",), "bench": ("v",),
              "sp": ("pct",), "send": ("pri", "dst", "mes", "data", "retries", "wake")}[op]
    for key in p:
        if key not in fields:
            raise ValueError("bean_tx op %s takes no '%s' (fields: %s)"
                             % (op, key, ", ".join(fields) or "none"))
    out: dict = {}
    if "wake" in p:
        if _bean_tx_flag("wake", p["wake"]):
            out["wake"] = 1
    if op in ("allow", "bench"):
        if "v" not in p:
            raise ValueError("bean_tx op %s needs 'v'" % op)
        out["v"] = _bean_tx_flag("v", p["v"])
    elif op == "sp":
        if "pct" not in p:
            raise ValueError("bean_tx op sp needs 'pct'")
        out["pct"] = _bean_tx_int("pct", p["pct"], BEAN_TX_SP[0], BEAN_TX_SP[1])
    elif op == "send":
        for key in ("pri", "dst", "mes", "data"):
            if key not in p:
                raise ValueError("bean_tx op send needs '%s'" % key)
        out["pri"] = _bean_tx_int("pri", p["pri"], 0, 15)
        out["dst"] = _bean_tx_byte("dst", p["dst"])
        out["mes"] = _bean_tx_byte("mes", p["mes"])
        data = p["data"]
        if not isinstance(data, str):
            raise ValueError("bean_tx 'data' must be a hex string")
        data = data.strip().replace(" ", "").upper()
        try:
            raw = bytes.fromhex(data)
        except ValueError:
            raise ValueError("bean_tx 'data' must be a hex string")
        if not 1 <= len(raw) <= 11:
            raise ValueError("bean_tx 'data' must be 1..11 bytes")
        out["data"] = data
        if "retries" in p:
            out["retries"] = _bean_tx_int("retries", p["retries"], 0, BEAN_TX_RETRIES_MAX)
    return out


def build_bean_tx_command(op: str, **kw) -> OutgoingCommand:
    """BEAN transmitter op: ``{"a":"bean_tx","op":op,...}`` (validated by
    :func:`validate_bean_tx_params`; ValueError on bad input). The firmware
    answers ``{"ack":"bean_tx","op":op,"ok":0|1,...}`` (mirrored into
    ``powerbox.bean_tx``)."""
    fields = validate_bean_tx_params(op, kw)
    payload = {"a": "bean_tx", "op": str(op).strip().lower()}
    payload.update(fields)
    return OutgoingCommand(device_id=POWERBOX_LOCAL_SYSTEM, command_type="power",
                           payload=payload, priority=50)


BEAN_RX_RAW_MAX = 512


def build_bean_rx_command(on: Optional[bool] = None, op: Optional[str] = None,
                          raw: Optional[int] = None) -> OutgoingCommand:
    """BEAN receiver control (fw >= 2.3.0): ``{"a":"bean_rx","on":0|1}``,
    ``{"a":"bean_rx","op":"stats"}`` or ``{"a":"bean_rx","raw":N}`` (exactly
    one; ValueError otherwise). The receiver never drives the bus."""
    given = [x is not None for x in (on, op, raw)]
    if sum(given) != 1:
        raise ValueError("bean_rx takes exactly one of on / op / raw")
    if on is not None:
        payload = {"a": "bean_rx", "on": 1 if on else 0}
    elif op is not None:
        if str(op).lower() != "stats":
            raise ValueError("bean_rx op must be 'stats'")
        payload = {"a": "bean_rx", "op": "stats"}
    else:
        raw = int(raw)
        if not 1 <= raw <= BEAN_RX_RAW_MAX:
            raise ValueError("bean_rx raw must be 1..%d" % BEAN_RX_RAW_MAX)
        payload = {"a": "bean_rx", "raw": raw}
    return OutgoingCommand(device_id=POWERBOX_LOCAL_SYSTEM, command_type="power",
                           payload=payload, priority=40)


def build_bean_tx_enable_command(on: bool) -> OutgoingCommand:
    """BENCH ONLY: arm (60 s) / disarm the firmware BEAN TX guard (fw >= 2.3.0)."""
    return OutgoingCommand(device_id=POWERBOX_LOCAL_SYSTEM, command_type="power",
                           payload={"a": "bean_tx_enable", "v": 1 if on else 0},
                           priority=50)


class PowerboxCommander:
    """Send commands to the powerbox over an OutputPort (None = log only)."""

    def __init__(self, output_port: "Optional[OutputPort]" = None) -> None:
        self._output_port = output_port

    def _send(self, cmd: OutgoingCommand, what: str, warn: bool = False) -> bool:
        if self._output_port is None:
            log = logger.warning if warn else logger.debug
            log("Powerbox %s requested but no output port is wired — not sent", what)
            return False
        try:
            ok = self._output_port.send(cmd)
            (logger.warning if warn else logger.debug)("Sent powerbox %s: ok=%s", what, ok)
            return bool(ok)
        except Exception:
            logger.exception("Failed to send powerbox %s", what)
            return False

    def request_power_off(self, reason: str = "undervoltage", grace_s: int = 30) -> bool:
        return self._send(
            build_power_off_command(reason=reason, grace_s=grace_s),
            "power-off (reason=%s, grace=%ds)" % (reason, grace_s),
            warn=True,
        )

    def send_heartbeat(self, counter: int) -> bool:
        return self._send(build_heartbeat_command(counter), "heartbeat")

    def set_relay(self, channel: int, on: bool) -> bool:
        try:
            cmd = build_relay_command(channel, on)
        except ValueError as exc:
            logger.error("Powerbox relay command refused: %s", exc)
            return False
        return self._send(cmd, "relay%d=%s" % (int(channel), "on" if on else "off"),
                          warn=True)

    def set_rs485_power(self, on: bool) -> bool:
        """RS485 satellite rail = relay RELAY_CH_RS485."""
        return self.set_relay(RELAY_CH_RS485, on)

    def hard_restart(self) -> bool:
        """Cut the powerbox's own supply (restart relay). Drops every NO relay."""
        return self._send(build_hard_restart_command(), "hard_restart", warn=True)

    def press_button(self, ms: int = BUTTON_POWER_ON_MS, poco: int = 1) -> bool:
        return self._send(build_button_command(ms, poco),
                          "power-button poco%d %dms" % (poco, ms), warn=True)

    def wake(self, poco: int = 1) -> bool:
        return self._send(build_wake_command(poco), "wake poco%d" % poco, warn=True)

    def report_poco2_alive(self, alive: bool) -> bool:
        return self._send(build_poco2_alive_command(alive),
                          "p2 alive=%s" % alive)

    def request_i2c_scan(self) -> bool:
        return self._send(build_i2c_scan_command(), "i2c_scan", warn=True)

    def request_pmlog(self) -> bool:
        return self._send(build_pmlog_command(), "getlog", warn=True)

    def set_fan(self, pct: float) -> bool:
        return self._send(build_fan_command(pct), "fan %.0f%%" % float(pct))

    def set_acc_sim(self, on: Optional[bool]) -> bool:
        """BENCH ONLY ACC override (None clears)."""
        return self._send(build_acc_sim_command(on), "acc_sim %s" % on, warn=True)

    def bean_rx(self, on: Optional[bool] = None, op: Optional[str] = None,
                raw: Optional[int] = None) -> bool:
        """BEAN receiver control (see :func:`build_bean_rx_command`)."""
        try:
            cmd = build_bean_rx_command(on=on, op=op, raw=raw)
        except ValueError as exc:
            logger.error("Powerbox bean_rx command refused: %s", exc)
            return False
        return self._send(cmd, "bean_rx %s" % cmd.payload, warn=True)

    def bean_tx_enable(self, on: bool) -> bool:
        return self._send(build_bean_tx_enable_command(on),
                          "bean_tx_enable %s" % bool(on), warn=True)

    def send_bean_tx(self, op: str, **kw) -> bool:
        """BEAN transmitter op (see :func:`build_bean_tx_command`)."""
        try:
            cmd = build_bean_tx_command(op, **kw)
        except ValueError as exc:
            logger.error("Powerbox bean_tx command refused: %s", exc)
            return False
        return self._send(cmd, "bean_tx %s" % cmd.payload, warn=True)

    def send_bean(self, op: str, **kw) -> bool:
        """BEAN transceiver bench op (see :func:`build_bean_command`)."""
        try:
            cmd = build_bean_command(op, **kw)
        except ValueError as exc:
            logger.error("Powerbox bean command refused: %s", exc)
            return False
        return self._send(cmd, "bean %s %s" % (cmd.payload["op"], kw), warn=True)

    def bean_wake(self, retries: int = 2) -> bool:
        """Wake the sleeping BEAN body bus by transmitting the safe, allow-listed
        test frame FE:48 (a single valid frame triggers the NM wake cascade).

        Fires the guarded TX sequence the firmware requires — arm (60 s), edge
        test, per-boot frames flag, then the send with wake=1 — as four ordered
        serial commands; the firmware processes them in order (the edge op
        blocks its command loop until done, so allow/send see a passed edge).
        Only ever sends FE:48; it never uses the bench override, so the block
        list and budgets still apply. The arm window is left to auto-expire.
        Returns True if all four commands were written to the link (the actual
        wake result arrives async in PowerboxState.bean_tx)."""
        retries = max(0, min(3, int(retries)))
        ok = self.bean_tx_enable(True)
        ok = self.send_bean_tx("edge", wake=True) and ok
        ok = self.send_bean_tx("allow", v=True) and ok
        ok = self.send_bean_tx("send", pri=2, dst="FE", mes="48", data="00",
                               wake=True, retries=retries) and ok
        return ok


# ─────────────────────────────────────────────────────────────────────────────
# POCO power profile control (writes the prius-power flag file)
# ─────────────────────────────────────────────────────────────────────────────

class PriusPowerController:
    """Apply the POCO CPU power profile by writing ``/etc/prius/power-mode``.

    The ``prius-power.path`` systemd unit watches this flag and re-applies the
    profile (``full`` = all cores, ``low`` = Gold cluster offline + capped). On a
    dev machine the path is not writable; that is logged and treated as a no-op so
    the same code runs everywhere.
    """

    FULL = "full"
    LOW = "low"

    def __init__(self, flag_path: str = "/etc/prius/power-mode") -> None:
        self._flag_path = flag_path
        self._last_written: Optional[str] = None

    @property
    def current(self) -> Optional[str]:
        return self._last_written

    def set_mode(self, mode: str) -> bool:
        """Write the power-mode flag if it changed. Returns True on write."""
        mode = self.FULL if mode == self.FULL else self.LOW
        if mode == self._last_written:
            return False
        try:
            with open(self._flag_path, "w", encoding="ascii") as fh:
                fh.write(mode + "\n")
            self._last_written = mode
            logger.info("POCO power profile -> %s (%s)", mode, self._flag_path)
            return True
        except OSError as exc:
            # Remember intent so we do not spam the log every tick on dev hosts.
            self._last_written = mode
            logger.info("POCO power profile -> %s (no-op: %s)", mode, exc)
            return False

    def set_for_ignition(self, acc_on: bool) -> bool:
        """ACC/ON -> full performance; key off -> low idle."""
        return self.set_mode(self.FULL if acc_on else self.LOW)
