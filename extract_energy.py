"""
Extract energy-related data from NDJSON comm logs.

Extracts: battery current, voltage, power, SOC, cell/block voltages,
charge/discharge limits, temperatures, energy flow directions,
and solicited hybrid system data (21C3, 21C4, 21CE, 21CF, 21D0).
"""

import json
from pathlib import Path
from datetime import datetime


def sign_extend_12(val):
    """Sign-extend a 12-bit value to signed int."""
    if val & 0x800:
        return val - 0x1000
    return val


# ---------------------------------------------------------------------------
# Unsolicited CAN decoders
# ---------------------------------------------------------------------------

def decode_03B(data):
    """0x03B - HV Battery Current & Voltage (unsolicited, ~8ms)."""
    if len(data) < 5:
        return None
    raw_current = ((data[0] & 0x0F) << 8) | data[1]
    current_a = sign_extend_12(raw_current) * 0.1
    voltage_v = data[3]
    power_kw = round(voltage_v * current_a / 1000, 3)
    return {
        "type": "hv_battery_realtime",
        "can_id": "0x03B",
        "battery_current_A": round(current_a, 1),
        "battery_voltage_V": voltage_v,
        "power_kW": power_kw,
        "is_charging": current_a < 0,
        "is_discharging": current_a > 0,
    }


def decode_3CB(data):
    """0x3CB - SOC, Current Limits, Temperatures (unsolicited, ~100ms)."""
    if len(data) < 7:
        return None
    soc = data[3] * 0.5
    cdl = data[0]
    ccl = data[1]
    temp_low = data[4] if data[4] < 128 else data[4] - 256
    temp_high = data[5] if data[5] < 128 else data[5] - 256
    return {
        "type": "hv_battery_status",
        "can_id": "0x3CB",
        "soc_percent": soc,
        "discharge_current_limit_A": cdl,
        "charge_current_limit_A": ccl,
        "battery_temp_low_C": temp_low,
        "battery_temp_high_C": temp_high,
    }


def decode_3CD(data):
    """0x3CD - Fault Code and Pack Voltage (unsolicited, ~100ms)."""
    if len(data) < 5:
        return None
    fault = (data[0] << 8) | data[1]
    voltage_raw = (data[3] << 8) | data[4]
    return {
        "type": "hv_battery_voltage_raw",
        "can_id": "0x3CD",
        "fault_code": fault,
        "voltage_raw": voltage_raw,
    }


def decode_3B6(data):
    """0x3B6 - Energy Flow Arrows (unsolicited)."""
    if len(data) < 7:
        return None
    flags = data[5]
    return {
        "type": "energy_flow",
        "can_id": "0x3B6",
        "engine_to_wheels": bool(flags & 0x08),
        "battery_to_motor": bool(flags & 0x10),
        "motor_to_battery_regen": bool(flags & 0x20),
        "engine_to_battery": bool(flags & 0x40),
        "battery_to_wheels": bool(flags & 0x80),
        "flow_flags_raw": f"0x{flags:02X}",
    }


def decode_3C8(data):
    """0x3C8 - System Status / Alternative SOC (unsolicited, ~70ms)."""
    if len(data) < 5:
        return None
    ice_rpm_target = 256 * data[2] + data[3]
    return {
        "type": "system_status",
        "can_id": "0x3C8",
        "ice_rpm_target": ice_rpm_target,
        "byte4_soc_hint": data[4] if len(data) > 4 else None,
    }


def decode_3C9(data):
    """0x3C9 - HV Battery Cell Data (unsolicited, raw broadcast)."""
    return {
        "type": "hv_battery_cell_data_raw",
        "can_id": "0x3C9",
        "data_hex": [f"0x{b:02X}" for b in data],
        "data_length": len(data),
    }


def decode_038(data):
    """0x038 - ICE Running Status (~40ms)."""
    if len(data) < 3:
        return None
    ice_running = data[1] > 0
    return {
        "type": "ice_status",
        "can_id": "0x038",
        "ice_running": ice_running,
        "status_byte2": f"0x{data[2]:02X}",
    }


def decode_039(data):
    """0x039 - ICE Coolant Temperature (~88ms)."""
    if len(data) < 1:
        return None
    return {
        "type": "ice_coolant_temp",
        "can_id": "0x039",
        "coolant_temp_C": data[0],
    }


# ---------------------------------------------------------------------------
# Solicited PID decoders (payload = bytes after 0x61 + PID byte)
# ---------------------------------------------------------------------------

