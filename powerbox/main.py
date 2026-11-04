"""
Powerbox RP2040 Firmware — NDJSON protocol over USB-CDC.

Communicates with the POCO backend (cyberpunk_computer) using the same
NDJSON envelope format as the gateway and satellites:

    {"id": <device_id>, "d": <payload>}

Device IDs (offset by DEVICE_POWERBOX_BASE=200 on the computer side):
    0  → 200  System: ready banner, acks, errors, STATUS heartbeat
    1  → 201  Telemetry: INA219 voltage / current / accumulated mAh, env
              sensors, fan duty + tach RPM
    2  → 202  Events: ignition (ACC) state

The computer side remaps these via MultiInputPort id_offset=200.
On the wire (this firmware) we use the LOCAL ids 0/1/2.

Inbound commands from the computer (device 0):
    {"id": 0, "d": {"a": "off", "reason": "...", "grace_s": 30}}
            host is shutting itself down -> stop waking it (sleep) until ACC
            rises again or a "wake" arrives
    {"id": 0, "d": {"a": "wake", "poco": 1}}        leave sleep / re-arm ladder
    {"id": 0, "d": {"a": "button", "poco": 1, "ms": 6000}}  pulse the button
            (poco 2 is NOT connected: both reply ERROR POCO2_NC, no press)
    {"id": 0, "d": {"a": "hb", "n": 0-255}}         main POCO heartbeat
    {"id": 0, "d": {"a": "p2", "alive": true|false}} host-reported secondary
            POCO liveness (the secondary is not on our USB; the main POCO
            pings it over the network and tells us)
    {"id": 0, "d": {"a": "fan", "pct": 0-100}}      chassis fan speed
    {"id": 0, "d": {"a": "relay", "ch": 2-8, "on": true|false}}  relay board
            (ch 1 is the powerbox restart relay and is REFUSED here)
    {"id": 0, "d": {"a": "hard_restart"}}  energise relay 1 = cut our own
            supply; every NO relay drops, the box is back ~5 s later
    {"id": 0, "d": {"a": "set_interval", "ms": 1000}}
    {"id": 0, "d": {"a": "whoami"}}  → {"msg":"IDENT","role":"powerbox","ver":..}
    {"id": 0, "d": {"a": "ping"}}    → {"msg":"PONG"}
    {"id": 0, "d": {"a": "getlog"}}  → PMLOG (power-event ring buffer)
    {"id": 0, "d": {"a": "i2c_scan"}}

STATUS (every HEARTBEAT_TX_MS on id 0):
    {"msg":"STATUS","hb":n,"acc":0|1,"poco":0|1,"p2":0|1,"pm":"normal",
     "rc":<reset cause>,"up":<uptime s>,"fan":pct,"rpm":n,
     "rly":[null,ch2,...,ch8],"rly_fix":n,"rly_err":n}
    rly: 1 = load powered, read back from the PCF8574 (not a cache); index
    0 (relay 1 = restart relay) is always null. rly_fix counts drift repairs
    (lost write / PCF reset), rly_err counts I2C errors; rly is omitted when
    the read-back itself failed.

The "whoami" identify reply lets the backend tell the powerbox apart from the
gateway over USB (both share ids 1/2), so it can bind each role to its stable
/dev/serial/by-id path regardless of /dev/ttyACM* enumeration order.

Hardware (Waveshare RP2040-Zero), bench state of 2026-11-03:
    GP0/GP1  I2C0: INA219 @0x40 (12 V supply), BMP280 @0x76 (outside box /
             cabin), PCF8574 @0x20 (8-channel relay board). The inside-box
             BMP280 @0x77 and AHT20 @0x38 are NOT fitted; the firmware
             simply omits their fields.
    GP11     ACC / ignition sense — INVERTED logic (active-low):
             ACC ON => LOW (0), ACC OFF => HIGH (1). Internal pull-up enabled.
    GP27     MAIN POCO power button, wired straight across the phone's button
             (line idles at ~1.8 V from the phone's PMIC pull-up). Driven LOW
             = press; input with pulls off = released; NEVER driven high. 6 s
             boots the phone from off (verified 2026-11-03). The main POCO is
             never power-switched: it lives on a permanent buck and is only
             ever started/woken through this button.
    (none)   SECONDARY POCO power button: not connected for now
             (POCO2_BTN_PIN = None). Must never sit on a fan pin.
    GP29     Chassis fan PWM, 25 kHz, straight to the fan's PWM wire: GPIO
             duty = fan duty (NOT inverted, no transistor stage). 0 % stops
             the fan. While the firmware is not driving GP29 (bootloader,
             safe-boot window) the fan runs at full speed.
    GP28     Chassis fan tachometer (open collector, 2 pulses/rev), internal
             pull-up, falling-edge counted.
    GP16     Onboard WS2812 status LED.
    GP14/15  DEAD: pads blown inside the chip (shorted to GND, 11 ohm to each
             other, measured 2026-10-27). Never assign them.

Relay board (HW-281 type, 12 V coils, active-low inputs; IN1..IN8 on PCF
P0..P7, so board relay N = firmware channel N). Opto/input VCC comes from the
RP2040's 5 V pin: whenever the RP2040 is unpowered or restarting, EVERY NO
relay releases (MFD, CAN hub, camera hub drop) and the PCF comes back at 0xFF.
    ch1 NC  powerbox's own supply -> hard_restart only (never a load)
    ch2 NO  MFD Raspberry Pi Zero (backend: ssh poweroff + wait before cut)
    ch3 NO  USB hub: CAN gateway + comma.ai panda (one port, "can_hub")
    ch4 NO  USB hub (VIA): fvision dashcam / aux ("cam_hub")
    ch5 NO  RS485 satellite power (untested on this board, semantics kept)
    ch6-8   unassigned (ch6 reserved for a secondary POCO hub)

Power model: the powerbox, its USB hub and both POCO bucks sit on the
permanent 12 V feed. There is no master rail and no suicide path any more; the
relay board switches the loads above.

A bidirectional rolling-counter heartbeat (this board's STATUS "hb" out, the
main POCO's "hb" in) lets each side detect if the other died: if the POCO stops
heartbeating the firmware wakes it with the power button.
"""

import json
import sys
import time
import select

import machine
from machine import I2C, SoftI2C, Pin
from ina219 import INA219
import pcf8574                      # PCF8574 driver + RelayBoard (pin map, NC, read-back)

# ─── Configuration ────────────────────────────────────────────────────────────

VERSION = "2.1.0"

# Device role — reported in the unified identify ("whoami") response and the
# ready banner so the computer can discover which USB-CDC port is the powerbox
# vs the gateway regardless of enumeration order.
ROLE = "powerbox"

# Local device IDs (computer adds +200 offset via MultiInputPort)
ID_SYSTEM = 0
ID_TELEMETRY = 1
ID_EVENTS = 2

# Default telemetry interval in milliseconds
DEFAULT_INTERVAL_MS = 1000

