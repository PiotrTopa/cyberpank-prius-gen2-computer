import json

def analyze_gear():
    speed = 0
    last_b4 = -1
    last_b5 = -1
    
    print("Time | Speed | 0x120 Bytes")
    
    with open("assets/data/full.ndjson", "r") as f:
        for line in f:
            try:
                entry = json.loads(line)
                can_data = entry.get("d", {})
                can_id_str = can_data.get("i")
                data = can_data.get("d")
                
                if not can_id_str or not data:
                    continue
                    
                can_id = int(can_id_str, 16)
                
                if can_id == 0x0B4 and len(data) >= 7:
                     speed = ((data[5] << 8) | data[6]) / 100.0
                
                if can_id == 0x120 and len(data) >= 6:
                    ts = entry.get("ts")
                    b4 = data[4]
                    b5 = data[5]
                    
                    # Only print if something changed
                    if b4 != last_b4 or b5 != last_b5:
                        print(f"{ts:<8} | {speed:>5.1f} | {data}")
                        last_b4 = b4
                        last_b5 = b5

            except Exception as e:
                pass

if __name__ == "__main__":
    analyze_gear()
