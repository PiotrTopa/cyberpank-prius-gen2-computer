#!/usr/bin/env python3
"""
Diagnostic script to test solicited CAN communication.
Tests ISO-TP multi-frame response reassembly from Gateway v2.9.0.
"""

import serial
import json
import time
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"
BAUD = 1000000

def main():
    print(f"Opening {PORT} at {BAUD} baud...")
    ser = serial.Serial(PORT, BAUD, timeout=1)
    time.sleep(0.5)
    
    # Read and discard any startup messages
    while ser.in_waiting:
        ser.readline()
    
    print("\n--- Switching to Normal mode ---")
    cmd = {"id": 1, "d": {"a": "mode", "m": "normal"}}
    ser.write((json.dumps(cmd) + "\n").encode())
    time.sleep(0.3)
    
    # Read confirmations
    while ser.in_waiting:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if line:
            print(f"  RX: {line[:100]}")
    
    print("\n--- Subscribing to PID 21C3 (inverter temps) with ISO-TP ---")
    # Request: ECU 0x7E2, Mode 21, PID C3
    cmd = {
        "id": 1,
        "d": {
            "a": "sub",
            "slot": 0,
            "i": "0x7E2",
            "d": [0x03, 0x21, 0xC3, 0x00, 0x00, 0x00, 0x00, 0x00],
            "int": 1000,
            "t": 500,
            "r": ["0x7EA"],
            "isotp": True
        }
    }
    ser.write((json.dumps(cmd) + "\n").encode())
    print(f"  TX: {json.dumps(cmd)}")
    
    # Also subscribe to standard OBD-II RPM (should work if engine is running)
    print("\n--- Also subscribing to RPM (PID 010C) ---")
    cmd2 = {
        "id": 1,
        "d": {
            "a": "sub",
            "slot": 1,
            "i": "0x7DF",
            "d": [0x02, 0x01, 0x0C, 0x00, 0x00, 0x00, 0x00, 0x00],
            "int": 500,
            "t": 100,
            "r": ["0x7E8"]
        }
    }
    ser.write((json.dumps(cmd2) + "\n").encode())
    print(f"  TX: {json.dumps(cmd2)}")
    
    print("\n--- Waiting for responses (15 seconds) ---")
    print("NOTE: Car must be in READY mode for ECUs to respond!\n")
    
    print("\n--- Waiting for responses (15 seconds) ---")
    start = time.time()
    count = 0
    while time.time() - start < 15:
        if ser.in_waiting:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue
            
            try:
                data = json.loads(line)
                device_id = data.get("id")
                payload = data.get("d", {})
                
                if device_id == 1:
                    action = payload.get("a")
                    can_id = payload.get("i")
                    slot = payload.get("slot")
                    raw_data = payload.get("d", [])
                    
                    # Show ALL CAN traffic
                    if can_id:
                        print(f"  CAN {can_id}: a={action} slot={slot} len={len(raw_data)} data={[hex(b) if isinstance(b, int) else b for b in raw_data[:12]]}")
                    
                    if action == "sub" and can_id == "0x7EA":
                        count += 1
                        print(f"\n[{count}] Subscription response from 0x7EA (slot {slot}):")
                        print(f"  Data length: {len(raw_data)} bytes")
                        print(f"  Raw (hex): {[hex(b) for b in raw_data[:40]]}")
                        
                        if len(raw_data) >= 2:
                            mode = raw_data[0]
                            pid = raw_data[1]
                            print(f"  Mode: 0x{mode:02X}, PID: 0x{pid:02X}")
                            
                            if mode == 0x61 and pid == 0xC3:
                                payload_data = raw_data[2:]
                                print(f"  Payload length: {len(payload_data)} bytes (need 26+ for temps)")
                                
                                if len(payload_data) >= 26:
                                    mg1_inv = payload_data[24] - 40
                                    mg2_inv = payload_data[25] - 40
                                    print(f"  ✓ MG1 Inverter Temp: {mg1_inv}°C")
                                    print(f"  ✓ MG2 Inverter Temp: {mg2_inv}°C")
                                else:
                                    print(f"  ✗ Payload too short for inverter temps!")
                    
                    elif can_id == "0x3CB":
                        if len(raw_data) >= 6:
                            temp1 = raw_data[4] if raw_data[4] <= 127 else raw_data[4] - 256
                            temp2 = raw_data[5] if raw_data[5] <= 127 else raw_data[5] - 256
                            print(f"    -> Battery temps: {temp1}°C, {temp2}°C")
                
                elif device_id == 0:
                    msg = payload.get("msg", "")
                    print(f"[SYS] {msg}")
                    
            except json.JSONDecodeError:
                pass
    
    print(f"\n--- Done. Received {count} subscription responses. ---")
    
    # Cleanup - unsubscribe
    cmd = {"id": 1, "d": {"a": "unsub", "slot": "all"}}
    ser.write((json.dumps(cmd) + "\n").encode())
    
    ser.close()

if __name__ == "__main__":
    main()
