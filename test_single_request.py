#!/usr/bin/env python3
"""
Simple single-request test for Gateway OBD-II.
Tests if Gateway can send request and receive response.
Fixed version with proper timing and unique request IDs.
"""

import serial
import json
import time
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"
BAUD = 1000000


def drain_buffer(ser, timeout=0.5):
    """Read all available data with timeout."""
    messages = []
    end_time = time.time() + timeout
    while time.time() < end_time:
        if ser.in_waiting:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line:
                messages.append(line)
        else:
            time.sleep(0.01)
    return messages


def send_and_wait(ser, cmd, timeout=1.0):
    """Send command and wait for response with matching ID."""
    req_id = cmd.get("id", 0)
    ser.write((json.dumps(cmd) + "\n").encode())
    
    responses = []
    end_time = time.time() + timeout
    
    while time.time() < end_time:
        if ser.in_waiting:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                # Check if this is a response to our request
                if data.get("id") == req_id:
                    payload = data.get("d", {})
                    if payload.get("a") == "resp":
                        responses.append(data)
                        # If we got an error, might still get more responses
                        if "err" not in payload:
                            break  # Got valid response
            except json.JSONDecodeError:
                pass
        else:
            time.sleep(0.01)
    
    return responses


def main():
    print(f"Opening {PORT} at {BAUD} baud...")
    ser = serial.Serial(PORT, BAUD, timeout=0.1)
    time.sleep(0.5)
    
    # Drain startup messages
    drain_buffer(ser, 0.3)
    
    print("\n--- Switching to Normal mode ---")
    cmd = {"id": 1, "d": {"a": "mode", "m": "normal"}}
    ser.write((json.dumps(cmd) + "\n").encode())
    time.sleep(0.5)
    
    # Drain mode change messages
    for line in drain_buffer(ser, 0.3):
        if "CAN_MODE" in line or "NORMAL" in line:
            print(f"  ✓ {line[:80]}")
    
    # Test 1: Coolant Temp (single-frame, Engine ECU)
    print("\n" + "="*60)
    print("Test 1: Coolant Temperature (PID 0105) -> Engine ECU")
    print("="*60)
    cmd = {
        "id": 1,
        "d": {
            "a": "req",
            "i": "0x7E0",
            "d": [2, 1, 5, 0, 0, 0, 0, 0],
            "r": ["0x7E8"],
            "t": 1000
        }
    }
    print(f"TX: 0x7E0 -> [02 01 05 00 00 00 00 00]")
    responses = send_and_wait(ser, cmd, timeout=1.5)
    
    if responses:
        for resp in responses:
            payload = resp.get("d", {})
            if "err" in payload:
                print(f"  ❌ ERROR: {payload['err']}")
            else:
                raw = payload.get("d", [])
                print(f"  RX: 0x7E8 <- {[hex(b) for b in raw]}")
                if len(raw) >= 4 and raw[0] >= 3 and raw[1] == 0x41 and raw[2] == 0x05:
                    temp = raw[3] - 40
                    print(f"  ✓ Coolant Temp: {temp}°C")
    else:
        print("  ❌ No response received!")
    
    time.sleep(0.3)
    drain_buffer(ser, 0.2)
    
    # Test 2: RPM (single-frame, broadcast)
    print("\n" + "="*60)
    print("Test 2: Engine RPM (PID 010C) -> Broadcast")
    print("="*60)
    cmd = {
        "id": 1,
        "d": {
            "a": "req",
            "i": "0x7DF",
            "d": [2, 1, 12, 0, 0, 0, 0, 0],
            "r": ["0x7E8"],
            "t": 1000
        }
    }
    print(f"TX: 0x7DF -> [02 01 0C 00 00 00 00 00]")
    responses = send_and_wait(ser, cmd, timeout=1.5)
    
    if responses:
        for resp in responses:
            payload = resp.get("d", {})
            if "err" in payload:
                print(f"  ❌ ERROR: {payload['err']}")
            else:
                raw = payload.get("d", [])
                print(f"  RX: {payload.get('i', '???')} <- {[hex(b) for b in raw]}")
                if len(raw) >= 5 and raw[1] == 0x41 and raw[2] == 0x0C:
                    rpm = ((raw[3] * 256) + raw[4]) / 4
                    print(f"  ✓ RPM: {rpm}")
    else:
        print("  ❌ No response received!")
    
    time.sleep(0.3)
    drain_buffer(ser, 0.2)
    
    # Test 3: Hybrid Battery SOC (Toyota extended PID)
    print("\n" + "="*60)
    print("Test 3: HV Battery SOC (PID 21CF) -> Hybrid ECU")
    print("="*60)
    cmd = {
        "id": 1,
        "d": {
            "a": "req",
            "i": "0x7E2",
            "d": [3, 0x21, 0xCF, 0, 0, 0, 0, 0],
            "r": ["0x7EA"],
            "t": 1000
        }
    }
    print(f"TX: 0x7E2 -> [03 21 CF 00 00 00 00 00]")
    responses = send_and_wait(ser, cmd, timeout=1.5)
    
    if responses:
        for resp in responses:
            payload = resp.get("d", {})
            if "err" in payload:
                print(f"  ❌ ERROR: {payload['err']}")
            else:
                raw = payload.get("d", [])
                print(f"  RX: {payload.get('i', '???')} <- {[hex(b) for b in raw]}")
                # Response format: [len, 0x61, 0xCF, data...]
                if len(raw) >= 4 and raw[1] == 0x61 and raw[2] == 0xCF:
                    soc = raw[3]
                    print(f"  ✓ SOC: {soc}%")
    else:
        print("  ❌ No response received!")
    
    time.sleep(0.3)
    drain_buffer(ser, 0.2)
    
    # Test 4: Multi-frame Inverter Temps (PID 21C3 - 44 bytes per Gateway)
    print("\n" + "="*60)
    print("Test 4: Inverter Temps (PID 21C3) -> Hybrid ECU [ISO-TP]")
    print("="*60)
    cmd = {
        "id": 1,
        "d": {
            "a": "req",
            "i": "0x7E2",
            "d": [3, 0x21, 0xC3, 0, 0, 0, 0, 0],
            "r": ["0x7EA"],
            "t": 2000,
            "isotp": True
        }
    }
    print(f"TX: 0x7E2 -> [03 21 C3 00 00 00 00 00] (expecting 44-byte multi-frame response)")
    responses = send_and_wait(ser, cmd, timeout=2.5)
    
    if responses:
        for resp in responses:
            payload = resp.get("d", {})
            if "err" in payload:
                print(f"  ❌ ERROR: {payload['err']}")
            else:
                raw = payload.get("d", [])
                print(f"  RX: {payload.get('i', '???')} <- {len(raw)} bytes")
                print(f"      Full: {[hex(b) for b in raw]}")
                
                # Decode PID 21C3 response
                # Structure: [0x61, 0xC3, payload...]
                # Payload byte positions per Toyota docs:
                # A,B (0-1): MG2 RPM, C,D (2-3): MG2 Torque
                # G,H (6-7): MG1 RPM, I,J (8-9): MG1 Torque
                # Y (24): MG1 Inverter Temp, Z (25): MG2 Inverter Temp
                # AA (26): MG2 Motor Temp, AB (27): MG1 Motor Temp
                
                if len(raw) >= 2 and raw[0] == 0x61 and raw[1] == 0xC3:
                    payload_data = raw[2:]  # Skip service + PID bytes
                    print(f"\n      Payload ({len(payload_data)} bytes):")
                    
                    if len(payload_data) >= 2:
                        mg2_rpm = ((payload_data[0] * 256) + payload_data[1]) - 16383
                        print(f"        MG2 RPM (bytes 0-1): {mg2_rpm}")
                    
                    if len(payload_data) >= 4:
                        mg2_torque = ((payload_data[2] * 256) + payload_data[3]) / 8 - 500
                        print(f"        MG2 Torque (bytes 2-3): {mg2_torque:.1f} Nm")
                    
                    if len(payload_data) >= 8:
                        mg1_rpm = ((payload_data[6] * 256) + payload_data[7]) - 16383
                        print(f"        MG1 RPM (bytes 6-7): {mg1_rpm}")
                    
                    if len(payload_data) >= 10:
                        mg1_torque = ((payload_data[8] * 256) + payload_data[9]) / 8 - 500
                        print(f"        MG1 Torque (bytes 8-9): {mg1_torque:.1f} Nm")
                    
                    # Inverter and Motor Temperatures
                    if len(payload_data) >= 25:
                        mg1_inv = payload_data[24] - 40
                        print(f"        MG1 Inverter Temp (byte 24): {mg1_inv}°C (raw=0x{payload_data[24]:02X})")
                    else:
                        print(f"        MG1 Inverter Temp: MISSING (need byte 24, have {len(payload_data)})")
                    
                    if len(payload_data) >= 26:
                        mg2_inv = payload_data[25] - 40
                        print(f"        MG2 Inverter Temp (byte 25): {mg2_inv}°C (raw=0x{payload_data[25]:02X})")
                    else:
                        print(f"        MG2 Inverter Temp: MISSING (need byte 25, have {len(payload_data)})")
                    
                    if len(payload_data) >= 27:
                        mg2_motor = payload_data[26] - 40
                        print(f"        MG2 Motor Temp (byte 26): {mg2_motor}°C (raw=0x{payload_data[26]:02X})")
                    
                    if len(payload_data) >= 28:
                        mg1_motor = payload_data[27] - 40
                        print(f"        MG1 Motor Temp (byte 27): {mg1_motor}°C (raw=0x{payload_data[27]:02X})")
                    
                    # HV Battery
                    if len(payload_data) >= 29:
                        hv_voltage = 2 * payload_data[28]
                        print(f"        HV Voltage (byte 28): {hv_voltage}V")
                    
                    if len(payload_data) >= 31:
                        hv_current = 2 * payload_data[30] - 256
                        print(f"        HV Current (byte 30): {hv_current}A")
    else:
        print("  ❌ No response received!")
    
    print("\n" + "="*60)
    print("Summary")
    print("="*60)
    print("If Test 1-3 work: Single-frame OBD-II is working")
    print("If Test 4 fails: ISO-TP multi-frame needs Gateway fix")
    print("If all fail: Check car is in IG-ON or READY mode")
    
    ser.close()


if __name__ == "__main__":
    main()
