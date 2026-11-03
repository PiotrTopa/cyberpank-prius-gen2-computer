# Relay board + I2C — current state and change plan

Status: **OPEN — final port assignment not decided, no code changed yet.**
Code changes go to one Fable lane once the open questions are answered.

## Hardware as it is

**I2C0:** GP0 = SDA, GP1 = SCL (normal assignment).

| Address | Device | Firmware field |
|---|---|---|
| `0x20` | PCF8574 relay expander | `rly` |
| `0x40` | INA219 (sees the 12 V supply) | `v`, `ma`, `mah` |
| `0x76` | BMP280, outside/cabin | `bmp2_t`, `bmp2_p` |

The inside-box BMP280 (`0x77`) and the AHT20 (`0x38`) are not fitted.

**Relay board:** 8-channel opto board with 12 V coils (HW-281 type), active-low inputs.
- IN1..IN8 go straight to PCF8574 P0..P7, so **board relay N = PCF pin P(N−1)**.
- JD-VCC = 12 V, always on; JD jumper removed.
- VCC (opto/input side) comes from the **RP2040-Zero 5 V pin**. When the RP2040 is unpowered, all coils are released.
- PCF8574 power-up state is 0xFF, so all coils are released after any restart.

**Loads:** only relay 1 is wired NC; relays 2–8 are NO.
- Relay 1 (NC): the powerbox's own supply, and nothing else. Energised = hard restart of the powerbox (below); de-energised (incl. RP2040 off or reset) = powered.
- Relay 2 (NO): MFD Raspberry Pi Zero v1.3 (≈ +100–115 mA at 12 V while running). Tested: relay on → Pi draws within ~1 s → USB gadget link (host `enx021e4d000001`) and ping at 49–50 s; three hard off/on cycles gave one clean enumeration each, no xHCI wedge (Pi gadget binds late, see `cyberpunk-prius-gateway/raspberry_pi_config/README.md`). Every hard cut risks the SD card: the backend must keep `ssh … poweroff` + wait before cutting the relay.
- Relay 3 (NO, fw ch2 with today's map): powers a USB 2.0 hub (`1a40:0101`, "USB2.0 HUB", on bench-hub port 3), with the CAN gateway (`2e8a:000a` "Prius Gateway", fw 3.2.0, hub port 3) and the comma.ai panda (`bbaa:ddcc`, hw type cuatro, fw `DEV-f120999e-DEBUG`, hub port 4) behind it. The gateway is powered from this hub, so relay 3 off = gateway off. The panda is for unsolicited CAN reading; its use is not designed yet. Tested 2026-11-03 off the car: relay on → 50 → 155 mA at 12 V; the hub enumerates at high speed within about 6 s, the gateway at about 7 s (sends `GW_HB` every second, CAN in LISTEN mode, no errors) and the panda at about 9 s (answers hw type, version and health; no CAN traffic, since no bus is connected).
- Relay 4 (NO, fw ch1 with today's map): a second USB 2.0 hub (VIA Labs `2109:2817`, 4 ports, on bench-hub port 4), for several devices (not yet assigned). Tested 2026-11-03: with the relay off the hub is absent from USB; relay on → +84 mA at 12 V, high-speed enumeration in about 6 s. Its power comes only through the relay now (no VBUS back-feed from the upstream cable).
- **Board relay 1 energised = hard restart of the powerbox.** The RP2040 loses power, the PCF comes back at 0xFF, relay 1 releases, and power returns by itself. Measured from command: USB drops after 0.4 s, re-enumerates after 1.0 s, firmware STATUS after 4.9 s (3 s of that is `SAFE_BOOT_DELAY_S`).

## What the code must take into account

1. **Pin map.** `RELAY_PIN_MAP` (`main.py:146`) is `{1:3, 2:2, 3:1, 4:0, 5:4, 6:5, 7:6, 8:7}`, so firmware ch1..4 are reversed against the board (fw ch4 "GATEWAY" = board relay 1). Target: `{n: n-1}`, fw channel N = board relay N, with the `RELAY_CH_*` constants taken from the final port table.
2. **NC contacts.** For an NC load, "on" means coil released (pin HIGH). `set_relay` and `relay_states` must keep `rly` meaning "1 = load powered".
3. **Restart relay.** Expose it as a dedicated command (name TBD), not as an ordinary `relay ch=N`, so the backend cannot fire it by accident. Report it separately or leave it out of `rly`. Allow about 5 s until the board is back.
4. **Backend mirrors the channel numbers.** `backend/port_power.py` (`gateway_relay_ch=4`, `mfd_relay_ch=3`, `sdr_relay_ch=2`), `backend/mfd_power.py` (ch3), RS485 = ch5 (`docs/SATELLITE_POWER.md`, `powerbox.rs485_power`). With the new map: MFD = ch2; gateway = ch3 (the relay-3 hub, shared with the panda, so switching the gateway also switches the panda); ch1 is the powerbox restart, not a load, so the backend must not treat it as the gateway. The repo has **uncommitted backend changes**: build on them, don't discard them.
5. **Docs.** `main.py` header (around lines 41 and 130–145), README, `POCO/POWER.md`: the relay table, NC loads, VCC-from-RP2040, the restart relay.
6. **Tests.** `.venv/bin/python -m pytest tests` (pygame test `--ignore`). Add cases for the pin map, NC inversion, and the restart channel being rejected by plain `relay`.
7. **Relay state is not read back from the chip.** `relay_states()` returns the cached byte `self._port`, and `PCF8574.pin()` updates that cache *before* the I2C write. Observed 2026-11-02: after an off/on cycle of relay 2 the firmware reported `rly` ch on while the relay was physically off (Pi drawing nothing); resending the command fixed it. The PCF either reset or a write failed silently. The code must update the cache only after a successful write, report `rly` from a chip read-back, and periodically re-assert the wanted state (or detect a PCF reset by reading it back).
8. **Host heartbeats.** Firmware TX is gated on them (`{"a":"hb","n":N}`). Without heartbeats the port is silent. Bench tools must send them.

## Open questions

- Q1. Final port table: which load on which board relay (1–8), NO or NC for each?
- Q3. Restart scope. Relay VCC comes from the RP2040 5 V, so while the powerbox restarts every NO relay releases: the MFD Pi (hard cut) and the relay-3 hub (gateway + panda) drop too. Is that acceptable, or does the restart need to keep them up?
- Q4. Fit the inside-box BMP280 `0x77` / AHT20 `0x38`, or drop them from the firmware?
