#!/usr/bin/env python3
"""Quick serial diagnostic: sends commands, prints ALL responses."""
import serial
import json
import time
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"
BAUD = 1000000

ser = serial.Serial(PORT, BAUD, timeout=0.1)
time.sleep(0.5)

# Drain existing buffer
print("=== Draining buffer (2s) ===")
end = time.time() + 2
count = 0
while time.time() < end:
    line = ser.readline().decode('utf-8', errors='ignore').strip()
    if line:
        count += 1
        if count <= 5:
            print(f"  [{count}] {line[:120]}")
print(f"  Drained {count} lines")

# Test 1: Send mode switch
print("\n=== Test 1: Mode switch to NORMAL ===")
cmd = {"id": 1, "d": {"a": "mode", "m": "normal"}}
raw = json.dumps(cmd) + "\n"
print(f"  TX: {raw.strip()}")
ser.write(raw.encode())
ser.flush()

time.sleep(0.5)
# Read responses
print("  Responses:")
end = time.time() + 2
found = 0
while time.time() < end:
    line = ser.readline().decode('utf-8', errors='ignore').strip()
    if line:
        found += 1
        try:
            d = json.loads(line)
            if d.get("id") == 0:
                print(f"  ** SYS: {line[:120]}")
            elif d.get("id") == 1:
                dd = d.get("d", {})
                print(f"  ** CAN: id={dd.get('i','?')} a={dd.get('a','?')} {line[:80]}")
            else:
                if found <= 3:
                    print(f"     [{found}] {line[:80]}")
        except:
            print(f"  ?? {line[:80]}")

print(f"  Total responses: {found}")

# Test 2: Send subscription
print("\n=== Test 2: Subscribe slot 0 (RPM) ===")
cmd = {"id": 1, "d": {"a": "sub", "slot": 0, "i": "0x7E0", "d": [2, 1, 12, 0, 0, 0, 0, 0], "int": 500, "t": 200, "r": ["0x7E8"]}}
raw = json.dumps(cmd) + "\n"
print(f"  TX: {raw.strip()}")
ser.write(raw.encode())
ser.flush()

time.sleep(0.5)
print("  Responses (3s):")
end = time.time() + 3
found = 0
while time.time() < end:
    line = ser.readline().decode('utf-8', errors='ignore').strip()
    if line:
        found += 1
        try:
            d = json.loads(line)
            if d.get("id") == 0:
                print(f"  ** SYS: {line[:120]}")
            elif d.get("id") == 1:
                dd = d.get("d", {})
                if dd.get("a") == "sub":
                    print(f"  ** SUB_RESP slot={dd.get('slot')} i={dd.get('i')} d={dd.get('d','?')}")
                else:
                    if found <= 5:
                        print(f"     CAN [{found}]: {line[:100]}")
        except:
            print(f"  ?? {line[:80]}")

print(f"  Total responses: {found}")

# Test 3: List subs
print("\n=== Test 3: List subscriptions ===")
cmd = {"id": 1, "d": {"a": "subs"}}
raw = json.dumps(cmd) + "\n"
print(f"  TX: {raw.strip()}")
ser.write(raw.encode())
ser.flush()

time.sleep(0.5)
end = time.time() + 1
while time.time() < end:
    line = ser.readline().decode('utf-8', errors='ignore').strip()
    if line:
        try:
            d = json.loads(line)
            if d.get("id") == 0:
                print(f"  ** SYS: {line[:120]}")
        except:
            pass

# Cleanup: unsub all + listen mode
print("\n=== Cleanup ===")
ser.write((json.dumps({"id": 1, "d": {"a": "unsub", "slot": "all"}}) + "\n").encode())
time.sleep(0.1)
ser.write((json.dumps({"id": 1, "d": {"a": "mode", "m": "listen"}}) + "\n").encode())
time.sleep(0.5)
end = time.time() + 1
while time.time() < end:
    line = ser.readline().decode('utf-8', errors='ignore').strip()
    if line:
        try:
            d = json.loads(line)
            if d.get("id") == 0:
                print(f"  ** SYS: {line[:120]}")
        except:
            pass

ser.close()
print("\nDone.")
