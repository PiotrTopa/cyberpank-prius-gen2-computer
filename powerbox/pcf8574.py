"""PCF8574 I2C expander driver + the relay-board logic that sits on it.

Pure Python (no ``machine`` import) so the relay logic is unit-testable on
CPython with a fake bus; on the device it is driven by ``main.py``.

Relay board (HW-281 type, 8 channels, active-low inputs on P0..P7):
    board relay N = PCF pin P(N-1); firmware channel N = board relay N.
    Pin LOW  = coil energised.  Pin HIGH (power-up state 0xFF) = coil released.
    NO contact: load powered when the coil is energised  (pin LOW).
    NC contact: load powered when the coil is released   (pin HIGH).

Rules this module enforces (powerbox/RELAY-CHANGE-PLAN.md, item 7):
    * the cached byte is updated only after a successful I2C write;
    * reported states come from a chip read-back, not from the cache;
    * every read-back is compared with the wanted byte; a mismatch (lost
      write, PCF reset to 0xFF) is re-written and counted;
    * the restart relay (powerbox's own supply, NC) is never reachable through
      the generic ``set``; ``restart()`` is the only way to energise it.
"""


class PCF8574:
    """Minimal PCF8574 driver. ``read()``/``write()`` raise on bus errors."""

    def __init__(self, i2c, address=0x20):
        self.i2c = i2c
        self.address = address

    def read(self):
        return self.i2c.readfrom(self.address, 1)[0]

    def write(self, value):
        self.i2c.writeto(self.address, bytes([value & 0xFF]))


# Channel -> PCF pin (board relay N = pin N-1).
RELAY_PIN_MAP = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7}
RELAY_CHANNELS = (1, 2, 3, 4, 5, 6, 7, 8)

# Channels wired with the NC contact (load powered while the coil is released).
RELAY_NC = (1,)

# Relay 1 (NC) carries the powerbox's OWN supply: energising it cuts our power
# (hard restart; the PCF comes back at 0xFF so it self-restores). Not a load.
RELAY_CH_RESTART = 1
# Channels that plain "relay" commands may never touch.
RELAY_RESTRICTED = (RELAY_CH_RESTART,)

# If we are still alive this long after energising the restart relay, the cut
# did not happen (relay not wired?): release the coil and report it.
RESTART_RELEASE_MS = 3000


def relay_level(ch, on):
    """Pin level (0/1) that powers (``on``) or unpowers a channel's load."""
    if ch in RELAY_NC:
        return 1 if on else 0
    return 0 if on else 1


def relay_is_on(ch, level):
    """Load powered? from a pin level (0/1)."""
    if ch in RELAY_NC:
        return 1 if level else 0
    return 0 if level else 1


def relay_byte(port, ch, on):
    """Return ``port`` with channel ``ch`` set to power/unpower its load."""
    pin = RELAY_PIN_MAP[ch]
    if relay_level(ch, on):
        return port | (1 << pin)
    return port & ~(1 << pin) & 0xFF