# Ignition (ACC) sense pin.
#
# GP11 carries the INVERTED logic level of the Prius accessory line:
#   ACC ON  -> pin reads LOW  (0)
#   ACC OFF -> pin reads HIGH (1)
# We enable the internal pull-up so the line idles HIGH (= ACC OFF) when the
# accessory feed is not driving it low.
ACC_PIN = 11

# Re-send the ACC event at least this often even when it has not changed, so the
# computer's virtual twin recovers the state after a reconnect / restart.
ACC_HEARTBEAT_MS = 5000

# Re-announce identity (IDENT on the system channel) at least this often. This
# makes the device discoverable by the computer at ANY time — not just at boot —
# so role auto-discovery and reconnection resync work even when the firmware was
# unblocked mid-run (e.g. after a USB power-cycle) and never re-sent its banner.
IDENT_HEARTBEAT_MS = 10000

# Hardware watchdog timeout (ms). The RP2040 WDT maxes out at ~8.3 s. We feed it
# every main-loop iteration; if the loop ever stalls — most importantly on a
# blocked USB-CDC stdout.write when the host stops draining — the watchdog
# resets the MCU within this window instead of freezing forever. Must stay
# larger than any single in-loop sleep (telemetry interval + error backoff).
WDT_TIMEOUT_MS = 8000

# Safe boot delay — allows remote interrupt via Ctrl-C before main loop
SAFE_BOOT_DELAY_S = 3

# ─── Relay board (PCF8574) ────────────────────────────────────────────────────
#
# Pin map, NC flags and the read-back/re-assert logic live in pcf8574.py
# (RelayBoard), pure Python so they are unit-tested on the host. Board relay N
# = PCF pin P(N-1) = firmware channel N. Only relay 1 is wired NC and it is the
# powerbox's own supply (restart relay; not reachable through "relay").
# PCF8574 power-up state is 0xFF (all coils released) so every NO load comes up
# OFF after any restart until the backend re-asserts it.
RELAY_CH_MFD = 2          # MFD Raspberry Pi Zero
RELAY_CH_CAN_HUB = 3      # USB hub: CAN gateway + comma.ai panda
RELAY_CH_CAM_HUB = 4      # USB hub (VIA): fvision dashcam / aux
RELAY_CH_RS485 = 5        # RS485 satellite power
RELAY_CH_POCO2_HUB = 6    # reserved, unassigned
# Delay between the hard_restart ack and energising the restart relay, so the
# ack can leave the CDC FIFO before our USB disappears.
RESTART_ACK_DELAY_MS = 300

# ─── POCO power buttons ───────────────────────────────────────────────────────
#
# Each button pin is wired straight across the phone's power button. The phone
# holds the line at ~1.8 V through its PMIC pull-up and triggers when the line
# is pulled to GND. We "press" by driving the pin LOW; at ALL other times the
# pin is an input with pulls off. Never drive it HIGH (3.3 V into a 1.8 V node).
#
# GP27 is an ADC pad (GP26-29), which resets with input disabled and no pulls,
# so the button is untouched through the bootloader and WDT resets. Verified
# on-bench 2026-11-03: idle ~1.8 V, a 6 s LOW boots the phone from off.
# The secondary POCO's button is NOT connected for now (None disables it:
# commands for poco 2 are refused with ERROR POCO2_NC). When it comes back,
# assign a free non-fan, non-dead pin here — never GP28/GP29 (fan) or GP14/15.
POCO1_BTN_PIN = 27
POCO2_BTN_PIN = None
POCO_BTN_PRESS_MS = 6000      # power-on hold (POCO F1 board: 3 s fails, 6 s boots)
POCO_BTN_FORCE_MS = 35000     # forced hard power-cycle hold (POCO F1: ~30-40 s)
POCO_BTN_MAX_MS = 40000

# Secondary POCO: it has permanent power and is normally off. Its ACC-follower
# state machine (boot on key-on, key-off grace, clean shutdown over SSH) lives
# in the BACKEND on the main POCO, mirroring the MFD Pi manager
# (cyberpunk_computer/backend/mfd_power.py): the backend can ping and SSH the
# secondary, this board cannot. The firmware only provides the primitives:
#   {"a":"button","poco":2,"ms":6000}   press (power on / force)
#   {"a":"p2","alive":true|false}       host-reported liveness, echoed in STATUS
# The board never presses the secondary's button on its own.
POCO2_ALIVE_TIMEOUT_MS = 90000

# ─── Chassis fan (4-wire BLDC, ADDA AB07505HX110B00, 5 V 0.40 A) ─────────────
#
# PWM on GP29 straight to the fan's PWM wire at 25 kHz (the standard 4-wire
# fan frequency), NOT inverted: GPIO duty = fan duty. 0 % stops this fan
# completely, 5 % already turns it (~255 RPM), 100 % = ~4680 RPM (bench map
# 2026-11-03 in RELAY-CHANGE-PLAN.md / README). While GP29 is not driven
# (bootloader, the 3 s safe-boot window) the fan runs at full speed, so the
# PWM is set up as early as possible in main(). Tach on GP28: open collector,
# 2 pulses per revolution, internal pull-up, counted on falling edges.
FAN_PWM_PIN = 29
FAN_TACH_PIN = 28
FAN_PWM_FREQ = 25000
FAN_PWM_INVERT = False        # True only with an open-drain (2N7000) stage
FAN_PULSES_PER_REV = 2
FAN_BOOT_PCT = 0              # the backend raises it as temperatures demand


def fan_duty_u16(pct, invert=FAN_PWM_INVERT):
    """GPIO duty (0-65535) for a fan duty in percent (clamped to 0-100)."""
    pct = 0 if pct < 0 else (100 if pct > 100 else int(pct))
    if invert:
        pct = 100 - pct
    return pct * 65535 // 100

# ─── Main POCO heartbeat / wake ladder ────────────────────────────────────────
#
# We emit a rolling counter in every STATUS message (every HEARTBEAT_TX_MS) so
# the POCO can detect if we die; the POCO sends its own rolling counter back
# ({"a":"hb","n":N}). If the POCO's counter stops advancing for
# POCO_HB_TIMEOUT_MS we consider it dead.
HEARTBEAT_TX_MS = 2000
POCO_HB_TIMEOUT_MS = 15000
# How long after our own boot to wait for an ALREADY-running POCO to prove
# itself (heartbeat) before concluding it is off and pressing. A running
# backend heartbeats every 2 s; 20 s covers a warm RP2040 reboot + USB-CDC
# re-enumeration.
POCO_BOOT_GRACE_MS = 20000
# After a power-button press, wait this long before pressing again. MUST exceed
# the POCO's worst-case boot -> first heartbeat time (~3-5 min: pmOS boot +
# backend start), or the ladder kills a booting phone before it can ever report
# alive — proven on-bench 2026-08-10.
POCO_WAKE_COOLDOWN_MS = 300000
# Wake escalation: a power-on press only boots a POCO that is OFF. A POCO whose
# SoC has FROZEN (kernel hang — observed 2x on 2026-07-10) ignores short presses;
# only a long (~35 s) forced power-cycle recovers it. After this many wake
# presses with no heartbeat recovery, one force press fires and the ladder
# restarts with normal presses (a force press leaves an OFF phone off).
POCO_WAKE_SHORT_TRIES = 2

