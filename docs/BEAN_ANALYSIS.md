# BEAN analysis: owner's NHW20 (EU LHD, smart entry) vs published JP dumps

Date: 2026-11-13. Inputs: `docs/data/bean/2026-11-13_session30_doors_locks.ndjson.gz` (4,934 frames,
16:18:29–16:37:06 CET, car OFF), `docs/data/bean/2026-11-13_remote_test.ndjson.gz` (350 frames,
15:24:34–15:24:59 CET, car OFF), the bean_tools NHW20 JP dumps (`tests/fixtures/bean_nhw20_frames.txt.gz`,
8,627 frames: 3 power-OFF dumps, 1 IG-ON dump, 1 READY dump) and the Toyota repair manual RM1076E.
Times written `mm:ss` are minutes:seconds after 16:00 CET (remote test: after 15:00 CET).
All frames in both of our captures have valid CRCs. Scripts: `tools/bean_analysis/` (see §8).

Confidence labels:

| Label | Meaning |
|---|---|
| **[C]** | Confirmed on our car: follows a logged owner action, or repeats at every occurrence of a trigger in our captures |
| **[P]** | Primary source (Toyota RM1076E / NCF) |
| **[X]** | Same ID and encoding as a published decode for another Toyota/Lexus model |
| **[I]** | Inference from timing/structure. A hypothesis, not a fact |
| **[?]** | Unknown |

---

## 0. Outside temperature: the answer

**It cannot be read from BEAN with the car OFF. It appears only with the power switch ON (IG-ON;
READY not needed).**

1. **The sensor is read by the ECM, not by the A/C amplifier [P].** RM1076E 05-1054 (DTC B1412):
   "The A/C ambient temperature sensor is installed in the front part of the condenser … The sensor
   connected to the ECM … The ECM sends the read signal to the A/C amplifier via CAN and body multiplex
   communications." The wiring diagram shows the sensor on ECM terminal TAM (E5-21). The A/C system
   diagram (05-1019) shows the path: ECM → CAN → gateway → BEAN → A/C amplifier. From there the A/C amplifier
   sends "ambient temperature" to the combination meter over BEAN (low-temperature indicator, below 3 °C,
   05-1679/1680) and to the multi-display over AVC-LAN via the gateway.
   (`docs/prius_buses.md` says the A/C amplifier reads the sensor. Per the RM that is wrong for the NHW20.)
2. **The ECM and the A/C amplifier are not on the bus with the car off [C]/[I].** In all 19 car-OFF wakes
   on our car and the 3 power-OFF JP dumps, only six nodes take part (0x40, 0x52, 0x62, 0xB5, 0xC1,
   0xE9). Nodes 0x98 (A/C amplifier, §3) and 0x19 never appear. No ECM-derived BEAN message (coolant
   `FE:2C`, rpm `FE:26`, `FE:CD`) appears either [C]. The ECM is a CAN-only node that runs with the power
   switch ON [I]. The A/C amplifier has +B backup, but its IG input switches "LOCK or ACC → ON" (05-1037)
   [P], and it never answered a wake [C].
3. **Byte-by-byte, nothing in the car-OFF traffic is a temperature [C].** Every byte is constant, a
   flag, an enum, or a per-event random value (key crypto). Nothing varies like an analogue value. Bytes
   that happen to fall in a plausible range are constant and identical on the Japanese car in August:
   `FE:53` b0 = 0x49, and `FE:AB[52]` 0x20/0x8E/0xC0. So they are not temperatures (§2.5).