def decode_solicited_21C3(payload):
    """PID 21C3 - Hybrid System Data from ECU 0x7E2 (response 0x7EA).

    MG1/MG2 RPM & torque, inverter/motor temperatures,
    HV battery voltage and current.
    """
    if len(payload) < 10:
        return None
    mg2_rpm = (payload[0] * 256 + payload[1]) - 16383
    mg2_torque = (payload[2] * 256 + payload[3]) / 8 - 500
    mg1_rpm = (payload[6] * 256 + payload[7]) - 16383
    mg1_torque = (payload[8] * 256 + payload[9]) / 8 - 500

    result = {
        "type": "hybrid_system_21C3",
        "pid": "21C3",
        "ecu": "0x7EA",
        "mg2_rpm": mg2_rpm,
        "mg2_torque_Nm": round(mg2_torque, 1),
        "mg1_rpm": mg1_rpm,
        "mg1_torque_Nm": round(mg1_torque, 1),
    }

    if len(payload) >= 25:
        result["mg1_inverter_temp_C"] = payload[24] - 40
    if len(payload) >= 26:
        result["mg2_inverter_temp_C"] = payload[25] - 40
    if len(payload) >= 27:
        result["mg2_motor_temp_C"] = payload[26] - 40
    if len(payload) >= 28:
        result["mg1_motor_temp_C"] = payload[27] - 40
    if len(payload) >= 29:
        result["hv_battery_voltage_V"] = 2 * payload[28]
    if len(payload) >= 31:
        result["hv_battery_current_A"] = 2 * payload[30] - 256

    return result


def decode_solicited_21C4(payload):
    """PID 21C4 - Additional Hybrid Data (boost voltages, converter temp)."""
    if len(payload) < 6:
        return None
    return {
        "type": "hybrid_boost_21C4",
        "pid": "21C4",
        "ecu": "0x7EA",
        "accelerator_pedal_percent": round(100 * payload[2] / 255, 1),
        "voltage_before_boost_V": 2 * payload[3],
        "voltage_after_boost_V": 2 * payload[4],
        "converter_temp_C": payload[5] - 40,
    }


def decode_solicited_21CE(payload):
    """PID 21CE - HV Battery Detail from ECU 0x7E3 (response 0x7EB).

    Precise SOC, battery current, and 14 block voltages.
    Each block is 2 NiMH modules in series (~14.4V nominal).
    """
    if len(payload) < 31:
        return None
    soc = 0.5 * payload[0]
    current = (256 * payload[1] + payload[2]) / 100 - 327.68

    block_voltages = []
    for i in range(14):
        hi = payload[3 + i * 2]
        lo = payload[4 + i * 2]
        v = round((256 * hi + lo) / 100 - 327.68, 2)
        block_voltages.append(v)

    total_voltage = round(sum(block_voltages), 2)

    return {
        "type": "hv_battery_detail_21CE",
        "pid": "21CE",
        "ecu": "0x7EB",
        "soc_percent": soc,
        "battery_current_A": round(current, 2),
        "pack_voltage_sum_V": total_voltage,
        "block_voltages_V": block_voltages,
        "min_block_voltage_V": min(block_voltages),
        "max_block_voltage_V": max(block_voltages),
        "block_voltage_spread_V": round(max(block_voltages) - min(block_voltages), 3),
    }


def decode_solicited_21CF(payload):
    """PID 21CF - Battery Temps & Delta SOC from ECU 0x7E3."""
    if len(payload) < 8:
        return None
    air_intake_temp = round((256 * payload[0] + payload[1]) / 100 - 327.68, 2)
    aux_battery_v = round(0.2 * payload[3] - 25.6, 1)
    charge_limit_kw = payload[4] - 64
    discharge_limit_kw = payload[5] - 64
    delta_soc = round(0.01 * payload[6], 2)
    fan_speed = payload[7]

    return {
        "type": "battery_limits_21CF",
        "pid": "21CF",
        "ecu": "0x7EB",
        "air_intake_temp_C": air_intake_temp,
        "aux_battery_voltage_V": aux_battery_v,
        "charge_limit_kW": charge_limit_kw,
        "discharge_limit_kW": discharge_limit_kw,
        "delta_soc_percent": delta_soc,
        "fan_speed_level": fan_speed,
    }


def decode_solicited_21D0(payload):
    """PID 21D0 - Block Internal Resistances from ECU 0x7E3."""
    if len(payload) < 14:
        return None
    resistances = [round(0.001 * payload[i], 4) for i in range(14)]
    return {
        "type": "block_resistances_21D0",
        "pid": "21D0",
        "ecu": "0x7EB",
        "block_resistances_ohm": resistances,
        "min_resistance_ohm": min(resistances),
        "max_resistance_ohm": max(resistances),
    }


# ---------------------------------------------------------------------------
# Decoder tables
# ---------------------------------------------------------------------------

CAN_DECODERS = {
    0x03B: decode_03B,
    0x3CB: decode_3CB,
    0x3CD: decode_3CD,
    0x3B6: decode_3B6,
    0x3C8: decode_3C8,
    0x3C9: decode_3C9,
    0x038: decode_038,
    0x039: decode_039,
}

