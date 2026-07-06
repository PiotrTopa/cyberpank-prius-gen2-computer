#!/bin/sh
HUB_LOC="${HUB_LOC:-1-1}"
HUB_PORT="${HUB_PORT:-1}"
echo awokado88 | sudo -S uhubctl -f -l "$HUB_LOC" -p "$HUB_PORT" -a off
sleep 1
echo awokado88 | sudo -S uhubctl -f -l "$HUB_LOC" -p "$HUB_PORT" -a on &

for i in $(seq 1 200); do
    if [ -c /dev/ttyACM0 ]; then
        echo awokado88 | sudo -S chmod a+rw /dev/ttyACM0 2>/dev/null
        ~/.local/bin/mpremote fs rm main.py 2>/dev/null
        if [ $? -eq 0 ]; then
            echo "Successfully removed main.py!"
            break
        fi
    fi
    sleep 0.05
done

echo awokado88 | sudo -S chmod a+rw /dev/ttyACM0 2>/dev/null
~/.local/bin/mpremote cp ahtx0.py :ahtx0.py
~/.local/bin/mpremote cp bmp280.py :bmp280.py
~/.local/bin/mpremote cp ina219.py :ina219.py
~/.local/bin/mpremote cp main.py :main.py
~/.local/bin/mpremote reset
sleep 1
echo awokado88 | sudo -S chmod a+rw /dev/ttyACM0 2>/dev/null
echo awokado88 | sudo -S timeout 4 cat /dev/ttyACM0