4. **With IG-ON, ambient temperature is `FE:CD` (1 byte) [X]/[I].** In the JP dumps it is `47`, then `46`,
   and stays 46 for 413 s. On the RX300 (specadmin) and the IS300 the same ID is "°C + 48". That gives 23
   and 22 °C, which is plausible for the JP capture (repository dated August). **Our car has not
   confirmed the scaling yet.** For a 5–10 °C evening, expect 0x35–0x3A if the RX300 scaling holds.
   Other scalings would give 31 °C (A−40) or 1.5 °C (the tester's 0.35·x−23.3 range from 05-1044). The
   August date makes the "+48" reading the most plausible, but it is still unconfirmed.
   * **Sender: node 0x98 = A/C amplifier [I].** `FE:CD` arrives in the burst of the A/C amplifier's
     first transmissions (`FE:FC`, `52:FD`, `52:ED`, `52:6D`, `FE:CD`), 20–170 ms after `FE:AB[98]`
     first appears (IG-ON dump +5.80 → +5.97 s; READY dump +2.07 → +2.24 s). Gateway-relayed CAN data
     (PRI 6) started about 0.2 s earlier. This matches 05-1678/1019: "A/C ECU → meter: ambient temperature".
   * **First appearance:** at the transition to IG-ON. In the IG-ON dump the first power-switch press
     gave ACC and nodes 0x19/0x98 appeared only after the second press (+5.5 s). In the READY dump (one
     press with brake) it appears at +2.2 s. It never appears in ACC or OFF [C for the JP car].
5. **No BEAN request exists that could produce a reading with the car off.** No poll/response pattern
   for temperature was seen. The only request/response pair on the bus is the immobiliser crypto
   exchange (§1.4). Waking the bus wakes only the six +B nodes; it cannot power the ECM. **Speculation:**
   a Techstream-style diagnostic request through the gateway would also need the power switch ON, because
   05-1044/1055 say "Turn the power switch ON" for the A/C DATA LIST "Ambi Temp Sens".
6. **Other places to get it (all need IG-ON):** AVC-LAN A/C amplifier → multi-display (already used by
   the project, `docs/AVC_BACKEND.md`); OBD-II PID 0x0146 from the ECM (A−40, already polled); CAN 0x5CC
   (decoded in the project but unverified). **For car-OFF use**, the only options are caching the last
   IG-ON value at power-off, or the project's own sensor (powerbox BMP280 "outside-box").

---

## 1. Network structure found in the data

### 1.1 Node addresses → ECUs

Eight nodes: six with +B that answer every wake, plus two IG-only nodes. This matches the eight BEAN
ECUs that the RM lists for this car [P]: body, meter, A/C, transponder key, power source control,
transmission control, smart, gateway.

| Node | ECU (best guess) | Evidence | Conf. |
|---|---|---|---|
| 0x40 | **Body ECU** (instrument panel J/B, multiplex network body ECU) | Woke the bus first (un-ACKed `FE:C2 40 80`) at all 19 of our door/lock/remote wakes and at the JP key-ejected wake. Holds its keep-awake bit for 5.5 s after every remote/smart lock/unlock (hazard answer-back), and for 1 s after every door or lamp change. Door courtesy and lock switches and the door-control-receiver data line (RDA, 05-1883) go to the body ECU [P] | [I] strong |
| 0x52 | **Gateway ECU** | Receives the odometer (`52:3F`): RM "meter → gateway: ODO meter information" [P]. At IG it receives the display-bound frames that go to the display node on the RX300 (`52:FD` set temperature, `52:90`/`52:A2` fuel economy, `52:76`). On the Prius the display sits behind the gateway on AVC-LAN. Its keep-awake bit always drops last (network master); its status frame is the 11-byte one; its `FE:C2` uses PRI 12 | [I] strong |
| 0x62 | **Combination meter** | Destination of `62:6E` (OFF) and `62:21`/`62:C8` (IG). DST 0x62 = meter on the IS300 and the BeanMPX car [X]. First node to release the bus. It sometimes fails to ACK `62:6E` (3 tries, then the sender gives up) while it dozes and the bus stays awake (32:25.8, 35:59.6, 24:38.4) | [I] medium |
| 0xB5 | **Smart ECU** | Answers the 0.5 s challenge (`B5:D9` → 12–14 ms → `C1:E2`) in the 30 s window after the driver door opens. RM 05-1919: after the driver door opens/closes the smart ECU checks the key and "the transponder key ECU and the smart ECU communicate encryption codes … using the multiplex communication line" [P]. Its status byte is 0x9F/0x93 throughout the window | [I] medium |
| 0xC1 | **Transponder key ECU** | The other side of that exchange: it sends the challenge `B5:D9`, constant within a window, random between windows. Woke the bus at key insertion (JP key-inserted dump) | [I] medium. B5/C1 could be swapped |
| 0xE9 | **Power source control ECU** | Woke the bus (un-ACKed `FE:C2 E9 A1`) in all 3 JP dumps that start with a power-switch press. Its status byte follows power mode: A0/A1 at the press, 0x81/0x83 in IG/READY | [I] strong |
| 0x98 | **A/C amplifier** (IG-only) | Receives `98:EC`, whose bytes 0–1 follow, with lag, the compressor target speed it sends to the gateway in `52:ED` b3–4 (§5). Its first transmissions are the climate set (`FE:FC`, `52:FD`, `FE:CD`) | [I] strong |
| 0x19 | **Transmission control (P position) ECU** (IG-only) | By elimination. `FE:50` (P lock state) appears in the same IG start-up block | [I] weak |

### 1.2 Network management: `FE:C2` and `FE:AB`

* **`FE:C2 <node> <code>` = wake / "network start" announcement [C]/[I].** A sleeping bus is woken by
  one node. Its first `FE:C2` is never ACKed (17/17 in session30, 2/2 in the remote test), because
  the other nodes are only waking up from that frame's edges. That is the "why" of the un-ACKed
  first frame. The others then answer in a fixed order 7 ms apart (one frame time): 52 (PRI 12), E9, C1,
  B5, 62, 40. The waker repeats its `FE:C2` at +42 ms, now ACKed.
  * Wake-burst timing on our car (19/19 identical within ±2 ms): `C2` waker +0, others +7…+36 ms,
    waker again +42, `52:3F` odometer +54, `40:59` +66, the event frames (`FE:E1`, `FE:B6`, `FE:83`,
    `FE:7F`) at +130 ms, the first periodic cycle at +0.5 s.
  * A node can also send `FE:C2` while the bus is awake: `E9 87` at 30:30.9 and 32:01.4, 0.1–0.2 s before
    a handle-touch unlock and a driver-door opening. Within 0.5 s every node sets its keep-awake bit
    again (all `FE:AB` → 0x80). So `FE:C2` acts as a "restart the network timers" request [I]. The second
    byte is a node-specific code (90 normal wake for E9/B5, 80 for the others, A1 for E9 at a power-switch
    press, 87 for E9 at a handle event) [I].
* **`FE:AB <node> <status> […]` = per-node status / keep-awake, every 1.00 s [C].** Bit 7 of the status
  byte = "I need the bus". Every node sets it at wake and clears it about 1.4–1.6 s later if it has
  nothing to do. The lower bits are node-specific state [I]:
  * 0x40 body: 0x80/0x00 only.
  * 0xB5 smart: 0x90 at wake; 0x9F driver door open during the key window; 0x93 door closed, window
    still open. JP: 0x8F, 0x91, 0xB3, 0x82 (READY).
  * 0xE9 power source: 0x90 at wake. JP: 0xA0 at power-switch press, 0x8F with power OFF and key
    present, 0x81 ACC→IG, 0x83 IG/READY.
  * 0xC1, 0x62: 0x80/0x00 only. JP IG-only nodes 0x19 and 0x98: 0x80.
  * **0x52 gateway (11 bytes):** ours `52 s 20 20 00 00 C0 04 8E 03 80`, JP `52 s 20 20 00 00 C0 04 8E 07 80`.
    s = 0x80/0x00 keep-awake [C]. In the JP IG dump: b2 0x20 → 0xA0 at +3.5 s (after the first
    gateway-relayed CAN data) → 0xB0 together with b7 0x04 → 0x06 at +6.5 s (after nodes 0x19/0x98
    appeared). At power-off b2 0xB0 → 0x30, and 12 s later back to 0x20 with b7 0x04. Reading: b2 bit 7 =
    CAN side / IG active; b2 bit 4 and b7 bit 1 = IG-domain nodes present [I]. b9 0x03 (ours) vs 0x07 (JP)
    = a configuration difference [?].
* **PRI bit 3 = "immediate" copy [C].** Every message has a base priority (periodic copy) and the same
  priority + 8 when sent at once: the first copy after wake, or on a value change. Examples: `FE:B6` 2/10
  (31 of 31 PRI-10 copies carry a change), `FE:7F` 2/10, `FE:83` 2/10, `FE:D8` 2/10, `FE:E1` 2/10,
  `52:3F` 1/9, `40:59` 2/10, `FE:C2` 8 (0|8) and 12 (4|8), JP `FE:40`/`FE:91`/`FE:29` 6/14. This resolves
  gap 7 of `BEAN_PROTOCOL.md` §8.1.
* **Base priorities by sender class [I]:** 0 = NM status (4 for the gateway), 1 = configuration/
  odometer/display, 2 = body/meter state, 6 = gateway-relayed CAN data.

### 1.3 Who sends what (inferred from timing)

* `FE:E1` and `FE:B6` always come back-to-back, 8–9 ms apart (one frame, no gap), at every lock/unlock
  event. A smart unlock appears as E1, B6 `01 00`, E1, B6 `C1 00` within 32 ms. A lock motor cannot move
  in 8 ms, so B6 byte 0 is the ECU's lock state, not a physical switch read, and **E1 and B6 most likely
  come from the same node, the body ECU** [I]. This weakens the earlier reading of E1 as a command sent
  *to* the body ECU (`BEAN_FINDINGS.md`). It is more likely the body ECU's own "lock/unlock by
  wireless/smart" event broadcast.
* `B5:D9` is sent by 0xC1 and `C1:E2` by 0xB5 (request → response 12–14 ms later, 0.50 s cycle) [I].
* The 1 s periodic cycle is phase-locked across nodes: `FE:AB[62]`, `FE:AB[52]`, `FE:DB`, `FE:54`, `FE:E1`,
  `FE:53`, `62:6E`, `FE:AB[40]`, `FE:AB[B5]`, `FE:7F`, `FE:62`, `FE:B6`, `FE:AB[E9]`, `FE:AB[C1]` all
  arrive within 120 ms. `FE:83`, `FE:D8` and `40:59` have their own phases (+0.3, +0.5, +0.6 s).

### 1.4 The immobiliser exchange (do not touch)

`B5:D9` = 2 random bytes + `00`, constant for one driver-door window (values not published).
`C1:E2` = `03 03 80` at the start; then either a 2-byte value + `00`, a different one per window
(values not published), or `00 00 80`.
* `00 00 80` appears while the driver door is open, and after the door closed with the owner outside.
  The 2-byte value appears while a key is presumably in the cabin.
* Reading [I]: `03 03 80` = searching, `00 00 80` = no key in cabin, xx yy `00` = encrypted response to
  the challenge.

This is the RM's encrypted code exchange [P]. Replay is pointless and interference risks B2289.

---

## 2. Complete signal map of our captures (car OFF)

Periods: 1.00 s for every periodic message (p10–p90 0.98–1.02 s). `52:3F` every 10.0 s. `B5:D9`/`C1:E2`
every 0.50 s, only inside the key window. Frame rate about 19 frames/s while awake, about 21/s inside
the key window.

| Message | PRI | Bytes / bits | Behaviour | Meaning | Conf. |
|---|---|---|---|---|---|
| `FE:C2` | 8 (52: 12) | b0 node, b1 code | wake burst; see §1.2 | network wake announcement | [C]/[I] |
| `FE:AB` | 0 (52: 4) | b0 node, b1 bit 7 keep-awake, low bits state; 52: 11 bytes | 1 s | NM status | [C]/[I] |
| `52:3F` | 1/9 | b0–b2 = 0x0493DF = 299,999; b3 0x80 | const, every 10 s and at wake | odometer km (meter → gateway). JP sends it to FE | [C] (owner) |
| `40:59` | 2/10 | `00 C0` | const (identical on the JP car in every state) | unknown, addressed to the body ECU | [?] |
| `62:6E` | 2 | `00 00` | const (JP: `80 80` → `80 00` → `00 00` in 5 s at a power-switch press with a door open) | possibly a meter buzzer request [I weak] | [?] |
| `FE:53` | 1 | `49 00 00 00 00 00` | const (JP `49 00 08 20 00 00`) | vehicle specification / option bitmap (RM: body ECU → A/C, smart, theft, power source: "vehicle spec(s)") | [I] |
| `FE:54` | 1 | `57 42 4C 41 45 4E` ("WBLAEN") | const (JP `00 42 00 41 45 4E`) | same: spec/destination bitmap. The ASCII look is probably a coincidence: the differences vs JP sit in b0 (0x57 vs 0x00) and b2 (0x4C vs 0x00). Bits set only on our EU car could mean EU destination, LHD, rear fog, smart option … | [I] |
| `FE:62` | 2/10 | `20 00` | const with the car OFF | power-source ECU state. JP decode: b0 0x20 = shift in P (cleared when `FE:40` leaves P and set again on return, 3/3), 0x40 = key present (key in slot / key verified), 0x10 = power not OFF, 0x08 = power switch pressed (pulses at each press, 4/4), 0x80 = transient during a power-switch wake. `20 00` = OFF, P, no key | [I] (JP-derived) |
| `FE:7F` | 2/10 | b2 bit 7 | 1 while any door is open | door-open indicator (meter) | [C] |
| | | b1 bit 4 (0x10) | always 1, except the first frame of a door-open wake (`00 00 80…`), 2/2 | unknown | [?] |
| | | b0 | 0 (car OFF) | JP READY: 0x10 tail/clearance, 0x20 headlamp, 0x01 a third lamp; bean_tools: 0x08 high beam | [S]/[I] |
| `FE:83` | 2/10 | b3 bit 3 (0x08) | set at unlock/door open; cleared 15.2 s after the driver door closes (2/2) or at lock (1/1) | interior (dome) light ON (illuminated entry). The bus sleeps while it is still set | [C] |
| | | b3 bit 2 (0x04) | 36:01.0–36:02.1 only | luggage lamp / back door (single occurrence) | [C weak] |
| | | b0 | 0 (car OFF) | JP: 0x02 key inserted, 0x20 then 0x40 added on ACC→IG (02 → 22 → 62; at power-off 62 → 42 → 02) = ACC/IG flags | [I] (JP-derived) |
| `FE:B6` | 2/10 | b0 bit 0 | driver door unlocked; set first at every unlock | driver door lock state | [C] |
| | | b0 bits 7, 6 | other doors unlocked. Unlock: `01` → `C1` in 17–25 ms. Lock: `C0` → `00` in 7 ms or `C0` → `40` → `00` in 80 ms. At 33:00.2 the value `80` appeared alone (only bit 7) | two more lock-state groups (which doors: open question) | [C]/[?] |
| | | b1 bit 7 / 6 / 2 | driver / front passenger / rear or back door open (one bit for three doors) | door courtesy switches. The JP car (RHD) shows bit 6 with the driver-door timer, so bit 7 = left front and bit 6 = right front may be physical sides | [C] / [I] |
| `FE:D8` | 2/10 | b1 bit 0 | set when the driver door opens; cleared **29.9–30.0 s after the driver door closes** (n = 4). The 0.5 s crypto exchange runs exactly inside it | smart-key check window after driver door (RM 05-1919) | [C] timing, [I] meaning |
| | | b0 | 0 | JP: 0x04 with key inserted, 0x0C in IG (drops to 0x04 20 s after power-off) | [?] |
| `FE:DB` | 2 | `00` | const (RX300: driver-door state; not on the Prius) | unknown | [?] |
| `FE:E1` | 2/10 | b2 | `04` remote unlock, `08` remote lock (remote test); `10` unlock, `20` lock (session30: 30:31, 31:01, 31:21, 36:57). One frame (two for a two-step unlock) at PRI 10, then `00` in the periodic copy | lock/unlock event, coded by source: **04/08 = wireless remote, 10/20 = smart (handle touch / handle button) [I]**. Not sent for the interior switch (32:51.6, 33:11.3, 33:30.4) [C] | [C] event; [I] source split |
| `B5:D9` | 10 | b0–b1 random per window, b2 `00` | 0.5 s inside the D8 window | immobiliser challenge (transponder → smart) | [P] exchange, [I] direction |
| `C1:E2` | 10 | see §1.4 | 0.5 s inside the D8 window | smart ECU response | [P]/[I] |

### 2.1 Correction to `BEAN_FINDINGS.md`

* `FE:E1` **does** distinguish sources. The capture named "remote test" shows `04` (unlock) and `08`
  (lock); session30 shows only `10`/`20`. Either session30 had no remote presses, or the owner's
  attribution needs rechecking against the action list. I did not have the full timed action list, only
  the door intervals in `BEAN_FINDINGS.md`. Capture-plan item 3 settles this.
* Two driver-door openings are missing from the `BEAN_FINDINGS.md` table: 33:00.2–33:01.7 (with B6 b0 =
  `80`) and 35:27.3–35:31.0.

### 2.2 Reconstructed session30 timeline (from the bus; owner attributions where the findings give them)

| Wake | Duration | What the bus shows |
|---|---|---|
| 29:49.6, 30:04.5 | 4.6 s | nothing changes (locked); dome bit already set at the 29:49 wake |
| 30:26.2 | 13.3 s | wake; 4.9 s later `C2 E9 87`, `E1 10` + B6 `01` → `C1` = unlock |
| 30:45.9 | 4.6 s | nothing (unlocked) |
| 31:00.9 | 8.5 s | `E1 20` + B6 `C0` → `00` = lock |
| 31:20.8 | 85.0 s | `E1 10` unlock; driver door 31:21.5–31:26.4 and (after `C2 E9 87`) 32:01.5–32:11.3; key window until 32:41.2 |
| 32:51.5 | 4.6 s | B6 `C0` → `00`, no E1 = lock by interior switch |
| 33:00.1 | 36.5 s | B6 `80 00`; driver door 33:00.2–33:01.7; B6 → `00 00` at 33:11.3; interior-switch unlock (`01` → `C1`, no E1) 33:30.4 |
| 33:45.4 | 4.6 s | nothing |
| 34:00.9 / 34:10.9 | 4.6 s each | passenger door open / closed. The bus slept in between while the door was open |
| 34:26.1 | 4.6 s | nothing |
| 35:27.1 | 39.5 s | driver door 35:27.3–35:31.0; rear/back door 35:40.2–35:52.0; back door (+ luggage lamp bit) from 36:01.0 |
| 36:16.1 / 36:31.1 / 36:41.3 | 4.6 s each | back door closed / a rear door open / closed (the bus sleeps with a door open) |
| 36:56.5 | 9.5 s | `E1 20`, B6 `C0` → `40` → `00` = lock |

The seven "nothing changes" wakes are all started by the body ECU. They are probably the smart key's
radio replies reaching the door control receiver, whose data line goes to the body ECU (05-1883), while
the owner stood near the car [I]. Before 29:49 the bus was silent for 11.3 min with the car locked: no
periodic wake [C].

### 2.3 Sleep/wake numbers [C]

| Quantity | Value |
|---|---|
| Wake with no state change | 4.58 s awake (13 wakes, range 4.55–4.62 s) |
| Each node's keep-awake bit when idle | cleared 1.42–1.62 s after wake (62 first, then E9, C1, B5, 40) |
| Body ECU hold after an `FE:E1` lock/unlock | 5.4–5.6 s (n = 5); after any other B6/83 change: a 1.0 s pulse |
| Smart/transponder hold | to the end of the 30 s driver-door window, +1.0–2.0 s |
| Gateway 0x52 | drops 0.87–0.93 s after the last other node |
| Last frame after the gateway drops | **2.07 s** median (1.97–2.23, n = 24 incl. JP) |
| Last payload change → sleep | 4.3–8.0 s (median 4.6 s) |
| Longest awake | 85 s (two driver-door windows back to back) |

**Shutdown sequence:** there is no dedicated sleep frame. The `FE:AB` status bytes drop bit 7 node by
node, the gateway's drops last, then about two more periodic cycles (last frames `FE:54`, `FE:E1`,
`FE:53`, `FE:AB[40] 40 00`), then silence. The dome-light request and door-open bits can still be set
when the bus sleeps. **Who keeps it awake:** the body ECU (lock answer-back, every door/lamp change)
and the smart + transponder pair (key window).