ENERGY_CAN_IDS = set(CAN_DECODERS.keys())

SOLICITED_DECODERS = {
    0xC3: decode_solicited_21C3,
    0xC4: decode_solicited_21C4,
    0xCE: decode_solicited_21CE,
    0xCF: decode_solicited_21CF,
    0xD0: decode_solicited_21D0,
}

# ECU response CAN IDs that carry energy-relevant solicited data
SOLICITED_ECUS = {"0x7EA", "0x7EB"}


# ---------------------------------------------------------------------------
# Solicited response decoder
# ---------------------------------------------------------------------------

def decode_solicited_response(d):
    """Decode a solicited (subscription) CAN response.

    The gateway delivers already-reassembled ISO-TP payloads in format:
        {"a": "sub", "slot": N, "i": "0x7EA", "d": [0x61, PID, ...]}

    First byte is the service response (0x61 = response to 0x21 Toyota extended).
    Second byte is the PID byte (0xC3, 0xCE, etc.).
    Remaining bytes are the payload.
    """
    can_id = d.get("i", "")
    raw = d.get("d", [])
    if not raw or len(raw) < 3:
        return None

    # Only process responses from hybrid / battery ECUs
    if can_id not in SOLICITED_ECUS:
        return None

    first_byte = raw[0]

    # ISO-TP reassembled format: first byte is mode (>= 0x40)
    if first_byte >= 0x40:
        mode = first_byte
        pid_byte = raw[1]
        payload = raw[2:]
    # Single-frame format: first byte is PCI length (0x01-0x07)
    elif first_byte <= 0x07 and len(raw) >= 3:
        mode = raw[1]
        pid_byte = raw[2]
        payload = raw[3:]
    else:
        return None

    # Only handle mode 0x61 (response to Toyota extended 0x21)
    if mode != 0x61:
        return None

    decoder = SOLICITED_DECODERS.get(pid_byte)
    if decoder:
        return decoder(payload)
    return None


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_log_file(filepath):
    """Process a single NDJSON log file and extract energy-related records."""
    records = []
    metadata = None

    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Capture metadata (only the opening header, not the closing summary)
            if "meta" in msg:
                if msg.get("meta") == "comm_log":
                    metadata = msg
                continue

            ts = msg.get("ts")
            direction = msg.get("dir")
            msg_id = msg.get("id")
            d = msg.get("d", {})
            seq = msg.get("seq")

            # --- CAN bus messages (id=1) ---
            if msg_id == 1 and isinstance(d, dict):
                can_id_str = d.get("i", "")
                can_data = d.get("d", [])

                # Solicited subscription response
                if d.get("a") == "sub":
                    result = decode_solicited_response(d)
                    if result:
                        result["timestamp"] = ts
                        result["seq"] = seq
                        result["source"] = "can_solicited"
                        records.append(result)
                    continue

                # Unsolicited broadcast
                if can_id_str.startswith("0x"):
                    try:
                        can_id = int(can_id_str, 16)
                    except ValueError:
                        continue
                else:
                    continue

                if can_id in ENERGY_CAN_IDS:
                    decoder = CAN_DECODERS.get(can_id)
                    if decoder:
                        decoded = decoder(can_data)
                        if decoded:
                            decoded["timestamp"] = ts
                            decoded["seq"] = seq
                            decoded["source"] = "can_unsolicited"
                            records.append(decoded)

            # --- Outgoing display state (id=110, dir=OUT) ---
            if msg_id == 110 and direction == "OUT" and isinstance(d, dict):
                msg_type = d.get("t")
                if msg_type == "E":
                    rec = {
                        "type": "display_energy_state",
                        "timestamp": ts,
                        "source": "display_out",
                        "soc_display": d.get("soc"),
                        "motor_generator_kw": d.get("mg"),
                        "fuel_consumption": d.get("fl"),
                        "brake_regen": d.get("br"),
                        "speed": d.get("spd"),
                        "pointer_position": d.get("ptr"),
                        "lpg_mode": d.get("lpg"),
                        "ice_running": d.get("ice"),
                    }
                    records.append(rec)

    return metadata, records


