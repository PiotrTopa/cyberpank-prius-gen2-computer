"""
Powerbox RP2040 Firmware — NDJSON protocol over USB-CDC.

Communicates with the POCO backend (cyberpunk_computer) using the same
NDJSON envelope format as the gateway and satellites:

    {"id": <device_id>, "d": <payload>}

Device IDs (offset by DEVICE_POWERBOX_BASE=200 on the computer side):
    0  → 200  System: ready banner, acks, errors
    1  → 201  Telemetry: INA219 voltage / current / accumulated mAh
    2  → 202  Events: ignition (ACC) state

The computer side remaps these via MultiInputPort id_offset=200.
On the wire (this firmware) we use the LOCAL ids 0/1/2.

Inbound commands from the computer (device 0):
    {"id": 0, "d": {"a": "off", "reason": "...", "grace_s": 30}}  shutdown->suicide
    {"id": 0, "d": {"a": "set_interval", "ms": 1000}}
    {"id": 0, "d": {"a": "whoami"}}  → {"msg":"IDENT","role":"powerbox","ver":..}
    {"id": 0, "d": {"a": "ping"}}    → {"msg":"PONG"}
    {"id": 0, "d": {"a": "hb", "n": 0-255}}   POCO heartbeat (rolling counter)
    {"id": 0, "d": {"a": "out", "ch": 2|3, "on": true|false}}  set OUT2/OUT3
    {"id": 0, "d": {"a": "button", "ms": 3000}}   pulse the POCO power button

The "whoami" identify reply lets the backend tell the powerbox apart from the
gateway over USB (both share ids 1/2), so it can bind each role to its stable
/dev/serial/by-id path regardless of /dev/ttyACM* enumeration order.

Hardware (RP2040 / Raspberry Pi Pico pinout):
    GP0/GP1  I2C (INA219 current/voltage monitor + optional BMP280/AHT20).
             All three OUTn power rails are downstream of the INA219 shunt, so
             its current/coulomb count is the WHOLE-computer draw.
    GP11     ACC / ignition sense — INVERTED logic (active-low):
             ACC ON => LOW (0), ACC OFF => HIGH (1). Internal pull-up enabled.
    GP29     OUT1 — MASTER power rail MOSFET (active-high): POCO + this RP2040 +
             USB hub 5V. Hardware OR with ACC: computer_power = OUT1 OR ACC, so
             the RP2040 auto-powers when the ignition is ON; OUT1 is driven HIGH
             to LATCH/hold the rail after ACC drops. Driving OUT1 LOW = "suicide"
             (removes the latch; the computer powers off once ACC is also gone),
             protecting the 12V battery from deep discharge.
    GP28     OUT2 — RS485 satellite power MOSFET (active-high). Controllable so
             satellites can run while the Prius is off. Default ON at boot.
    GP27     OUT3 — spare power MOSFET (active-high). Unused; default OFF.
    GP15     POCO power button — soldered directly across the phone's power
             button (which triggers by shorting to GND). Drive LOW to "press";
             must be high-impedance (Pin.IN) at ALL other times. A ~3s press
             powers the POCO ON; a ~10s press forces a hard reboot.

A bidirectional rolling-counter heartbeat (this board's STATUS "hb" out, the
POCO's "hb" in) lets each side detect if the other died: if the POCO stops
heartbeating the firmware can wake it with the power button; if the firmware
dies the POCO's rail eventually drops (OUT1 un-fed) or the POCO notices the gap.
"""

import json
import sys
import time
import select

import machine
from machine import I2C, SoftI2C, Pin
from ina219 import INA219

# ─── Configuration ────────────────────────────────────────────────────────────

VERSION = "1.6.0"

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