### 2.4 Jitter

All 1 s messages: p10–p90 within ±10 ms, except `FE:D8` (1.010 s, 0.998–1.015) and the `FE:AB`
frames of 0x40/0xB5 (0.98–1.01). The wake burst is reproducible to ±2 ms.

### 2.5 Temperature candidates in the OFF traffic (all rejected)

| Candidate | Value | Why not |
|---|---|---|
| `FE:53` b0 | 0x49 (= 25 °C at −48, 33 °C at −40) | constant across 19 wakes, identical on the JP car |
| `FE:54` bytes | 0x41–0x57 | constant, partly identical to JP; spec bitmap |
| `FE:AB[52]` b2/b3/b6/b8 | 0x20/0x20/0xC0/0x8E | constant, identical to JP |
| `40:59` b1 | 0xC0 | constant, identical to JP |
| `B5:D9`/`C1:E2` | random per event | crypto (§1.4) |
| `52:3F` | odometer | confirmed |

---

## 3. IG-ON / READY signals (JP dumps only so far)

The JP adapter drops about 2/3 of frames: `FE:53` measures 3.3 s there vs 1.00 s on our car. Periods
below are JP-measured and therefore upper bounds. "First" = seconds after the IG-ON transition.

| Message | Sender [I] | Content | Scaling / decode | Conf. |
|---|---|---|---|---|
| `FE:CD` | A/C amp (0x98) | **ambient temperature** | °C = x − 48 (RX300/IS300) → 23/22 °C; unconfirmed on the Prius | [X]/[I] |
| `FE:2C` | gateway (ECM) | coolant | °C = x/2 (0x63 = 49.5 °C at IG, 0x5E–0x60 in READY, cooling: engine did not run) | [X], consistent |
| `FE:24` | gateway | vehicle speed | km/h, 1 byte (only 0 seen) | [X] |
| `FE:26` | gateway (ECM) | engine speed | 2 bytes, only `00 00` seen; RX300 (b0·256+b1)/5.12 | [X], unverified |
| `FE:40` | gateway (HV ECU) | shift position | b0: 0x80 P, 0x40 R, 0x20 N, 0x10 D; B not captured; `00` while moving between positions | [X]/[C-JP] |
| `FE:50` | P ECU (0x19)? | P-lock state | 0x80 = P engaged (follows `FE:40` P, 3/3); 0x08 = out of P / actuator moving; 0x04 = IG; 0x02 at power-off | [I] |
| `FE:62` | power source | power mode | see §2 (P, key, ON, switch press) | [I] |
| `FE:83` | body | ACC/IG/key flags + dome light | b0 0x02 key, 0x20/0x40 ACC/IG | [I] |
| `FE:91` | gateway (HV ECU) | READY state | `A0 3F 06` IG-not-READY → `80 20 00` at READY. Bit 7 drops briefly (`00 00 00`/`00 20 00`) around shifts out of P | [I] |
| `FE:29` | gateway (HV ECU) | HV status | b2: 0x04 IG, 0x08/0x20/0x40/0x80 sequence at READY start, then 0x84 ↔ 0x8C toggles every few s | [?] |
| `FE:7F` | body | lamps | b0 0x10 tail, 0x20 head, 0x01 a third lamp; b2 0x80 door | [S]/[I] |
| `FE:FC` | A/C amp | climate state | RX300 layout fits: b0 0x80 AUTO, 0x08 rear defogger (on 70.6–79.3 s in READY), 0x02 A/C, 0x01 face/foot. `41` fan, `49` + rear defog, `C3 00 02 00 00 20` AUTO A/C | [X]/[I] |
| `52:FD` | A/C amp → MFD | set temperature | RX300: 0x38 initialising, 0x39 climate off, else 0.5 °C·x + 16 → `12` = 25.0 °C; b1 0x38 = second zone unused | [X]/[I] |
| `52:6D` | A/C amp → MFD | climate display | b0 low nibble 1–4 rises and falls with the blower (fan step?); b1/b2 `80 7F` ↔ `20 DF` with A/C on | [I] |
| `52:ED` | A/C amp → gateway (→ HV ECU) | compressor request | b3–b4 = target compressor rpm (0x0C77 = 3191 … 0x0D81 = 3457 … 0x0A04 = 2564 → 0); b0 0x08 READY, 0x20 A/C on, 0x40 compressor active | [I] |
| `98:EC` | gateway → A/C amp | compressor feedback | b0–b1 = actual compressor rpm, ramps toward the `52:ED` target with lag (0x0686 → 0x0D83); b2/b3 vary with load (power/current?); b4–b5 slow (0x243F → 0x253D), present with the compressor off (inverter temperature or voltage?) | [I] |
| `98:CC` | gateway → A/C amp | `01 94`/`01 95`/`01 96` flicker | unknown; candidates SOC or a raw sensor value | [?] |
| `98:27`, `98:B2`, `98:F8` | gateway → A/C amp | `01 FF` at READY, `09 FF` after A/C off; `00 04 00` at READY | HV-ECU → A/C permissions ("use permissible power", "standby") | [?] |
| `52:90` | meter/gateway | momentary fuel economy | RX300 0x90: b0–b1 × 0.1, b2 0x80 = km/L → 0.0 | [X] |
| `52:A2` | | average fuel economy | 0x0093 = 14.7 km/L (b2 0x80 = km/L), cf. RX300 0xA3 | [X]/[I] |
| `52:76` | | distance | 0x1013 = 4115 with km flag; sent once at IG. Trip since reset? | [?] |
| `52:A4` | meter → gateway | fuel level? | 0x14 / 0x11 (0x00 at start). 0xA4 = fuel on IS300/BeanMPX | [X]/[I] |
| `52:D0`, `52:99`, `52:CA`, `62:21`, `62:C8`, `FE:D4` | | always 0 / `00 61 00` | RX300 D4 = starter/charge/oil warnings; D0 = meter indicators | [?] |

