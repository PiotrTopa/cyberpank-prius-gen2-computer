#!/usr/bin/env python3
"""
Raw debug test - shows ALL traffic and sends a simple request.
Use this to verify Gateway is working and car is awake.
"""

import serial
import json
import time
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"
BAUD = 1000000

def main():
    print(f"Opening {PORT} at {BAUD} baud...")
    ser = serial.Serial(PORT, BAUD, timeout=0.1)
    time.sleep(0.3)
    
    print("\n=== Phase 1: Check if CAN bus has traffic (5 sec) ===")
    print("Looking for 0x3CB (battery temp), 0x38 (speed), 0x3B (HV battery)...")
    
    can_ids_seen = set()
    start = time.time()
    while time.time() - start < 5:
        if ser.in_waiting:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                payload = data.get("d", {})
                can_id = payload.get("i", "")
                if can_id and can_id.startswith("0x"):
                    can_ids_seen.add(can_id)
            except:
                pass
        else:
            time.sleep(0.01)
    
    print(f"\nCAN IDs seen: {sorted(can_ids_seen)}")
    
    if not can_ids_seen:
        print("\n❌ NO CAN TRAFFIC! Car is likely OFF or in ACC-OFF state.")
        print("   Turn ignition to IG-ON or start the car (READY mode).")
        ser.close()
        return
    
    if "0x3CB" in can_ids_seen or "0x3B" in can_ids_seen:
        print("✓ Hybrid system messages detected - car is awake!")
    elif "0x30" in can_ids_seen:
        print("⚠ Only 0x30 messages - car might be in ACC mode (limited ECU response)")
    
    print("\n=== Phase 2: Switch to Normal mode ===")
    cmd = {"id": 1, "d": {"a": "mode", "m": "normal"}}
    ser.write((json.dumps(cmd) + "\n").encode())
    time.sleep(0.3)
    
    # Show mode response
    while ser.in_waiting:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if line and ("CAN_MODE" in line or "NORMAL" in line or "id\":1" in line):
            print(f"  {line[:80]}")
    
    print("\n=== Phase 3: Send RPM request and show ALL responses ===")
    cmd = {
        "id": 1,
        "d": {
            "a": "req",
            "i": "0x7DF",
            "d": [2, 1, 12, 0, 0, 0, 0, 0],
            "r": ["0x7E8"],
            "t": 2000
        }
    }
    print(f"TX: {json.dumps(cmd)}")
    ser.write((json.dumps(cmd) + "\n").encode())
    
    print("\nWaiting 3 seconds for response (showing 'resp' action or 0x7E*)...")
    start = time.time()
    responses = []
    while time.time() - start < 3:
        if ser.in_waiting:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue
            # Show anything that might be our response
            if '"a":"resp"' in line or '0x7E' in line:
                print(f"  RX: {line[:120]}")
                responses.append(line)
        else:
            time.sleep(0.01)
    
    if not responses:
        print("\n❌ No 'resp' action received!")
        print("\nPossible causes:")
        print("  1. Gateway not sending request to CAN bus (TX issue)")
        print("  2. Gateway not forwarding response (filter issue)")
        print("  3. ECU not responding (car not in READY mode)")
        print("\nCheck Gateway serial output for debug messages.")
    else:
        print(f"\n✓ Received {len(responses)} response(s)")
    
    ser.close()

if __name__ == "__main__":
    main()