class RelayBoard:
    """Wanted-state keeper with read-back verification.

    ``bus`` needs ``read() -> int`` and ``write(int)``; ``ticks`` is a
    ``time.ticks_ms``-like clock and ``ticks_diff(a, b)`` its difference,
    both injectable for tests. ``on_event(kind, **fields)`` and
    ``on_error(code, detail)`` are reporting hooks (``log_event``/``tx_error``
    in main.py); both optional.
    """

    def __init__(self, bus, ticks, ticks_diff, on_event=None, on_error=None):
        self.bus = bus
        self._ticks = ticks
        self._ticks_diff = ticks_diff
        self._on_event = on_event
        self._on_error = on_error
        # Adopt the chip's current latch (relay state survives warm MCU
        # reboots); the restart relay is always "released" in the wanted byte.
        port = bus.read()
        self.wanted = relay_byte(port, RELAY_CH_RESTART, True)
        if self.wanted != port:
            bus.write(self.wanted)
        self.fix_count = 0        # drift re-writes (lost write / PCF reset)
        self.pcf_resets = 0       # read-backs that showed the 0xFF power-up byte
        self.err_count = 0        # I2C errors
        self.restart_at = None    # ticks when the restart coil was energised
        self._drift_streak = 0

    # -- reporting ------------------------------------------------------------
    def _event(self, kind, **fields):
        if self._on_event is not None:
            self._on_event(kind, **fields)

    def _error(self, code, detail):
        self.err_count += 1
        if self._on_error is not None:
            self._on_error(code, detail)

    # -- commands -------------------------------------------------------------
    def set(self, ch, on):
        """Power (``on``) a load channel. Returns (ok, error_detail_or_None).

        Refuses unknown channels and the restricted (restart) channel. The
        wanted byte changes only after the write succeeded.
        """
        if ch not in RELAY_PIN_MAP:
            return False, "bad channel %s" % ch
        if ch in RELAY_RESTRICTED:
            return False, "ch %d is the powerbox restart relay; use hard_restart" % ch
        new = relay_byte(self.wanted, ch, on)
        try:
            self.bus.write(new)
        except Exception as e:
            self._error("RELAY_IO", "ch%d write: %s" % (ch, e))
            return False, "write failed"
        self.wanted = new
        return True, None

    def restart(self):
        """Energise the restart relay (cuts our own supply). Returns ok."""
        new = relay_byte(self.wanted, RELAY_CH_RESTART, False)
        try:
            self.bus.write(new)
        except Exception as e:
            self._error("RELAY_IO", "restart write: %s" % e)
            return False
        self.restart_at = self._ticks()
        return True

    # -- read-back / verification ---------------------------------------------
    def expected(self):
        """Byte the chip should hold right now."""
        if self.restart_at is not None:
            return relay_byte(self.wanted, RELAY_CH_RESTART, False)
        return self.wanted

    def states(self, report_restricted=False):
        """Read the chip back, repair drift, return [ch1..ch8] of 0/1/None.

        1 = load powered, from the CHIP, not the cache. Restricted channels
        report None unless ``report_restricted``. Returns None when the read
        fails (the caller omits ``rly`` and reports the error count).
        """
        try:
            port = self.bus.read()
        except Exception as e:
            self._error("RELAY_IO", "read: %s" % e)
            return None
        if self._verify(port):
            # Drift was repaired: report the chip as it is NOW (second
            # read-back), not the byte we just overwrote.
            try:
                port = self.bus.read()
            except Exception as e:
                self._error("RELAY_IO", "read: %s" % e)
        out = []
        for ch in RELAY_CHANNELS:
            if ch in RELAY_RESTRICTED and not report_restricted:
                out.append(None)
            else:
                out.append(relay_is_on(ch, (port >> RELAY_PIN_MAP[ch]) & 1))
        return out

    def _verify(self, port):
        """Compare a read-back with the wanted byte; rewrite on mismatch.
        Returns True when a repair write was issued."""
        now = self._ticks()
        if self.restart_at is not None:
            if self._ticks_diff(now, self.restart_at) < RESTART_RELEASE_MS:
                return False  # cut in progress; we should be dead any moment
            # Still alive: the restart relay did not cut our supply.
            self.restart_at = None
            self._error("RESTART_FAILED",
                        "alive %d ms after energising relay %d; released"
                        % (RESTART_RELEASE_MS, RELAY_CH_RESTART))
        want = self.wanted
        if port == want:
            self._drift_streak = 0
            return False
        if port == 0xFF:
            self.pcf_resets += 1
        try:
            self.bus.write(want)
        except Exception as e:
            self._error("RELAY_IO", "reassert: %s" % e)
            return False
        self.fix_count += 1
        self._drift_streak += 1
        if self._drift_streak == 1:
            # First mismatch of a streak -> one event; a chip that keeps
            # disagreeing is visible through fix_count without log spam.
            self._event("rly_fix", got=port, want=want,
                        pcf_reset=1 if port == 0xFF else 0)
        return True

    def tick(self):
        """Periodic verification without reporting (used between STATUS)."""
        try:
            port = self.bus.read()
        except Exception as e:
            self._error("RELAY_IO", "read: %s" % e)
            return
        self._verify(port)