Not seen in any dump: seat belt, washer/wiper, turn/hazard, key-in-slot except through `FE:83`/`FE:62`,
rear fog, dimmer.

### 3.1 Differences between the JP car and ours (OFF traffic)

| Item | JP | Ours |
|---|---|---|
| Odometer | `FE:3F 02 87 E9 80` (broadcast) | `52:3F 04 93 DF 80` (to gateway). bean_tools' own dashboard file uses DST 0x52, so both may exist [?] |
| `FE:53` | `49 00 08 20 00 00` | `49 00 00 00 00 00` |
| `FE:54` | `00 42 00 41 45 4E` | `57 42 4C 41 45 4E` |
| `FE:AB[52]` b9 | 0x07 | 0x03 |
| `FE:E1` | never non-zero | `04/08/10/20` events |
| `C1:E2` | absent (single `B5:D9` frame at a power press) | 0.5 s exchange in every driver-door window |
| Wake burst | gateway `FE:C2` absent in 4/5 dumps (probably adapter drops) | always present |
| `FE:AB[40]` | logged once per dump (drops) | 1 s |

---

## 4. Transmission candidates (ranked; nothing here has been sent)

| Rank | Candidate | Evidence | Expected effect | Risk |
|---|---|---|---|---|
| 1 | **No BEAN TX: body ECU active tests over CAN** (diag 0x750, sub-address 0x40 "main body": Door Lock, Hazard, Trunk/Bdor) | RM active tests [P]; CAN path reported [S] | lock/unlock/hazard without touching BEAN | low (documented path) |
| 2 | **`FE:7F` b2 bit 7 (door indicator)** at PRI 2, once | bean_tools toggles it [S] | meter door lamp flicker until the owner's next 1 s copy (IG-ON only). Proves the TX hardware | low |
| 3 | **`FE:E1 00 00 08` / `00 00 04`** at PRI 10, once, with the bus already awake | event appears at every remote/smart lock/unlock [C]; timing says the body ECU sends it [I] | if the body ECU is the sender (likely), nothing or a confused smart/theft state; if it is a command to the body ECU, the doors lock/unlock | medium: two-sender conflict on one ID; may set nothing, may confuse answer-back logic |
| 4 | **Wake the bus** (any valid frame) as a "read the state" trigger | 19/19 wakes produce `52:3F` odometer, `FE:B6` lock/door, `FE:83`, `FE:62` within 0.6 s, then sleep after 4.6 s [C] | on-demand read of lock/door state and odometer with the car OFF. **No temperature** (the A/C amp/ECM do not wake) | low per wake; battery drain if repeated |
| — | `B5:D9`/`C1:E2`, `FE:62`, `FE:40`, `FE:91`, `FE:C2`/`FE:AB` look-alikes | immobiliser, power state, NM | — | **do not** (B2289/B228x, B12xx) |

