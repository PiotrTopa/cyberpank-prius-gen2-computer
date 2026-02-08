#!/usr/bin/env python3
"""
Ultra-minimal Gateway request test.
Shows EVERY message from Gateway to diagnose the issue.
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
    time.sleep(0.5)
    
    # Drain buffer
    while ser.in_waiting:
        ser.readline()
    
    print("\n=== Sending mode command (id=1, CAN device) ===")
    cmd = {"id": 1, "d": {"a": "mode", "m": "normal"}}
    ser.write((json.dumps(cmd) + "\n").encode())
    print(f"TX: {json.dumps(cmd)}")
    
    time.sleep(0.5)
    print("\nAll responses with id=1 or id=0 (system):")
    while ser.in_waiting:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if line and ('"id":1' in line or '"id":0' in line):
            print(f"  {line}")
    
    # Drain remaining CAN traffic
    time.sleep(0.2)
    while ser.in_waiting:
        ser.readline()
    
    print("\n=== Sending request (id=1, CAN device) ===")
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
    ser.write((json.dumps(cmd) + "\n").encode())
    print(f"TX: {json.dumps(cmd)}")
    
    print("\nWaiting 3 seconds - showing messages with 'a':'resp':")
    start = time.time()
    found_resp = False
    while time.time() - start < 3:
        if ser.in_waiting:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue
            if '"a":"resp"' in line:
                print(f"  RX: {line}")
                found_resp = True
        else:
            time.sleep(0.01)
    
    if found_resp:
        print("\n✓ Gateway returned a response!")
    else:
        print("\n❌ Gateway returned NO 'resp' action!")
    
    # Try subscription as fallback
    print("\n=== Alternative: Try subscription (id=1, slot 0) ===")
    cmd = {
        "id": 1,
        "d": {
            "a": "sub",
            "s": 0,
            "i": "0x7DF",
            "d": [2, 1, 12, 0, 0, 0, 0, 0],
            "r": ["0x7E8"],
            "p": 1000,
            "t": 500
        }
    }
    ser.write((json.dumps(cmd) + "\n").encode())
    print(f"TX: {json.dumps(cmd)}")
    
    print("\nWaiting 3 seconds for SUB_OK or subscription response:")
    start = time.time()
    while time.time() - start < 3:
        if ser.in_waiting:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue
            if 'SUB' in line or '"s":0' in line or '"a":"resp"' in line:
                print(f"  RX: {line}")
        else:
            time.sleep(0.01)
    
    ser.close()
    print("\nDone.")

if __name__ == "__main__":
    main()