# ─── Power management GPIO (MOSFET drivers + POCO power button) ───────────────
#
# Three high-side MOSFET power switches, all downstream of the INA219 shunt
# (so the coulomb count is the whole-computer draw). All ACTIVE-HIGH (HIGH = on):
#   OUT1 = GP29  MASTER rail: POCO + this RP2040 + USB hub 5V.
#                Hardware: computer_power = OUT1 OR ACC. The RP2040 powers up
#                automatically when ACC (ignition) is ON; OUT1 is then driven HIGH
#                to LATCH/hold the rail after ACC drops. Driving OUT1 LOW is the
#                "suicide": it removes the latch so the whole computer powers off
#                (effective once ACC is also gone) to protect the 12V battery.
#                => OUT1 MUST be HIGH at all times except a deliberate suicide.
#   OUT2 = GP28  RS485 satellite power (controllable while the Prius is off).
#   OUT3 = GP27  spare / unused.
OUT1_PIN = 29
OUT2_PIN = 28
OUT3_PIN = 27
OUT2_BOOT_ON = True    # satellites powered at boot
OUT3_BOOT_ON = True    # spare on

# POCO power button — GP15 is soldered directly across the POCO's power button,
# which triggers by being shorted to ground. We "press" it by driving GP15 LOW;
# at ALL other times it MUST be high-impedance (Pin.IN), never driven HIGH, or we
# would fight the phone's button circuit. A ~3s press powers the POCO ON from off
# (a ~10s press forces a hard reboot).
POCO_BTN_PIN = 15
POCO_BTN_PRESS_MS = 3000      # power-on hold (POCO F1: ~2-3 s)

# Bidirectional heartbeat (powerbox <-> POCO). We emit a rolling "automotive"
# counter in every STATUS message (every HEARTBEAT_TX_MS) so the POCO can detect
# if we die; the POCO sends its own rolling counter back ({"a":"hb","n":N}). If
# the POCO's counter stops advancing for POCO_HB_TIMEOUT_MS we consider it dead.
# Kept at 2 s (not 1 s) to halve the id-0 STATUS packet rate on the USB-CDC link
# — one fewer transfer per second eases the Full-Speed-behind-hub stall — while
# staying ~7x inside POCO_HB_TIMEOUT_MS. Telemetry (id 1) still streams at 1 s,
# so the backend's frame-staleness watchdog is unaffected.
HEARTBEAT_TX_MS = 2000
POCO_HB_TIMEOUT_MS = 15000
# At cold boot the POCO needs time to power on and start its backend before it
# can heartbeat — don't treat it as dead (or press its button) during this grace.
POCO_BOOT_GRACE_MS = 60000
# After a power-button press, wait this long before pressing again (lets the POCO
# finish booting; avoids a press storm / accidental power-off).
POCO_WAKE_COOLDOWN_MS = 60000
# Wake escalation: a 3 s press only powers ON a POCO that is OFF. A POCO whose
# SoC has FROZEN (kernel hang — observed 2x on 2026-07-10) ignores short presses;
# only a long (~10 s+) forced power-cycle recovers it. After this many short
# wake presses with no heartbeat recovery, escalate to a long press.
POCO_WAKE_SHORT_TRIES = 2
POCO_BTN_FORCE_MS = 12000     # forced hard power-cycle hold (POCO F1: ~10 s)

# Firmware-local under-voltage backstop. The backend's UndervoltageProtectionRule
# (11.0 V / 5 s) normally handles low voltage gracefully by sending an "off"
# command. This is the LAST-RESORT backstop for when the backend is dead: if the
# rail sits below SUICIDE_VOLTAGE for SUICIDE_CONFIRM_MS we start the
# shutdown->suicide sequence ourselves. Set well below the backend threshold so
# the backend always leads.
SUICIDE_VOLTAGE = 10.0
SUICIDE_CONFIRM_MS = 20000
# Default grace before suicide once a shutdown begins (POCO OS shutdown window),
# used when no grace is supplied with the "off" command.
SHUTDOWN_GRACE_S = 30

# ─── Helpers ──────────────────────────────────────────────────────────────────

# USB-CDC TX error counter.  If sys.stdout.write() throws repeatedly (host
# gone for good), we reset the MCU after 20 consecutive errors so it can
# start fresh when the host eventually re-opens the port.
_tx_errors = 0