The only request → response pair seen is the immobiliser exchange. No diagnostic or polling traffic
appears on BEAN in any capture.

---

## 5. Next-capture plan (priority order)

Log with marks (`bean_mark`) at every action. Repeat each action 3× with about 5 s quiet between.

1. **IG-ON with a reference temperature.** Note a thermometer reading and the MFD outside temperature.
   Log AVC-LAN at the same time. Power switch twice without brake (ACC, then IG). Hold 60 s. Then IG → OFF
   and wait for sleep. Goal: `FE:CD` scaling, nodes 0x19/0x98, `FE:62`/`FE:83` power-mode bits, IG-only
   messages on the EU car.
2. **READY** (brake + power) for 60 s, then P → R → N → D → B → P with the brake held. Then OFF. Goal: `FE:91`,
   `FE:40` B bit, `FE:50`.
3. **Remote vs smart separation** (car OFF): remote lock/unlock, handle-touch unlock at each front
   door, handle-button lock, interior switch, mechanical key, each marked. Goal: `FE:E1` 04/08 vs 10/20,
   and the B6 b0 bits 0x40/0x80.
4. **A/C** (IG-ON or READY): AUTO on/off, A/C on/off, recirc, front and rear defog, set temperature
   up/down by 0.5 °C, each fan step. Goal: `FE:FC`, `52:FD`, `52:6D`, `52:ED`/`98:EC`, meter indicators.