# Firmware-local under-voltage backstop. The backend's UndervoltageProtectionRule
# (10.5 V / 5 s) normally handles low voltage gracefully by shutting the POCO
# down. This is the LAST-RESORT backstop for when the backend is dead: if the
# rail sits below SUICIDE_VOLTAGE for SUICIDE_CONFIRM_MS we send SHUTDOWN and
# stop waking the POCO (sleep) until ACC rises again. We cannot cut power any
# more; an off phone draws next to nothing, which is the point.
SUICIDE_VOLTAGE = 10.0
SUICIDE_CONFIRM_MS = 20000
# Default grace for a shutdown (POCO OS shutdown window), used when no grace is
# supplied with the "off" command.
SHUTDOWN_GRACE_S = 30
# Floor before treating heartbeat loss as "shut down". The POCO's heartbeats
# come from its backend service, which stops at the START of an OS halt.
SHUTDOWN_MIN_HOLD_MS = 25000

# ─── Helpers ──────────────────────────────────────────────────────────────────

# USB-CDC TX error counter (diagnostic only — TX failures never reset the MCU).
_tx_errors = 0

# Host-presence gate for outbound writes.
#
# The main POCO (our USB host) is OFF while we are trying to power it on, and
# *during the phone's own boot* its USB attaches before Linux opens the ACM — a
# window where the CDC IN endpoint is connected but nobody is draining it. A
# blocking sys.stdout.write() then stalls until the 8 s WDT resets us. So we
# only write once the host has PROVEN it is present and draining — i.e. a
# recent POCO heartbeat (see PowerManager.poco_alive / on_poco_hb). While the
# host is absent we drop outbound frames: nobody is listening anyway, and the
# in-RAM event log (getlog/PMLOG) still records what happened for later readout.
_host_present = False

# The boot banner (POWERBOX_READY) is sent before any heartbeat can have
# arrived, so the gate drops it. Keep it here and send it once, on the first
# host-present edge, so the backend reliably sees exactly one banner per
# firmware boot (it re-asserts its wanted relay state on that banner).
_pending_banner = None

# machine.reset_cause() of this boot (None if unavailable); reported in the
# banner, in STATUS and in the "boot" event so spontaneous resets can be told
# apart: 1 = power-on/brown-out/hard_restart (PWRON), 3 = watchdog (WDT
# timeout, machine.reset(), mpremote reset).
_reset_cause = None
_boot_ticks = 0


def uptime_s():
    return time.ticks_diff(time.ticks_ms(), _boot_ticks) // 1000


def set_host_present(present):
    """Open/close the outbound-write gate. Called from the power state machine
    as the POCO's heartbeat liveness changes."""
    global _host_present, _tx_errors, _pending_banner
    present = bool(present)
    if present != _host_present:
        _host_present = present
        if present:
            _tx_errors = 0
            if _pending_banner is not None:
                banner, _pending_banner = _pending_banner, None
                tx(ID_SYSTEM, banner)


def tx(device_id: int, payload: dict):
    """Send an NDJSON message to the host via USB-CDC (stdout).

    Gated on host presence (``_host_present``): dropped when the POCO host is
    not draining the port, so a blocking write can never stall the loop and
    trip the WDT while the phone is off or booting. Returns True when written.
    """
    global _tx_errors
    if not _host_present:
        return False
    try:
        msg = json.dumps({"id": device_id, "d": payload})
        sys.stdout.write(msg + "\n")
        _tx_errors = 0  # reset on success
        return True
    except Exception:
        _tx_errors += 1
        return False


def tx_error(code: str, detail: str = ""):
    """Send an error message on the system channel."""
    tx(ID_SYSTEM, {"msg": "ERROR", "code": code, "detail": detail})


def tx_ident():
    """Send the unified identity on the system channel (id 0).

    Shared shape with the gateway so the computer can discover which USB-CDC
    port is which: ``{"msg":"IDENT","role":<role>,"ver":<version>}``.
    """
    tx(ID_SYSTEM, {"msg": "IDENT", "role": ROLE, "ver": VERSION})


def tx_ack(action: str):
    """Send a command acknowledgement on the system channel."""
    tx(ID_SYSTEM, {"ack": action})


# ─── Power-event log (RAM ring buffer) ────────────────────────────────────────
#
# The host that logs powerbox messages (the POCO's prius-backend) is OFF while
# we are booting it — so nothing records what the firmware does until AFTER the
# POCO is up. This in-RAM ring buffer records the key power events (boot, every
# button press with reason + voltage, shutdown, sleep, under-voltage trip) with
# their uptime timestamp; the backend fetches it with "getlog" once it is up.
_evlog = []
EVLOG_MAX = 48


def log_event(kind, **fields):
    """Append a power event to the RAM ring buffer (and echo it live via tx)."""
    ev = {"t_ms": time.ticks_ms(), "ev": kind}
    ev.update(fields)
    _evlog.append(ev)
    if len(_evlog) > EVLOG_MAX:
        del _evlog[0]
    tx(ID_SYSTEM, dict(ev, msg="PMEVENT"))


def tx_pmlog():
    """Dump the whole power-event ring buffer as one PMLOG system message."""
    tx(ID_SYSTEM, {"msg": "PMLOG", "now_ms": time.ticks_ms(),
                   "ver": VERSION, "events": list(_evlog)})


