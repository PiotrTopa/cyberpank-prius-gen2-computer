import serial, time, subprocess

import os

hub_loc = os.environ.get("HUB_LOC", "1-1")
hub_port = os.environ.get("HUB_PORT", "1")

subprocess.run(f"echo awokado88 | sudo -S uhubctl -f -l {hub_loc} -p {hub_port} -a off", shell=True, stdout=subprocess.DEVNULL)
time.sleep(1)
subprocess.Popen(f"echo awokado88 | sudo -S uhubctl -f -l {hub_loc} -p {hub_port} -a on", shell=True, stdout=subprocess.DEVNULL)

s = None
t_end = time.time() + 4.0
while time.time() < t_end:
    try:
        if s is None:
            s = serial.Serial("/dev/ttyACM0", 115200, timeout=0)
        s.write(b"\x03")
    except Exception:
        if s: 
            try: s.close()
            except: pass
        s = None
        time.sleep(0.005)

if s is not None:
    try:
        s.write(b"\r\nimport os; os.remove(\"main.py\")\r\n")
        s.flush()
    except Exception:
        pass
    print("Rescue sent!")
else:
    print("Never connected!")