5. **Temperature change:** repeat item 1 on a cold morning and after a warm drive. Optionally a hair
   dryer *near* (not on) the sensor in front of the condenser for 30 s at IG-ON. Watch `FE:CD`
   against AVC-LAN and OBD 0146.
6. **Lights** (IG-ON): tail, head, high beam, flash, front fog, rear fog, dimmer knob, hazards, turn
   signals. Goal: `FE:7F` and any new IDs.
7. **Key/belt/wipers** (IG-ON): seat belt buckle/unbuckle, washer, wiper positions, key in slot (if
   fitted), power switch with the key outside the car (warning buzzer request).
8. **Driving** (passenger logging): 0 → 50 km/h. Goal: `FE:24`, `FE:26` scaling.
9. **Long locked idle:** 30 min with nothing done and the key away from the car. Goal: confirm no
   periodic wake (11.3 min silent so far).

---

## 6. What remains unknown

* The `FE:CD` scaling and sender on the Prius (needs plan item 1). Whether the A/C amplifier keeps a
  last value in +B memory: irrelevant on BEAN, since it never transmits with the car OFF.
* Which ECU sends `FE:E1` (body vs smart), and whether it is an event report or a command.
* The B5 ↔ C1 role assignment; the IG-only node 0x19 identity; 0x62 = meter is likely but unproven.
* The meaning of `40:59`, `62:6E`, `FE:DB`, `FE:D8` b0, `FE:7F` b1, the `FE:53`/`FE:54` bit fields,
  `FE:AB[52]` b2–b10, `FE:29`, `98:CC`, `98:27`, `52:76`, `52:CA`.
