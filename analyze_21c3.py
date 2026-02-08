#!/usr/bin/env python3
"""
Analyze PID 21C3 response data to find correct byte offsets for inverter temps.
"""

# Your actual captured data (44 bytes):
# First 20 shown: ['0x61', '0xc3', '0x3f', '0xff', '0xf', '0xa0', '0x0', '0x0', '0x3f', '0xff', '0xf', '0xa0', '0x0', '0x0', '0x0', '0x0', '0x0', '0x0', '0x0', '0x80']...

# PLEASE PASTE THE FULL 44 BYTES HERE:
# Example format: raw_data = [0x61, 0xC3, 0x3F, 0xFF, ...]
raw_data = [
    0x61, 0xC3,  # Mode response + PID (bytes 0-1)
    0x3F, 0xFF, 0x0F, 0xA0, 0x00, 0x00,  # bytes 2-7
    0x3F, 0xFF, 0x0F, 0xA0, 0x00, 0x00,  # bytes 8-13
    0x00, 0x00, 0x00, 0x00, 0x00, 0x80,  # bytes 14-19
    # FILL IN THE REST (bytes 20-43) from your capture:
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  # bytes 20-25 (PLACEHOLDER)
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  # bytes 26-31 (PLACEHOLDER)
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  # bytes 32-37 (PLACEHOLDER)
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  # bytes 38-43 (PLACEHOLDER)
]

print(f"Total bytes: {len(raw_data)}")
print()

# Extract payload (skip mode+PID bytes)
payload = raw_data[2:]
print(f"Payload length: {len(payload)} bytes")
print()

# Show all bytes with temperature interpretation (X - 40)
print("=" * 70)
print("Byte Analysis (all values as temperature: X - 40)")
print("=" * 70)
print(f"{'Idx':>4} | {'Raw':>6} | {'Hex':>6} | {'Temp':>6} | Notes")
print("-" * 70)

for i, b in enumerate(payload):
    temp = b - 40
    notes = ""
    
    # Flag potentially valid temperature readings (10°C to 100°C)
    if 10 <= temp <= 100:
        notes = "← Valid temp range!"
    elif temp == -40:
        notes = "(null/no data)"
    elif b == 0xFF:
        notes = "(0xFF = invalid)"
    elif b == 0x80:
        notes = f"(0x80 → {temp}°C)"
        
    print(f"{i:4d} | {b:6d} | 0x{b:02X}   | {temp:5d}°C | {notes}")

print()

# According to documentation, expected byte positions:
# Y (byte 24) = MG1 Inverter Temp
# Z (byte 25) = MG2 Inverter Temp
# AA (byte 26) = MG2 Motor Temp
# AB (byte 27) = MG1 Motor Temp

if len(payload) >= 28:
    print("=" * 70)
    print("Expected Inverter Temps (from documentation)")
    print("=" * 70)
    print(f"Byte 24 (Y)  = MG1 Inverter Temp: {payload[24]} → {payload[24] - 40}°C")
    print(f"Byte 25 (Z)  = MG2 Inverter Temp: {payload[25]} → {payload[25] - 40}°C")
    print(f"Byte 26 (AA) = MG2 Motor Temp:    {payload[26]} → {payload[26] - 40}°C")
    print(f"Byte 27 (AB) = MG1 Motor Temp:    {payload[27]} → {payload[27] - 40}°C")
else:
    print(f"⚠ Payload only {len(payload)} bytes, need 28+ for inverter temps")

print()

# Also decode other known fields for validation
print("=" * 70)
print("Other Known Fields (for validation)")
print("=" * 70)
if len(payload) >= 2:
    mg2_rpm = ((payload[0] * 256) + payload[1]) - 16383
    print(f"Bytes 0-1: MG2 RPM = {mg2_rpm}")
if len(payload) >= 4:
    mg2_torque = ((payload[2] * 256) + payload[3]) / 8 - 500
    print(f"Bytes 2-3: MG2 Torque = {mg2_torque} Nm")
if len(payload) >= 8:
    mg1_rpm = ((payload[6] * 256) + payload[7]) - 16383
    print(f"Bytes 6-7: MG1 RPM = {mg1_rpm}")

print()
print("=" * 70)
print("INSTRUCTIONS")
print("=" * 70)
print("1. Run your Gateway test and capture the FULL 44-byte response")
print("2. Paste the complete hex values in raw_data above")
print("3. Run this script to see which bytes contain valid temperatures")
print("4. Look for bytes where 'temp' is in a reasonable range (10-80°C)")