def create_output(metadata, records, output_path, source_file):
    """Write extracted energy data to a formatted JSON file."""
    soc_values = []
    voltage_values = []
    current_values = []
    power_values = []
    block_voltage_records = []
    solicited_21c3 = []

    for r in records:
        rtype = r.get("type")
        if rtype == "hv_battery_status":
            soc_values.append(r["soc_percent"])
        elif rtype == "hv_battery_realtime":
            voltage_values.append(r["battery_voltage_V"])
            current_values.append(r["battery_current_A"])
            power_values.append(r["power_kW"])
        elif rtype == "hv_battery_detail_21CE":
            soc_values.append(r["soc_percent"])
            block_voltage_records.append(r)
        elif rtype == "hybrid_system_21C3":
            solicited_21c3.append(r)

    summary = {
        "source_file": source_file,
        "recording_started": metadata.get("created") if metadata else None,
        "total_energy_records": len(records),
        "record_type_counts": {},
    }

    type_counts = {}
    for r in records:
        t = r.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
    summary["record_type_counts"] = type_counts

    if soc_values:
        summary["soc_range"] = {
            "min_percent": min(soc_values),
            "max_percent": max(soc_values),
            "first_percent": soc_values[0],
            "last_percent": soc_values[-1],
        }
    if voltage_values:
        non_zero = [v for v in voltage_values if v > 0]
        if non_zero:
            summary["hv_battery_voltage_range"] = {
                "min_V": min(non_zero),
                "max_V": max(non_zero),
                "avg_V": round(sum(non_zero) / len(non_zero), 1),
            }
    if current_values:
        summary["hv_battery_current_range"] = {
            "min_A": round(min(current_values), 1),
            "max_A": round(max(current_values), 1),
            "avg_A": round(sum(current_values) / len(current_values), 1),
        }
    if power_values:
        summary["hv_battery_power_range"] = {
            "min_kW": round(min(power_values), 3),
            "max_kW": round(max(power_values), 3),
            "avg_kW": round(sum(power_values) / len(power_values), 3),
        }
    if block_voltage_records:
        all_blocks = []
        for bvr in block_voltage_records:
            all_blocks.extend(bvr["block_voltages_V"])
        if all_blocks:
            summary["cell_block_voltage_range"] = {
                "min_V": min(all_blocks),
                "max_V": max(all_blocks),
                "spread_V": round(max(all_blocks) - min(all_blocks), 3),
                "total_readings": len(block_voltage_records),
            }
    if solicited_21c3:
        voltages_21c3 = [r["hv_battery_voltage_V"] for r in solicited_21c3 if "hv_battery_voltage_V" in r]
        if voltages_21c3:
            summary["hv_voltage_from_hybrid_ecu"] = {
                "min_V": min(voltages_21c3),
                "max_V": max(voltages_21c3),
                "avg_V": round(sum(voltages_21c3) / len(voltages_21c3), 1),
            }

    output = {
        "_description": "Extracted energy flow data from Prius Gen 2 CAN/AVC-LAN recording",
        "_vehicle": "Toyota Prius Gen 2 (NHW20)",
        "_can_ids_decoded": {
            "0x03B": "HV battery current & voltage (8ms, unsolicited)",
            "0x3CB": "SOC, current limits, battery temps (100ms, unsolicited)",
            "0x3CD": "Fault code & pack voltage raw (100ms, unsolicited)",
            "0x3B6": "Energy flow direction arrows (unsolicited)",
            "0x3C8": "System status / ICE RPM target (70ms, unsolicited)",
            "0x3C9": "HV battery cell data raw (unsolicited)",
            "0x038": "ICE running status (40ms, unsolicited)",
            "0x039": "ICE coolant temperature (88ms, unsolicited)",
            "21C3":  "Hybrid system: MG1/MG2 RPM, torque, temps, HV V/I (solicited from 0x7E2)",
            "21C4":  "Boost voltages, converter temp (solicited from 0x7E2)",
            "21CE":  "Battery SOC, current, 14 block voltages (solicited from 0x7E3)",
            "21CF":  "Battery limits, delta SOC, aux voltage, fan (solicited from 0x7E3)",
            "21D0":  "14 block internal resistances (solicited from 0x7E3)",
        },
        "_extracted_at": datetime.now().isoformat(),
        "summary": summary,
        "records": records,
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"  Written {len(records)} records to {output_path}")
    print(f"  Record types: {type_counts}")
    if soc_values:
        print(f"  SOC range: {min(soc_values):.1f}% - {max(soc_values):.1f}%")
    if block_voltage_records:
        print(f"  Block voltage readings: {len(block_voltage_records)}")
    if solicited_21c3:
        print(f"  Hybrid system (21C3) readings: {len(solicited_21c3)}")


def main():
    log_dir = Path(__file__).parent / "logs"

    files = [
        "comm_20260219_144720.ndjson",
        "comm_20260219_202855.ndjson",
    ]

    for fname in files:
        src = log_dir / fname
        if not src.exists():
            print(f"SKIP: {src} not found")
            continue

        stem = fname.replace("comm_", "energy_").replace(".ndjson", ".json")
        out = log_dir / stem
        print(f"\nProcessing: {fname}")
        metadata, records = process_log_file(src)
        create_output(metadata, records, out, fname)


if __name__ == "__main__":
    main()