* B6 byte-0 bits 0x40/0x80: which doors.
* JP periods (adapter drops frames).

---

## 7. Sources

* Toyota RM1076E (Prius 2004 EU), section 05: A/C system diagram 05-1019/1020; location 05-1016; ECU
  terminals 05-1037; DATA LIST 05-1044; customize "Ambient Temperature Shift" 05-1034; **DTC B1412
  ambient temperature sensor circuit 05-1054/1055**; combination meter 05-1678–1680; smart entry ECU
  terminals 05-1883; smart ignition / encrypted exchange 05-1919.
  https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/rm/rm1076e/m_05_1054.pdf (and the other page
  numbers in the same pattern)
* haageduard/bean_tools (JP NHW20 dumps, `data/prius_20.json` dashboard): https://github.com/haageduard/bean_tools
* specadmin/toyota-mpx `MPX-codes.md` (RX300: 0xCD "°C + 48", 0xFC, 0xFD, 0x90, 0xA3, 0x76, 0xD0, 0xD4):
  https://github.com/specadmin/toyota-mpx/blob/master/MPX-codes.md
* daniel-dmitrzak/BeanMPX_IS300 (`case 0xCD: … beanBuffer[4] - 48`): https://github.com/daniel-dmitrzak/BeanMPX_IS300
* fiztech-code/BeanMPX: https://github.com/fiztech-code/BeanMPX
* PriusChat outside-temp / CAN PID threads (CAN side, A−40): https://priuschat.com/threads/scangauge-xgauge-code-for-outside-temp.142896/ ,
  https://www.priuschat.com/goto/post?id=1121217
* Web searches for Prius-specific BEAN IDs (0xCD, lock/unlock) found no further sources.

---

## 8. Reproduce

```
cd tools/bean_analysis
python3 bean_stats.py   ../../docs/data/bean/*.ndjson.gz                  # per-message/per-byte stats
python3 bean_timeline.py ../../docs/data/bean/2026-11-13_session30_doors_locks.ndjson.gz --skip-ab
python3 bean_sleep.py   ../../docs/data/bean/*.ndjson.gz ../../tests/fixtures/bean_nhw20_frames.txt.gz
python3 bean_correlate.py ../../docs/data/bean/2026-11-13_session30_doors_locks.ndjson.gz --builtin session30
python3 bean_correlate.py CAPTURE --marks marks.csv --toggles             # marks: "mm:ss[-mm:ss],label"
python3 bean_compare.py ../../docs/data/bean/*.ndjson.gz --fixture ../../tests/fixtures/bean_nhw20_frames.txt.gz
```

`beanlib.py` holds the shared loaders. Frame time comes from the firmware `t_ms` plus one offset,
because the host `ts` is batched.
