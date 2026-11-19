# roadsense device configuration of this car

The car's own values for roadsense, kept here so a rebuilt phone gets them back
(`/etc/roadsense/` on the phone; roadsense's installer never overwrites existing files there).

| File | Installed as | State |
|---|---|---|
| `calib.json` | `/etc/roadsense/calib.json` | camera mount: pitch and yaw measured 2026-11-19 from a drive (the road model's own motion, roadsense `tools/vision_eval/calib_from_pose.py`); roll 0 and height 1.22 m are nominal |

Install by hand: `sudo install -m 644 POCO/roadsense/calib.json /etc/roadsense/calib.json` (the road model re-reads it within 2 s).

The camera does not keep its aim across remounts (pitch read +1.3 deg on 2026-11-12 to 17 and +3.2 deg on 2026-11-19;
yaw -3.2 to -3.7 deg throughout). Re-measure after touching the camera and update this file. The previous nominal
file (all angles 0) is on the phone as `/etc/roadsense/calib.json.bak-20261119-nominal`.
