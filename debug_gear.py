import json
import statistics

def analyze_gear():
    speed = 0
    gear_candidates = {}
    
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
                     s = ((data[5] << 8) | data[6]) / 100.0
                     speed = s
                
                if can_id == 0x120 and len(data) >= 5:
                    b4 = data[4] # Alleged gear
                    # byte 3 or 5 might be it too?
                    timestamp = entry.get("ts")
                    
                    if speed > 5: # Moving significant speed
                        print(f"Time: {timestamp}, Speed: {speed:.1f}, 0x120 Data: {data}")
                        return # Just see the first time it moves

            except Exception as e:
                pass

if __name__ == "__main__":
    analyze_gear()
