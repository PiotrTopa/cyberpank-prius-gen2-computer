# Runs on every reset before main.py and before the REPL.
#
# 1. Release the POCO power button. Every RP2040 pad resets with its pull-DOWN
#    on; on GP27 (active-LOW button) that holds the button "pressed" whenever
#    main.py is not running (REPL, flash, crash) and the phone boot-loops
#    (seen 2026-11-04). Keep ACTIVE_HIGH in sync with POCO_BTN_ACTIVE_HIGH in
#    main.py (True once the button is driven through an N-MOSFET).
# 2. Finish an interrupted firmware swap (main.py -> main.py.bak, then
#    main.py.new -> main.py): if main.py is missing, put the old one back.
from machine import Pin

ACTIVE_HIGH = False
BTN_PIN = 27

if ACTIVE_HIGH:
    Pin(BTN_PIN, Pin.OUT, value=0)
else:
    Pin(BTN_PIN, Pin.IN, pull=None)

try:
    import os
    _files = os.listdir()
    if "main.py" not in _files and "main.py.bak" in _files:
        os.rename("main.py.bak", "main.py")
except Exception:
    pass
