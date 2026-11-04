# Runs before main.py and before the REPL. The RP2040 resets every GPIO pad
# with its pull-DOWN enabled: on GP27 (POCO power button, active LOW) that weak
# pull holds the button "pressed" whenever main.py is not running (REPL, flash,
# crash), and the phone boot-loops (seen 2026-11-04). Release it first thing.
from machine import Pin
Pin(27, Pin.IN, pull=None)