def _truthy(value) -> bool:
    """Tolerant truthiness for inbound command flags (bool / int / 'on'/'off')."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


# ─── Watchdog ─────────────────────────────────────────────────────────────────

_wdt = None


def setup_wdt():
    """Start the hardware watchdog (best-effort)."""
    global _wdt
    try:
        from machine import WDT
        _wdt = WDT(timeout=WDT_TIMEOUT_MS)
        tx(ID_SYSTEM, {"msg": "WDT_ON", "timeout_ms": WDT_TIMEOUT_MS})
    except Exception as e:
        _wdt = None
        tx_error("WDT_INIT", str(e))


def feed_wdt():
    """Pet the watchdog if it is running."""
    if _wdt is not None:
        try:
            _wdt.feed()
        except Exception:
            pass


def wdt_sleep_ms(total_ms):
    """Sleep while keeping the watchdog fed (chunked so long waits don't trip it)."""
    remaining = total_ms
    while remaining > 0:
        feed_wdt()
        step = 2000 if remaining > 2000 else remaining
        time.sleep_ms(step)
        remaining -= step
    feed_wdt()


# ─── Status LED (RP2040-Zero onboard WS2812 on GP16) ──────────────────────────
#
# The only way to see what the firmware does while the USB host (the main
# POCO) is off. Colours are deliberately dim. Colour key:
#   boot flash  white->blue  — a RESET LOOP shows this flashing every ~8 s
#   green blink  normal, POCO alive (healthy heartbeat)
#   amber solid  normal, POCO NOT alive — armed to press the wake button
#   RED solid    a power-button press is happening RIGHT NOW
#   magenta      shutdown in progress
#   cyan solid   sleep (host shut itself down; no wake until ACC rises)
#   blue solid   running without I2C sensors (no INA/BMP)
LED_PIN = 16
LED_ALIVE = (0, 14, 0)
LED_WAIT = (16, 10, 0)
LED_PRESS = (70, 0, 0)
LED_SHUTDOWN = (14, 0, 14)
LED_SLEEP = (0, 10, 10)
LED_NOSENSOR = (0, 0, 16)
_np = None


def setup_led():
    """Best-effort init of the onboard WS2812. Stays None if unavailable."""
    global _np
    try:
        import neopixel
        _np = neopixel.NeoPixel(Pin(LED_PIN), 1)
        _np[0] = (0, 0, 0)
        _np.write()
    except Exception:
        _np = None


def set_led(r, g, b):
    if _np is None:
        return
    try:
        _np[0] = (r, g, b)
        _np.write()
    except Exception:
        pass


def led_boot_flash():
    """Distinct power-on/reset signature (white -> blue -> off)."""
    set_led(30, 30, 30)
    time.sleep_ms(150)
    set_led(0, 0, 40)
    time.sleep_ms(150)
    set_led(0, 0, 0)


# ─── I2C Setup ────────────────────────────────────────────────────────────────

def setup_i2c():
    """Try standard pin assignment first, then swapped (SoftI2C fallback)."""
    # Standard: SDA=GP0, SCL=GP1
    try:
        i2c = I2C(0, sda=Pin(0), scl=Pin(1), freq=400000)
        devices = i2c.scan()
        if devices:
            tx(ID_SYSTEM, {
                "msg": "I2C_OK",
                "pins": "SDA=0,SCL=1",
                "addrs": [hex(d) for d in devices],
            })
            return i2c, devices
    except Exception:
        pass

    # Swapped: SDA=GP1, SCL=GP0 (software I2C)
    try:
        i2c = SoftI2C(sda=Pin(1), scl=Pin(0), freq=400000)
        devices = i2c.scan()
        if devices:
            tx(ID_SYSTEM, {
                "msg": "I2C_OK",
                "pins": "SDA=1,SCL=0(soft)",
                "addrs": [hex(d) for d in devices],
            })
            return i2c, devices
    except Exception:
        pass

    tx_error("I2C_FAIL", "No devices found on either pin configuration")
    return None, []


# ─── Ignition (ACC) Sense ────────────────────────────────────────

def setup_acc_pin():
    """Configure the ACC sense GPIO with an internal pull-up.

    The accessory feed drives GP11 LOW when ACC is ON (inverted logic); the
    pull-up keeps it HIGH (= ACC OFF) when the line is released.
    """
    return Pin(ACC_PIN, Pin.IN, Pin.PULL_UP)


def read_acc(acc_pin) -> bool:
    """Return True when ACC/ignition is ON. GP11 is active-low (inverted)."""
    return acc_pin.value() == 0


# ─── Chassis fan ──────────────────────────────────────────────────────────────

class Fan:
    """4-wire fan: 25 kHz PWM straight on the fan's PWM wire, tach RPM by IRQ."""

    def __init__(self):
        self.pwm = None
        self.tach = None
        self.pct = None
        self.rpm = 0
        self._pulses = 0
        self._last_rpm_ms = time.ticks_ms()

    def setup(self):
        try:
            self.pwm = machine.PWM(Pin(FAN_PWM_PIN))
            self.pwm.freq(FAN_PWM_FREQ)
            self.set_pct(FAN_BOOT_PCT)
            tx(ID_SYSTEM, {"msg": "FAN_ON", "pin": FAN_PWM_PIN,
                           "freq": self.pwm.freq(), "pct": self.pct})
        except Exception as e:
            self.pwm = None
            tx_error("FAN_INIT", str(e))
        try:
            self.tach = Pin(FAN_TACH_PIN, Pin.IN, Pin.PULL_UP)
            self.tach.irq(trigger=Pin.IRQ_FALLING, handler=self._on_pulse)
        except Exception as e:
            self.tach = None
            tx_error("TACH_INIT", str(e))

    def _on_pulse(self, pin):
        self._pulses += 1

    def set_pct(self, pct) -> bool:
        """Set fan speed 0-100 %. GPIO duty = fan duty (see FAN_PWM_INVERT)."""
        try:
            pct = int(pct)
        except (TypeError, ValueError):
            return False
        pct = 0 if pct < 0 else (100 if pct > 100 else pct)
        if self.pwm is None:
            return False
        self.pwm.duty_u16(fan_duty_u16(pct))
        self.pct = pct
        return True

    def update_rpm(self):
        """Recompute RPM from the pulses counted since the last call (>= 0.5 s)."""
        now = time.ticks_ms()
        dt = time.ticks_diff(now, self._last_rpm_ms)
        if dt < 500:
            return self.rpm
        st = machine.disable_irq()
        n = self._pulses
        self._pulses = 0
        machine.enable_irq(st)
        self._last_rpm_ms = now
        self.rpm = int(n * 60000 // (dt * FAN_PULSES_PER_REV))
        return self.rpm


_fan = None


# ─── POCO power buttons ───────────────────────────────────────────────────────

class Button:
    """One phone power button on a GPIO: LOW = pressed, input/no-pulls = released.

    ``pin_no=None`` = button not connected: ``press`` is refused (returns
    False) and no GPIO is ever touched.
    """

    def __init__(self, pin_no, poco):
        self.pin_no = pin_no
        self.poco = poco
        if pin_no is not None:
            Pin(pin_no, Pin.IN, pull=None)   # released, pulls explicitly OFF
        self.last_press_ms = None

    @property
    def connected(self):
        return self.pin_no is not None

    def press(self, ms, reason, **ctx):
        if self.pin_no is None:
            tx_error("POCO%d_NC" % self.poco, "button not connected")
            return False
        log_event("btn", poco=self.poco, ms=ms, why=reason, **ctx)
        tx(ID_SYSTEM, {"msg": "POCO_BTN", "poco": self.poco, "ms": ms, "why": reason})
        set_led(*LED_PRESS)                   # RED = pressing NOW
        Pin(self.pin_no, Pin.OUT, value=0)    # pull the button line to GND
        wdt_sleep_ms(ms)                      # feeds the WDT during the hold
        Pin(self.pin_no, Pin.IN, pull=None)   # release -> input, pulls off
        set_led(0, 0, 0)
        self.last_press_ms = time.ticks_ms()
        return True


def clamp_press_ms(ms):
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        ms = POCO_BTN_PRESS_MS
    if ms < 100:
        ms = POCO_BTN_PRESS_MS
    if ms > POCO_BTN_MAX_MS:
        ms = POCO_BTN_MAX_MS
    return ms


# Module-global PowerManager so process_command (driven from poll_stdin) can
# route power commands to it without threading it through.
_pm = None


class PowerManager:
    """Both POCO buttons, the main POCO heartbeat and the ACC-following logic.

    States:
        "normal"   running; wakes a dead main POCO via its button.
        "shutdown" the host said it is shutting down (or the under-voltage
                   backstop fired); waits for the heartbeat to stop or the
                   grace timeout, then goes to sleep. Does NOT wake the POCO.
        "sleep"    the main POCO is deliberately off. No wake presses until ACC
                   rises again or a "wake" command arrives.
    """

    def __init__(self):
        self.btn1 = Button(POCO1_BTN_PIN, 1)
        self.btn2 = Button(POCO2_BTN_PIN, 2)   # pin None = not connected
        # Relay board (pcf8574.RelayBoard); attached after I2C setup, None
        # when the expander is absent.
        self.relays = None
        # Pending hard restart: ticks at which to energise the restart relay.
        self.restart_due = None
        self.state = "normal"
        now = time.ticks_ms()
        self.boot_ms = now
        self.boot_grace_ms = POCO_BOOT_GRACE_MS
        # Our outbound rolling heartbeat counter (carried in STATUS).
        self.hb_tx = 0
        self.last_hb_tx_ms = now
        # Main POCO inbound heartbeat liveness — the counter must keep CHANGING.
        self.seen_poco = False
        self.last_poco_n = None
        self.last_poco_change_ms = now
        # Wake ladder.
        self.last_btn_ms = time.ticks_add(now, -POCO_WAKE_COOLDOWN_MS)
        self.wake_tries = 0           # consecutive wake presses without recovery
        self.poco_should_run = True
        # Queued button presses from the host: list of (poco, ms).
        self.pending = []
        # ACC edge tracking (None until the first reading).
        self.acc_on = None
        # Secondary POCO liveness as reported by the host.
        self.p2_alive_ms = None
        # Shutdown.
        self.shutdown_t0 = 0
        self.shutdown_grace_s = SHUTDOWN_GRACE_S
        # Under-voltage backstop timer.
        self.low_since = None
        # Last INA219 bus voltage seen in tick() (for event-log context).
        self.last_voltage = None

    # -- inbound from the host ------------------------------------------------
    def on_poco_hb(self, n):
        """Record a heartbeat from the main POCO. Liveness tracks counter
        CHANGES so a stuck (repeating) counter still reads as dead."""
        self.seen_poco = True
        now = time.ticks_ms()
        if n != self.last_poco_n:
            self.last_poco_n = n
            self.last_poco_change_ms = now
        # A heartbeat is proof the host has the port open and is draining it —
        # open the outbound-write gate immediately.
        set_host_present(True)

    def on_p2_alive(self, alive):
        """Host-reported liveness of the secondary POCO."""
        self.p2_alive_ms = time.ticks_ms() if alive else None

    def poco_alive(self, now=None):
        if not self.seen_poco:
            return False
        if now is None:
            now = time.ticks_ms()
        return time.ticks_diff(now, self.last_poco_change_ms) < POCO_HB_TIMEOUT_MS

    def p2_alive(self, now=None):
        if self.p2_alive_ms is None:
            return False
        if now is None:
            now = time.ticks_ms()
        return time.ticks_diff(now, self.p2_alive_ms) < POCO2_ALIVE_TIMEOUT_MS

    def on_acc(self, acc_on):
        """Feed the ACC reading every loop; acts on edges only."""
        acc_on = bool(acc_on)
        if self.acc_on is None:
            self.acc_on = acc_on          # first reading: no edge
            return
        if acc_on == self.acc_on:
            return
        self.acc_on = acc_on
        if acc_on:
            log_event("acc_on", pm=self.state)
            if self.state == "sleep":
                self.wake("acc_on")
        else:
            log_event("acc_off", pm=self.state)

    def wake(self, reason):
        """Leave sleep and re-arm the wake ladder (press after the boot grace
        unless a heartbeat shows up first)."""
        if self.state == "sleep":
            self.state = "normal"
        self.poco_should_run = True
        now = time.ticks_ms()
        self.boot_ms = now
        self.wake_tries = 0
        self.last_btn_ms = time.ticks_add(now, -POCO_WAKE_COOLDOWN_MS)
        self.low_since = None
        log_event("wake", why=reason)

    def button_for(self, poco):
        """Button object for ``poco`` (1|2), or None when not connected."""
        btn = self.btn2 if poco in (2, "2") else self.btn1
        return btn if btn.connected else None

    def request_button(self, poco, ms) -> bool:
        """Queue a power-button press (executed on the next tick). Returns
        False (and reports ERROR POCOn_NC) when that button is not connected."""
        poco = 2 if poco in (2, "2") else 1
        if self.button_for(poco) is None:
            tx_error("POCO%d_NC" % poco, "button not connected")
            return False
        self.pending.append((poco, clamp_press_ms(ms)))
        return True

    # -- relays -----------------------------------------------------------------
    def set_relay(self, ch, on) -> bool:
        """Power (``on``) a load relay channel. Returns False when refused.

        Generic NO/NC handling and the restart-relay lock-out live in
        pcf8574.RelayBoard; the wanted state is only recorded after a
        successful I2C write and verified against a read-back every STATUS.
        """
        if self.relays is None:
            tx_error("RELAY", "no PCF8574 expander")
            return False
        try:
            ch = int(ch)
        except (TypeError, ValueError):
            tx_error("RELAY", "bad channel %s" % ch)
            return False
        ok, why = self.relays.set(ch, on)
        if not ok:
            tx_error("RELAY", why)
        return ok

    def request_hard_restart(self) -> bool:
        """Schedule the self-power-cut (restart relay) for the next tick."""
        if self.relays is None:
            tx_error("RELAY", "no PCF8574 expander")
            return False
        if self.restart_due is None:
            self.restart_due = time.ticks_add(time.ticks_ms(), RESTART_ACK_DELAY_MS)
            log_event("hard_restart", v=self.last_voltage, up=uptime_s())
        return True

    def relay_states(self):
        """[ch1..ch8] of 0/1/None (1 = load powered) read back from the chip;
        None without expander or when the read failed. The restart channel
        reports None."""
        if self.relays is None:
            return None
        return self.relays.states()

    # -- shutdown / sleep -------------------------------------------------------
    def begin_shutdown(self, grace_s, reason):
        if self.state != "normal":
            return
        self.state = "shutdown"
        self.poco_should_run = False          # don't fight the shutdown
        self.shutdown_t0 = time.ticks_ms()
        self.shutdown_grace_s = grace_s if grace_s and grace_s > 0 else SHUTDOWN_GRACE_S
        log_event("shutdown", why=reason, grace_s=self.shutdown_grace_s,
                  v=self.last_voltage)
        tx(ID_SYSTEM, {"msg": "SHUTDOWN", "reason": reason, "grace_s": self.shutdown_grace_s})

    def _sleep(self, reason):
        log_event("sleep", why=reason, v=self.last_voltage)
        tx(ID_SYSTEM, {"msg": "SLEEP", "reason": reason})
        self.state = "sleep"

    def _tx_status(self, now):
        payload = {
            "msg": "STATUS",
            "hb": self.hb_tx,
            "acc": 1 if self.acc_on else 0,
            "poco": 1 if self.poco_alive(now) else 0,
            "p2": 1 if self.p2_alive(now) else 0,
            "pm": self.state,
            "rc": _reset_cause,
            "up": uptime_s(),
        }
        if _fan is not None:
            payload["fan"] = _fan.pct
            payload["rpm"] = _fan.rpm
        if self.relays is not None:
            rly = self.relays.states()
            if rly is not None:
                payload["rly"] = rly
            payload["rly_fix"] = self.relays.fix_count
            payload["rly_err"] = self.relays.err_count
        tx(ID_SYSTEM, payload)
        self.hb_tx = (self.hb_tx + 1) & 0xFF

    def _update_led(self, now):
        if self.state == "sleep":
            set_led(*LED_SLEEP)
        elif self.state == "shutdown":
            set_led(*LED_SHUTDOWN)
        elif self.poco_alive(now):
            set_led(*(LED_ALIVE if (self.hb_tx & 1) else (0, 0, 0)))  # blink green
        else:
            set_led(*LED_WAIT)  # amber = normal, no POCO heartbeat, will press

    def _press(self, poco, ms, reason):
        btn = self.button_for(poco)
        if btn is None:
            tx_error("POCO%d_NC" % poco, "button not connected")
            return
        boot_age = time.ticks_diff(time.ticks_ms(), self.boot_ms)
        btn.press(ms, reason, wt=self.wake_tries, boot_s=boot_age // 1000,
                  sp=1 if self.seen_poco else 0, v=self.last_voltage)
        if poco == 1:
            self.last_btn_ms = time.ticks_ms()

    def _do_hard_restart(self):
        """Energise the restart relay. We lose power ~0.4 s later; if we are
        still alive after RESTART_RELEASE_MS the RelayBoard releases the coil
        and reports RESTART_FAILED on the next read-back."""
        self.restart_due = None
        if self.relays is None:
            return
        tx(ID_SYSTEM, {"msg": "HARD_RESTART", "up": uptime_s()})
        set_led(70, 0, 70)                     # bright magenta = cutting power
        if not self.relays.restart():
            set_led(0, 0, 0)

    def tick(self, voltage):
        """Run the power state machine for one main-loop iteration.

        ``voltage`` is the INA219 bus voltage (V), or None when unavailable.
        """
        if voltage is not None:
            self.last_voltage = voltage
        # Drive the outbound-write gate from POCO liveness.
        set_host_present(self.poco_alive())
        now = time.ticks_ms()

        # 1) Heartbeat / status TX (rolling counter).
        if time.ticks_diff(now, self.last_hb_tx_ms) >= HEARTBEAT_TX_MS:
            self.last_hb_tx_ms = now
            self._tx_status(now)
            self._update_led(now)

        # 2) Execute queued host-commanded presses.
        while self.pending:
            poco, ms = self.pending.pop(0)
            self._press(poco, ms, "cmd")
            now = time.ticks_ms()

        # 2b) Relay board: pending hard restart, drift check between STATUS.
        if self.restart_due is not None and \
                time.ticks_diff(now, self.restart_due) >= 0:
            self._do_hard_restart()
        elif self.relays is not None and self.relays.restart_at is not None:
            self.relays.tick()               # notices a failed cut, releases

        # 3) Under-voltage backstop (only with a real reading, only while normal).
        if self.state == "normal" and voltage is not None:
            if voltage < SUICIDE_VOLTAGE:
                if self.low_since is None:
                    self.low_since = now
                elif time.ticks_diff(now, self.low_since) >= SUICIDE_CONFIRM_MS:
                    self.begin_shutdown(SHUTDOWN_GRACE_S, "undervoltage %.2fV" % voltage)
            else:
                self.low_since = None

        # 4) State machine for the main POCO.
        if self.state == "shutdown":
            elapsed = time.ticks_diff(now, self.shutdown_t0)
            poco_down = not self.poco_alive(now)
            timed_out = elapsed >= self.shutdown_grace_s * 1000
            min_hold = SHUTDOWN_MIN_HOLD_MS
            if self.shutdown_grace_s * 1000 < min_hold:
                min_hold = self.shutdown_grace_s * 1000
            if (poco_down and elapsed >= min_hold) or timed_out:
                self._sleep("poco_down" if poco_down else "grace_timeout")
        elif self.state == "normal" and self.poco_should_run:
            # Wake a dead main POCO with its button (after boot grace + cooldown).
            # Ladder: power-on presses boot an off POCO; if those never bring
            # the heartbeat back the SoC is likely FROZEN and only a long forced
            # power-cycle recovers it. The cooldown (5 min) exceeds the POCO's
            # boot -> first-heartbeat time so a booting phone is never pressed
            # again before it can report alive.
            past_boot = time.ticks_diff(now, self.boot_ms) >= self.boot_grace_ms
            cooled = time.ticks_diff(now, self.last_btn_ms) >= POCO_WAKE_COOLDOWN_MS
            if self.poco_alive(now):
                self.wake_tries = 0
            elif past_boot and cooled:
                if self.wake_tries < POCO_WAKE_SHORT_TRIES:
                    self._press(1, POCO_BTN_PRESS_MS, "wake")
                    self.wake_tries += 1
                else:
                    # Frozen-SoC recovery: long forced power-cycle. It leaves
                    # the phone OFF, so restart the ladder with normal presses.
                    self._press(1, POCO_BTN_FORCE_MS, "force")
                    self.wake_tries = 0


# ─── Command Processing ──────────────────────────────────────────────────────

class Config:
    """Mutable runtime configuration, adjustable via inbound commands."""
    __slots__ = ("interval_ms", "i2c", "_reset_mah", "_rxbuf")

    def __init__(self):
        self.interval_ms = DEFAULT_INTERVAL_MS
        self.i2c = None  # bus handle for the on-demand i2c_scan diagnostic
        self._reset_mah = False
        self._rxbuf = ""


def process_command(line: str, config: Config):
    """Parse and act on one inbound NDJSON command from the host."""
    try:
        msg = json.loads(line)
    except (ValueError, TypeError):
        return

    device_id = msg.get("id")
    data = msg.get("d")
    if not isinstance(data, dict):
        return

    # Only handle commands addressed to system channel (id 0)
    if device_id != ID_SYSTEM:
        return

    action = data.get("a", "").lower()

    if action == "off":
        # The host is shutting itself down: stop waking it until ACC rises.
        grace_s = int(data.get("grace_s", SHUTDOWN_GRACE_S))
        if _pm is not None:
            _pm.begin_shutdown(grace_s, str(data.get("reason", "host_off")))
        tx_ack("off")

    elif action == "wake":
        if _pm is not None:
            if data.get("poco") in (2, "2"):
                # Secondary: a power-on press (refused while not connected).
                if _pm.request_button(2, POCO_BTN_PRESS_MS):
                    tx_ack("wake")
            else:
                _pm.wake("cmd")
                tx_ack("wake")

    elif action == "button":
        # Pulse a POCO power button (wake / force-reboot). ERROR POCOn_NC
        # instead of an ack when that button is not connected.
        if _pm is not None:
            if _pm.request_button(data.get("poco", 1), data.get("ms", POCO_BTN_PRESS_MS)):
                tx_ack("button")

    elif action == "hb":
        # Main POCO heartbeat (rolling counter).
        if _pm is not None:
            _pm.on_poco_hb(data.get("n"))

    elif action == "p2":
        # Host-reported secondary POCO liveness.
        if _pm is not None:
            _pm.on_p2_alive(_truthy(data.get("alive")))
            tx_ack("p2")

    elif action == "set_interval":
        ms = data.get("ms")
        if isinstance(ms, (int, float)) and ms >= 100:
            config.interval_ms = int(ms)
            tx_ack("set_interval")

    elif action == "ping":
        tx(ID_SYSTEM, {"msg": "PONG"})

    elif action in ("whoami", "identify", "id"):
        tx_ident()

    elif action in ("getlog", "pmlog"):
        tx_pmlog()

    elif action == "reset_mah":
        tx_ack("reset_mah")
        config._reset_mah = True  # the main loop does the actual reset

    elif action == "fan":
        # Chassis fan speed in percent. "duty" (0-65535) is accepted for the
        # old backend message; "pin"/"freq" are ignored (fixed in hardware).
        pct = data.get("pct")
        if pct is None and data.get("duty") is not None:
            try:
                pct = int(data.get("duty")) * 100 // 65535
            except (TypeError, ValueError):
                pct = None
        if _fan is not None and _fan.set_pct(pct):
            tx(ID_SYSTEM, {"msg": "FAN", "pct": _fan.pct, "freq": FAN_PWM_FREQ,
                           "rpm": _fan.rpm})
            tx_ack("fan")
        else:
            tx_error("FAN", "pct=%s" % pct)

    elif action == "relay":
        # Set a load relay channel (2-8). Channel 1 (our own supply) is
        # refused here — only "hard_restart" may energise it.
        if _pm is not None:
            ch = data.get("ch")
            on = _truthy(data.get("on"))
            if _pm.set_relay(ch, on):
                tx_ack("relay_%s_%s" % (ch, "on" if on else "off"))

    elif action == "hard_restart":
        # Cut our own supply via the NC restart relay. All NO relays drop
        # with us; the PCF wakes at 0xFF so power returns by itself (~5 s to
        # the next STATUS). The ack goes out first, the cut follows on the
        # next tick after RESTART_ACK_DELAY_MS.
        if _pm is not None and _pm.request_hard_restart():
            tx_ack("hard_restart")

    elif action == "out":
        tx_error("OUT_REMOVED", "no power rails since firmware 2.0; use relay")

    elif action == "i2c_scan":
        if config.i2c is None:
            tx_error("I2C_SCAN", "no bus")
        else:
            try:
                tx(ID_SYSTEM, {"msg": "I2C_SCAN",
                               "addrs": [hex(d) for d in config.i2c.scan()]})
            except Exception as e:
                tx_error("I2C_SCAN", str(e))


def poll_stdin(config: Config):
    """Non-blocking read of any complete NDJSON lines from USB-CDC stdin."""
    try:
        while sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            if line:
                process_command(line.strip(), config)
    except Exception:
        # select may not be available on all MicroPython ports;
        # fall back to non-blocking read
        try:
            if hasattr(sys.stdin, "read"):
                data = sys.stdin.read(1)  # will return None if nothing
                if data:
                    config._rxbuf += data
                    while "\n" in config._rxbuf:
                        line, config._rxbuf = config._rxbuf.split("\n", 1)
                        process_command(line.strip(), config)
        except Exception:
            pass


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    global _pm, _fan, _reset_cause, _boot_ticks, _pending_banner

    _boot_ticks = time.ticks_ms()

    # Core clock. The RP2040's USB device stack (TinyUSB) is serviced from the
    # main core, so an aggressive underclock leaves the CDC IN endpoint slow to
    # answer host IN tokens (recurring "USB-CDC wedged" stalls behind the hub).
    # 96 MHz keeps a healthy servicing margin at ~2/3 of the 133 MHz default.
    machine.freq(96000000)

    # Fan PWM first: with GP29 undriven the fan runs flat out, so take it over
    # before the LED/banner/safe-boot window. (PWM + the tach IRQ are harmless
    # to the REPL.)
    _fan = Fan()
    _fan.setup()

    # Status LED — the earliest possible visual proof the firmware ran.
    setup_led()
    led_boot_flash()

    # Buttons released (inputs, pulls off) before anything else.
    _pm = PowerManager()
    try:
        _reset_cause = machine.reset_cause()
    except Exception:
        _reset_cause = None
    log_event("boot", ver=VERSION, rc=_reset_cause)

    # Banner — parsed by the computer's parse_powerbox_system(). The TX gate
    # is still closed here (no heartbeat yet), so it is parked and sent on
    # the first host-present edge instead.
    banner = {"msg": "POWERBOX_READY", "ver": VERSION, "role": ROLE,
              "rc": _reset_cause}
    if not tx(ID_SYSTEM, banner):
        _pending_banner = banner

    # Safe boot delay — allows mpremote / Ctrl-C to interrupt
    tx(ID_SYSTEM, {"msg": "SAFE_BOOT", "delay_s": SAFE_BOOT_DELAY_S})
    try:
        for _ in range(SAFE_BOOT_DELAY_S):
            time.sleep(1)
    except KeyboardInterrupt:
        print("Interrupt received during safe boot. Dropping to REPL.")
        sys.exit(0)

    # I2C setup
    i2c, devices = setup_i2c()

    # Start the watchdog only AFTER the interruptible safe-boot window, so a
    # developer can still drop to the REPL without the WDT resetting the board.
    setup_wdt()

    if i2c is None:
        # Even without the INA219/env sensors we still report ignition (ACC),
        # which is the more safety-relevant signal for the computer.
        tx(ID_SYSTEM, {"msg": "IDLE", "reason": "no_sensor"})
        idle_config = Config()
        acc_pin = setup_acc_pin()
        acc_state = read_acc(acc_pin)
        _pm.on_acc(acc_state)
        tx(ID_EVENTS, {"acc": acc_state})
        tx_ident()
        last_acc_tx = time.ticks_ms()
        last_ident_tx = time.ticks_ms()
        while True:
            feed_wdt()
            poll_stdin(idle_config)
            acc_now = read_acc(acc_pin)
            _pm.on_acc(acc_now)
            now_ms = time.ticks_ms()
            if acc_now != acc_state or \
                    time.ticks_diff(now_ms, last_acc_tx) >= ACC_HEARTBEAT_MS:
                acc_state = acc_now
                last_acc_tx = now_ms
                tx(ID_EVENTS, {"acc": acc_state})
            if time.ticks_diff(now_ms, last_ident_tx) >= IDENT_HEARTBEAT_MS:
                last_ident_tx = now_ms
                tx_ident()
            _fan.update_rpm()
            _pm.tick(None)
            time.sleep_ms(500)

    ina = None
    bmp = None
    bmp2 = None
    aht = None

    ina_addr = None
    for d in devices:
        if d in (0x40, 0x41, 0x44, 0x45):
            ina_addr = d
            break
    if ina_addr is not None:
        try:
            ina = INA219(i2c, addr=ina_addr)
            tx(ID_SYSTEM, {"msg": "SENSOR_OK", "chip": "INA219", "addr": hex(ina_addr)})
        except Exception as e:
            tx_error("INA219_INIT", str(e))

    # BMP280: 0x76 (SDO low) is OUTSIDE the box (cabin ambient) → bmp2_t/bmp2_p.
    # 0x77 (SDO high, inside the box → bmp_t/bmp_p) is NOT fitted at the
    # moment; it is picked up automatically if it ever answers on the bus.
    for addr in (0x77, 0x76):
        if addr not in devices:
            continue
        try:
            import bmp280
            unit = bmp280.BMP280(i2c, addr=addr)
            tx(ID_SYSTEM, {"msg": "SENSOR_OK", "chip": "BMP280", "addr": hex(addr)})
            if addr == 0x77:
                bmp = unit
            else:
                bmp2 = unit
        except Exception as e:
            tx_error("BMP280_INIT", "%s@%s" % (str(e), hex(addr)))

    # AHT20 (address 0x38) — not fitted at the moment; same auto-detection.
    if 0x38 in devices:
        try:
            import ahtx0
            aht = ahtx0.AHT20(i2c)
            tx(ID_SYSTEM, {"msg": "SENSOR_OK", "chip": "AHT20", "addr": "0x38"})
        except Exception as e:
            tx_error("AHT20_INIT", str(e))

    # PCF8574 relay expander. 0x20-0x27 is the plain PCF8574, 0x39-0x3F the
    # PCF8574A (0x38 excluded — that's the AHT20).
    pcf_addr = None
    for d in devices:
        if 0x20 <= d <= 0x27 or (0x39 <= d <= 0x3F):
            pcf_addr = d
            break
    if pcf_addr is not None:
        try:
            # RelayBoard adopts the chip's current latch, so relay state is
            # RETAINED across MCU warm reboots (no glitch on reflash/reset). On
            # a true cold power-up (or a hard_restart) the PCF8574 wakes at
            # 0xFF = all coils released = all NO loads OFF; the backend
            # re-asserts them on the POWERBOX_READY banner.
            _pm.relays = pcf8574.RelayBoard(
                pcf8574.PCF8574(i2c, address=pcf_addr),
                time.ticks_ms, time.ticks_diff,
                on_event=log_event, on_error=tx_error)
            tx(ID_SYSTEM, {"msg": "SENSOR_OK", "chip": "PCF8574", "addr": hex(pcf_addr),
                           "port": _pm.relays.wanted})
        except Exception as e:
            tx_error("PCF8574_INIT", str(e))

    # Telemetry loop
    config = Config()
    config.i2c = i2c
    total_mah = 0.0
    last_time = time.ticks_ms()
    error_streak = 0

    # Ignition (ACC) sense on GP11 (inverted logic, active-low)
    acc_pin = setup_acc_pin()
    acc_state = read_acc(acc_pin)
    _pm.on_acc(acc_state)
    last_acc_tx = time.ticks_ms()
    last_ident_tx = time.ticks_ms()
    # Announce the initial ACC state so the computer's virtual twin starts in sync
    tx(ID_EVENTS, {"acc": acc_state})
    tx_ident()

    while True:
        try:
            feed_wdt()

            # Process any inbound commands (non-blocking)
            poll_stdin(config)

            # Handle mAh reset command
            if config._reset_mah:
                total_mah = 0.0
                config._reset_mah = False

            # Read ignition (ACC) on GP11 — emit on change or as a heartbeat
            acc_now = read_acc(acc_pin)
            _pm.on_acc(acc_now)
            now_ms = time.ticks_ms()
            if acc_now != acc_state or \
                    time.ticks_diff(now_ms, last_acc_tx) >= ACC_HEARTBEAT_MS:
                acc_state = acc_now
                last_acc_tx = now_ms
                tx(ID_EVENTS, {"acc": acc_state})

            # Periodic identity so the computer can (re)discover us at any time
            if time.ticks_diff(now_ms, last_ident_tx) >= IDENT_HEARTBEAT_MS:
                last_ident_tx = now_ms
                tx_ident()

            payload = {}
            volt = None

            # Read INA219
            if ina is not None:
                v = ina.bus_voltage     # Volts
                volt = v
                i_ma = ina.current      # mA
                # Coulomb counting
                t_now = time.ticks_ms()
                dt_s = time.ticks_diff(t_now, last_time) / 1000.0
                last_time = t_now
                total_mah += i_ma * (dt_s / 3600.0)
                payload.update({
                    "v": round(v, 3),
                    "ma": round(i_ma, 1),
                    "mah": round(total_mah, 3),
                })
            else:
                last_time = time.ticks_ms()

            # Env sensors, each guarded on its own: a sensor that stops
            # answering is dropped after one ERROR instead of taking the
            # whole telemetry loop into the READ error backoff.
            if bmp is not None:
                try:
                    payload["bmp_t"] = round(bmp.temperature, 2)
                    payload["bmp_p"] = round(bmp.pressure, 2)
                except Exception as e:
                    tx_error("BMP280_READ", "0x77 dropped: %s" % e)
                    bmp = None
            if bmp2 is not None:
                try:
                    payload["bmp2_t"] = round(bmp2.temperature, 2)
                    payload["bmp2_p"] = round(bmp2.pressure, 2)
                except Exception as e:
                    tx_error("BMP280_READ", "0x76 dropped: %s" % e)
                    bmp2 = None
            if aht is not None:
                try:
                    payload["aht_t"] = round(aht.temperature, 2)
                    payload["aht_h"] = round(aht.relative_humidity, 2)
                except Exception as e:
                    tx_error("AHT20_READ", "dropped: %s" % e)
                    aht = None

            # Chassis fan: commanded duty and measured RPM
            if _fan.pwm is not None:
                payload["fan_pct"] = _fan.pct
            if _fan.tach is not None:
                payload["fan_rpm"] = _fan.update_rpm()

            # Telemetry message (device 201 on computer side)
            if payload:
                tx(ID_TELEMETRY, payload)

            # Power management: heartbeat TX, under-voltage backstop, POCO
            # wake ladder, ACC-following secondary, shutdown->sleep.
            _pm.tick(volt)

            error_streak = 0
            time.sleep_ms(config.interval_ms)

        except KeyboardInterrupt:
            tx(ID_SYSTEM, {"msg": "INTERRUPTED"})
            sys.exit(0)

        except Exception as e:
            error_streak += 1
            tx_error("READ", str(e))
            last_time = time.ticks_ms()
            # Back off on persistent errors (feeding the watchdog so the backoff
            # itself doesn't trip a reset).
            wdt_sleep_ms(min(2000 * error_streak, 6000))
            if error_streak > 30:
                tx_error("FATAL", "Too many consecutive read errors")
                machine.reset()


if __name__ == "__main__":
    main()