def tx(device_id: int, payload: dict):
    """Send an NDJSON message to the host via USB-CDC (stdout).

    Writes directly — if the CDC TX FIFO is full the call blocks until the
    host drains it.  This is the same approach the gateway firmware uses and
    avoids the select.poll(POLLOUT) "sticky not-ready" bug that caused
    permanent silent frame drops.

    The hardware watchdog (8 s) guarantees the MCU resets if a write blocks
    longer than that (e.g. host crashed and never drains), so the firmware
    can never deadlock permanently.
    """
    global _tx_errors
    try:
        msg = json.dumps({"id": device_id, "d": payload})
        sys.stdout.write(msg + "\n")
        _tx_errors = 0  # reset on success
    except Exception:
        _tx_errors += 1
        if _tx_errors > 20:
            import machine
            machine.reset()


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


def _truthy(value) -> bool:
    """Tolerant truthiness for inbound command flags (bool / int / 'on'/'off')."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


# ─── Watchdog ─────────────────────────────────────────────────────────────────

# Module-level so every loop / sleep site can feed it without threading it
# through call signatures. Stays None if the WDT is unavailable on this port.
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


# ─── Power management GPIO ────────────────────────────────────────────────────

def setup_power_pins():
    """Configure the OUT1/OUT2/OUT3 MOSFET drivers and the POCO power button.

    OUT1 (master rail) is driven HIGH FIRST so the rail is latched the instant
    this runs — if ACC drops mid-boot the computer stays powered. OUT2/OUT3 take
    their configured boot defaults. The POCO power button is left high-impedance
    (Pin.IN) so we never hold the phone's button down.
    """
    out1 = Pin(OUT1_PIN, Pin.OUT, value=1)                       # LATCH master rail HIGH
    out2 = Pin(OUT2_PIN, Pin.OUT, value=1 if OUT2_BOOT_ON else 0)
    out3 = Pin(OUT3_PIN, Pin.OUT, value=1 if OUT3_BOOT_ON else 0)
    Pin(POCO_BTN_PIN, Pin.IN)                                    # high-Z = not pressed
    return out1, out2, out3


# Module-global PowerManager so process_command (driven from poll_stdin) can route
# power commands (hb / out / button / off) to it without threading it through, the
# same pattern as the WDT handle above.
_pm = None


class PowerManager:
    """OUT1/2/3 rails, POCO power button, heartbeat and self-kill ("suicide").

    States:
        "normal"   running; holds OUT1 high; wakes a dead POCO via the button.
        "shutdown" a shutdown was requested (under-voltage backstop or host
                   "off"); waits for the POCO to power off (heartbeat lost) or the
                   grace timeout, then suicides. Does NOT wake the POCO.
        "dead"     suicide done; OUT1 low; idle.
    """

    def __init__(self, out1, out2, out3):
        self.out1 = out1
        self.out2 = out2
        self.out3 = out3
        self.state = "normal"
        now = time.ticks_ms()
        self.boot_ms = now
        # Our outbound rolling heartbeat counter (carried in STATUS).
        self.hb_tx = 0
        self.last_hb_tx_ms = now
        # POCO inbound heartbeat liveness — the counter must keep CHANGING.
        self.seen_poco = False
        self.last_poco_n = None
        self.last_poco_change_ms = now
        # POCO power button.
        self.last_btn_ms = time.ticks_add(now, -POCO_WAKE_COOLDOWN_MS)
        self.pending_button_ms = 0
        self.wake_tries = 0           # consecutive wake presses without recovery
        # Shutdown / suicide.
        self.poco_should_run = True
        self.shutdown_t0 = 0
        self.shutdown_grace_s = SHUTDOWN_GRACE_S
        # Under-voltage backstop timer.
        self.low_since = None

    # -- inbound from POCO ----------------------------------------------------
    def on_poco_hb(self, n):
        """Record a heartbeat from the POCO. Liveness tracks counter CHANGES so a
        stuck (repeating) counter still reads as dead."""
        self.seen_poco = True
        now = time.ticks_ms()
        if n != self.last_poco_n:
            self.last_poco_n = n
            self.last_poco_change_ms = now

    def poco_alive(self, now=None):
        if not self.seen_poco:
            return False
        if now is None:
            now = time.ticks_ms()
        return time.ticks_diff(now, self.last_poco_change_ms) < POCO_HB_TIMEOUT_MS

    # -- outputs --------------------------------------------------------------
    def set_out(self, ch, on):
        """Set OUT2/OUT3. OUT1 (the master latch) is NEVER host-controllable here;
        it is only ever dropped by the suicide path."""
        if ch == 2:
            self.out2.value(1 if on else 0)
        elif ch == 3:
            self.out3.value(1 if on else 0)

    def request_button(self, ms):
        """Queue a power-button press (executed on the next tick)."""
        try:
            ms = int(ms)
        except (TypeError, ValueError):
            ms = POCO_BTN_PRESS_MS
        if ms < 100:
            ms = POCO_BTN_PRESS_MS
        if ms > 12000:
            ms = 12000
        self.pending_button_ms = ms

    def _press_button(self, ms):
        tx(ID_SYSTEM, {"msg": "POCO_BTN", "ms": ms})
        Pin(POCO_BTN_PIN, Pin.OUT, value=0)   # short to GND = press
        wdt_sleep_ms(ms)                      # feeds the WDT during the hold
        Pin(POCO_BTN_PIN, Pin.IN)             # release -> high-impedance
        self.last_btn_ms = time.ticks_ms()

    # -- shutdown / suicide ---------------------------------------------------
    def begin_shutdown(self, grace_s, reason):
        if self.state != "normal":
            return
        self.state = "shutdown"
        self.poco_should_run = False          # don't fight our own shutdown
        self.shutdown_t0 = time.ticks_ms()
        self.shutdown_grace_s = grace_s if grace_s and grace_s > 0 else SHUTDOWN_GRACE_S
        tx(ID_SYSTEM, {"msg": "SHUTDOWN", "reason": reason, "grace_s": self.shutdown_grace_s})

    def _suicide(self, reason):
        tx(ID_SYSTEM, {"msg": "SUICIDE", "reason": reason})
        # Cut the master latch. With ACC also gone this powers the whole computer
        # (POCO + this RP2040 + hub) off. If ACC is still energising the rail we
        # stay powered but keep the latch LOW so power drops the moment ACC does.
        self.out1.value(0)
        self.state = "dead"

    def _tx_status(self, now):
        tx(ID_SYSTEM, {
            "msg": "STATUS",
            "hb": self.hb_tx,
            "out1": self.out1.value(),
            "out2": self.out2.value(),
            "out3": self.out3.value(),
            "poco": 1 if self.poco_alive(now) else 0,
            "pm": self.state,
        })
        self.hb_tx = (self.hb_tx + 1) & 0xFF

    def tick(self, voltage):
        """Run the power state machine for one main-loop iteration.

        ``voltage`` is the INA219 bus voltage (V), or None when unavailable.
        """
        if self.state == "dead":
            return

        now = time.ticks_ms()

        # 1) Heartbeat / status TX (rolling counter).
        if time.ticks_diff(now, self.last_hb_tx_ms) >= HEARTBEAT_TX_MS:
            self.last_hb_tx_ms = now
            self._tx_status(now)

        # 2) Execute a queued power-button press.
        if self.pending_button_ms:
            ms = self.pending_button_ms
            self.pending_button_ms = 0
            self._press_button(ms)
            now = time.ticks_ms()

        # 3) Under-voltage backstop (only with a real reading, only while normal).
        if self.state == "normal" and voltage is not None:
            if voltage < SUICIDE_VOLTAGE:
                if self.low_since is None:
                    self.low_since = now
                elif time.ticks_diff(now, self.low_since) >= SUICIDE_CONFIRM_MS:
                    self.begin_shutdown(SHUTDOWN_GRACE_S, "undervoltage %.2fV" % voltage)
            else:
                self.low_since = None

        # 4) State machine.
        if self.state == "shutdown":
            poco_down = not self.poco_alive(now)
            timed_out = time.ticks_diff(now, self.shutdown_t0) >= self.shutdown_grace_s * 1000
            if poco_down or timed_out:
                self._suicide("poco_down" if poco_down else "grace_timeout")
        elif self.state == "normal" and self.poco_should_run:
            # Wake a dead POCO with the power button (after boot grace + cooldown).
            # Escalate: short presses power ON an off POCO; if those don't bring
            # the heartbeat back the SoC is likely FROZEN, and only a long
            # (~12 s) forced power-cycle recovers it.
            past_boot = time.ticks_diff(now, self.boot_ms) >= POCO_BOOT_GRACE_MS
            cooled = time.ticks_diff(now, self.last_btn_ms) >= POCO_WAKE_COOLDOWN_MS
            if self.poco_alive(now):
                self.wake_tries = 0
            elif past_boot and cooled:
                if self.wake_tries < POCO_WAKE_SHORT_TRIES:
                    self._press_button(POCO_BTN_PRESS_MS)
                else:
                    self._press_button(POCO_BTN_FORCE_MS)
                self.wake_tries += 1


# ─── Command Processing ──────────────────────────────────────────────────────

class Config:
    """Mutable runtime configuration, adjustable via inbound commands."""
    __slots__ = ("interval_ms", "shutdown_requested", "shutdown_grace_s")

    def __init__(self):
        self.interval_ms = DEFAULT_INTERVAL_MS
        self.shutdown_requested = False
        self.shutdown_grace_s = 0


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
        config.shutdown_requested = True
        config.shutdown_grace_s = int(data.get("grace_s", SHUTDOWN_GRACE_S))
        if _pm is not None:
            _pm.begin_shutdown(config.shutdown_grace_s, str(data.get("reason", "host_off")))
        tx_ack("off")

    elif action == "set_interval":
        ms = data.get("ms")
        if isinstance(ms, (int, float)) and ms >= 100:
            config.interval_ms = int(ms)
            tx_ack("set_interval")

    elif action == "ping":
        tx(ID_SYSTEM, {"msg": "PONG"})

    elif action in ("whoami", "identify", "id"):
        tx_ident()

    elif action == "hb":
        # POCO heartbeat (rolling counter) — keeps the watchdog's "POCO alive"
        # state fresh so we don't wake/suicide a healthy POCO.
        if _pm is not None:
            _pm.on_poco_hb(data.get("n"))

    elif action == "out":
        # Set a controllable rail: OUT2 (RS485 satellites) or OUT3 (spare).
        if _pm is not None:
            _pm.set_out(data.get("ch"), _truthy(data.get("on")))
            tx_ack("out")

    elif action == "button":
        # Pulse the POCO power button (wake / force-reboot).
        if _pm is not None:
            _pm.request_button(data.get("ms", POCO_BTN_PRESS_MS))
            tx_ack("button")

    elif action == "reset_mah":
        tx_ack("reset_mah")
        # Caller must handle the actual reset in the main loop
        config._reset_mah = True  # noqa: SLF001

    elif action == "fan":
        pin_num = data.get("pin")
        duty = data.get("duty", 0)
        try:
            freq = data.get("freq", 25000)
            if not hasattr(config, "_pwm_channels"):
                config._pwm_channels = {}
            if pin_num not in config._pwm_channels:
                pwm = machine.PWM(Pin(pin_num))
                config._pwm_channels[pin_num] = pwm
            config._pwm_channels[pin_num].freq(int(freq))
            config._pwm_channels[pin_num].duty_u16(int(duty))
            tx_ack("fan")
        except Exception:
            pass


def poll_stdin(config: Config):
    """Non-blocking read of any complete NDJSON lines from USB-CDC stdin."""
    try:
        while sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            if line:
                process_command(line.strip(), config)
    except Exception:
        # select may not be available on all MicroPython ports;
        # fall back to non-blocking read via poll
        try:
            import micropython  # noqa: F811
            if hasattr(sys.stdin, "read"):
                data = sys.stdin.read(1)  # will return None if nothing
                if data:
                    # buffer until newline
                    if not hasattr(config, "_rxbuf"):
                        config._rxbuf = ""
                    config._rxbuf += data
                    while "\n" in config._rxbuf:
                        line, config._rxbuf = config._rxbuf.split("\n", 1)
                        process_command(line.strip(), config)
        except Exception:
            pass


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    global _pm

    # Core clock. The RP2040's USB device stack (TinyUSB) is serviced from the
    # main core, so an aggressive underclock leaves the CDC IN endpoint slow to
    # answer host IN tokens. Behind the old DUB-H4 USB-2 hub on the POCO's sdm845
    # xHCI this shows up as the recurring "USB-CDC wedged (board alive, host not
    # receiving)" stall the backend then has to recover from. 96 MHz keeps a
    # healthy servicing margin while still running at ~2/3 of the 133 MHz default
    # for power. (Do NOT drop back to 48 MHz without re-checking the link.)
    machine.freq(96000000)

    # Latch the master power rail (OUT1 HIGH) FIRST — before anything that could
    # take time — so the computer holds power even if ACC drops during boot. Also
    # set OUT2/OUT3 defaults and leave the POCO button high-impedance.
    out1, out2, out3 = setup_power_pins()
    _pm = PowerManager(out1, out2, out3)

    # Banner — will be parsed by the computer's parse_powerbox_system()
    tx(ID_SYSTEM, {"msg": "POWERBOX_READY", "ver": VERSION, "role": ROLE})

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
        tx(ID_EVENTS, {"acc": acc_state})
        tx_ident()
        last_acc_tx = time.ticks_ms()
        last_ident_tx = time.ticks_ms()
        while True:
            feed_wdt()
            poll_stdin(idle_config)
            acc_now = read_acc(acc_pin)
            now_ms = time.ticks_ms()
            if acc_now != acc_state or \
                    time.ticks_diff(now_ms, last_acc_tx) >= ACC_HEARTBEAT_MS:
                acc_state = acc_now
                last_acc_tx = now_ms
                tx(ID_EVENTS, {"acc": acc_state})
            if time.ticks_diff(now_ms, last_ident_tx) >= IDENT_HEARTBEAT_MS:
                last_ident_tx = now_ms
                tx_ident()
            # Power management (heartbeat TX, POCO wake, suicide). No INA here, so
            # no voltage reading is available for the under-voltage backstop.
            _pm.tick(None)
            time.sleep_ms(500)

    ina = None
    bmp = None
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

    bmp_addr = None
    for d in devices:
        if d in (0x76, 0x77):
            bmp_addr = d
            break
    if bmp_addr is not None:
        try:
            import bmp280
            bmp = bmp280.BMP280(i2c, addr=bmp_addr)
            tx(ID_SYSTEM, {"msg": "SENSOR_OK", "chip": "BMP280", "addr": hex(bmp_addr)})
        except Exception as e:
            tx_error("BMP280_INIT", str(e))

    # AHT20 (address 0x38)
    if 0x38 in devices:
        try:
            import ahtx0
            aht = ahtx0.AHT20(i2c)
            tx(ID_SYSTEM, {"msg": "SENSOR_OK", "chip": "AHT20", "addr": "0x38"})
        except Exception as e:
            tx_error("AHT20_INIT", str(e))

    # Telemetry loop
    config = Config()
    total_mah = 0.0
    last_time = time.ticks_ms()
    error_streak = 0

    # Ignition (ACC) sense on GP11 (inverted logic, active-low)
    acc_pin = setup_acc_pin()
    acc_state = read_acc(acc_pin)
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
            if getattr(config, "_reset_mah", False):
                total_mah = 0.0
                config._reset_mah = False

            # Read ignition (ACC) on GP11 — emit on change or as a heartbeat
            acc_now = read_acc(acc_pin)
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

            # Read BMP280
            if bmp is not None:
                payload["bmp_t"] = round(bmp.temperature, 2)
                payload["bmp_p"] = round(bmp.pressure, 2)

            # Read AHT20
            if aht is not None:
                payload["aht_t"] = round(aht.temperature, 2)
                payload["aht_h"] = round(aht.relative_humidity, 2)

            # Telemetry message (device 201 on computer side)
            if payload:
                tx(ID_TELEMETRY, payload)

            # Power management: heartbeat TX, under-voltage backstop, POCO wake,
            # shutdown->suicide state machine. Pass the measured bus voltage.
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
