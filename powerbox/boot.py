# Runs on every reset before main.py and before the REPL.
#
# 1. Release the POCO power buttons (GP27 main, GP26 secondary). Every RP2040 pad resets with its pull-DOWN
#    on; on GP27 (active-LOW button) that holds the button "pressed" whenever
#    main.py is not running (REPL, flash, crash) and the phone boot-loops
#    (seen 2026-11-04). Keep ACTIVE_HIGH in sync with POCO_BTN_ACTIVE_HIGH in
#    main.py (True once the button is driven through an N-MOSFET).
# 2. Hold the BEAN transceiver TX (GP7) LOW = bus released (HIGH would drive
#    the body bus to +B through a 220R short-circuit limit only) and leave the
#    RX (GP6) a plain input (R8 10k pull-up on the board). main.py re-owns
#    both pins first thing.
# 3. Finish an interrupted firmware swap (main.py -> main.py.bak, then
#    main.py.new -> main.py): if main.py is missing, put the old one back.
from machine import Pin

ACTIVE_HIGH = False
BTN_PINS = (27, 26)      # main POCO, secondary POCO (POCOn_BTN_PIN in main.py)

for _pin in BTN_PINS:
    if ACTIVE_HIGH:
        Pin(_pin, Pin.OUT, value=0)
    else:
        Pin(_pin, Pin.IN, pull=None)

BEAN_TX_PIN = 7
BEAN_RX_PIN = 6
Pin(BEAN_TX_PIN, Pin.OUT, value=0)
Pin(BEAN_RX_PIN, Pin.IN, pull=None)

try:
    import os
    _files = os.listdir()
    if "main.py" not in _files and "main.py.bak" in _files:
        os.rename("main.py.bak", "main.py")
except Exception:
    pass
