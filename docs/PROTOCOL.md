# Gateway host protocol (NDJSON)

The protocol between the backend and the RP2040 gateway, as the backend uses it with gateway
firmware **3.x** (C, `cyberpunk-prius-gateway/gateway-rp2350/`, `FW_VERSION` 3.2.9). Authoritative
descriptions: `cyberpunk-prius-gateway/PROTOCOL.md` (envelope, v2 base) and
`gateway-rp2350/README.md` ("Protocol: what changed"). The firmware facts below were read from
`gateway-rp2350/src/` (`can_sol.c`, `mcp2515.c`, `commands.c`).

## 1. Envelope

USB CDC, 1 000 000 baud, one JSON object per line.

```json
{"id": <int>, "ts": <ms since gateway boot, RX only>, "seq": <0-65535, optional>, "d": <payload>}
```

| Id | Channel |
|---|---|
| 0 | gateway system (status, configuration, errors) |
| 1 | CAN (solicited) |
| 2 | AVC-LAN |
| 6-255 | RS485 satellites (transparent tunnel; this project uses 100-199) |

The powerbox speaks the same envelope on its own CDC port and also uses ids 1 and 2 for other
buses; the backend offsets its ids into 200-203 ([VIRTUAL_TWIN_ARCHITECTURE.md](VIRTUAL_TWIN_ARCHITECTURE.md) §4).
Identification on either device: `{"id":0,"d":{"a":"whoami"}}` → `{"id":0,"d":{"msg":"IDENT","role":"gateway"|"powerbox","ver":…}}`.

## 2. System channel (id 0)

| Line | Meaning |
|---|---|
| `{"msg":"GATEWAY_READY","ver","role","fw","board","avc_pol",…}` | boot banner |
| `{"msg":"GW_HB","role","ver","n","up",…,"avc","avc_err","avc_lvl","drop"}` | heartbeat, 1 s; `up` = uptime (a regression = reboot), `drop` = lines the host did not read |
| `{"err":"<CODE>"}` | error (table in §5) |
| `{"msg":"SUB_OK","slot"}`, `{"msg":"UNSUB_OK","slot"}`, `{"msg":"UNSUB_ALL"}`, `{"msg":"CAN_MODE","m":"NORMAL"|"LISTEN"}` | confirmations |
| `{"msg":"CAN_NOT_ALIVE",…}`, `{"msg":"CAN_REINIT","why","ok","n"}`, `{"msg":"CAN_MODE_FIX","from","to","ok","n"}` | CAN controller supervision (§3.4) |
| `{"can_diag":{"mode","tec","rec","eflg","req","resp","to","txerr","isotp_err","subs","mode_fix","reinit","alive","tx_blocked",…}}` | answer to `can_diag` |

Host → gateway system actions: `{"seq":true}` (enable the sequence counter), `whoami`, `stats`,
`avc_raw`, `avc_err`, `avc_cfg` (`pol`, `t1`, `t0`, `tstart`, `glitch`, `ack`), `can_cfg`
(`stmin`, `gap`, `retries`), `can_diag`, `log_rx`, `reset`, `bootsel`, all as `{"id":0,"d":{"a":…}}`.
Lines the host is not reading are dropped (counted in `drop`), not queued.

## 3. CAN (id 1)

### 3.1 Model

- **Solicited only.** No passive frames are forwarded (broadcast CAN comes from the panda,
  [ARCHITECTURE.md](ARCHITECTURE.md) §3). Hardware acceptance filters admit only the response ids
  of live queries.
- The MCP2515 sits in **listen-only** until a request or subscription needs to transmit; then the
  firmware switches to NORMAL itself (`CAN_MODE_SWITCH_FAIL` if that fails).
- One job at a time: an 8-entry one-shot request queue plus subscriptions polled round-robin,
  5 ms between jobs; each job ends with its reply or a timeout.
- 500 kbit/s, 8 MHz crystal; bit timing CNF1/2/3 = `C0 D8 01` (verified on this module, not to be changed).

### 3.2 Actions

| Action | Request | Reply |
|---|---|---|
| `req` | `{"id":1,"d":{"a":"req","i":"0x7E2","d":[2,33,195],"r":["0x7EA"],"t":500,"isotp":true}}` | `{"id":1,"d":{"a":"resp","i":"0x7EA","d":[…]}}`; timeout `{"a":"resp","err":"TIMEOUT","i":"0x7EA","req":"0x7E2"}` |
| `sub` | `{"id":1,"d":{"a":"sub","slot":0,"i":"0x7E0","d":[2,1,12,0,0,0,0,0],"r":["0x7E8"],"int":500,"t":200}}` | `SUB_OK`, then `{"id":1,"d":{"a":"sub","slot":0,"i":"0x7E8","d":[…]}}` per answer; timeouts are silent (counted in `subs`) |
| `unsub` | `{"a":"unsub","slot":0}` or `{"a":"unsub","slot":"all"}` | `UNSUB_OK` / `UNSUB_ALL` |
| `subs` | `{"a":"subs"}` | list of slots with `i`, `int`, `ok`, `to`, `isotp` |
| `mode` | `{"a":"mode","m":"normal"}` / `{"a":"mode","m":"listen"}` | `CAN_MODE`; **`listen` abandons the current job and drops every subscription** |
| `tx` | `{"a":"tx","i":"0x123","d":[…]}` (also the default when `a` is omitted) | none; `CAN_TX_FULL` on failure |
| `sniff` | `{"a":"sniff","on":true,"chg":false,"ids":["0x3C8"]}` (max 32 ids) | frames `{"id":1,"ts","d":{"a":"sniff","t":µs,"i","x":"<hex>"}}`; off by default |

Fields: `i` request id (hex string or int), `d` up to 8 data bytes, `r` up to 8 response ids
(default OBD range 0x7E8-0x7EF), `t` timeout ms (default 100), `int` poll interval ms
(default 1000), `e` extended id, `slot` 0-15.

`sniff` drops subscriptions and queued requests, forces listen-only and accepts every id (or only
`ids`); while it is on, `req`/`sub`/`tx` answer `CAN_SNIFF_ACTIVE`. `{"a":"sniff","on":false}`
restores the normal filters.

### 3.3 ISO-TP

- `"isotp": true` on `req`/`sub`: the gateway reassembles multi-frame answers up to 256 bytes and
  sends flow control `30 00 05` (BS 0, STmin 5 ms, `can_cfg` tunable) to the request id.
- A timeout ≥ 300 ms turns ISO-TP on even when `"isotp": false` is sent.
- Without ISO-TP the first frame with a matching id is returned (for a multi-frame answer: only the
  first frame).
- Normal addressing only: extended addressing (a sub-address byte before the PCI, 0x750/0x758) and
  multi-frame requests are not supported. How the diagnostics engine works around this:
  [TOYOTA_DIAGNOSTICS.md](TOYOTA_DIAGNOSTICS.md) §8.

### 3.4 Transmit safety rules in the firmware

- Before every frame the firmware re-reads CNF1/CNF2; if the bit timing does not verify, the frame
  is not sent (`tx_blocked` counter).
- Supervision once per second: a chip that does not answer or has lost its configuration is
  re-initialised into **listen-only** with TX disabled (`CAN_REINIT`, at most one per 5 s); TX comes
  back only when the host asks for NORMAL again (the backend's solicited watchdog does). A chip in
  an unexpected mode is put back (`CAN_MODE_FIX`; three failures → re-init).
- Bus-off while transmitting → re-init into listen-only (silence until the host re-enables TX).
- Supervision never drives the bus; the worst case is a silent gateway.

These rules exist because a firmware that forced a self-resetting MCP2515 into NORMAL with default
bit timing disturbed the car's CAN bus (dashboard warning lamps) on 2026-11-06.

Host-side rules (diagnostics engine, [TOYOTA_DIAGNOSTICS.md](TOYOTA_DIAGNOSTICS.md) §7): physical
request ids only, one request outstanding, ≥ 50 ms between requests, ≤ 20 frames/s; the backend's
periodic subscriptions are listed in [ARCHITECTURE.md](ARCHITECTURE.md) §3.

## 4. AVC-LAN (id 2)

One line per frame, emitted when the frame ends:
`{"id":2,"ts":…,"d":{"m":"1C6","s":"110","c":15,"d":["00","E0","5D","F7","46"]}}` (hex strings;
optional `"b":1` broadcast, `"nak":1` unacknowledged individual frame, `"echo":1` our own TX seen on
the bus). The host transmits with the same `d` shape (`"b":true` for broadcast; default broadcast
when `s` is `FFF`). Nav-emulation helpers: `{"id":0,"d":{"a":"avc_cfg","ack":"178"}}` (ACK
stretching for one slave address, `"000"` = off) and the 4-slot auto-reply table
`{"id":2,"d":{"a":"auto","slot":N,…}}`. What the backend decodes, answers and sends:
[AVC_BACKEND.md](AVC_BACKEND.md).

## 5. Errors (`{"id":0,"d":{"err":…}}`)

| Code | Meaning |
|---|---|
| `CAN_OFFLINE` | CAN controller absent or not initialised |
| `CAN_MODE_SWITCH_FAIL`, `MODE_SWITCH_FAIL` | the mode change did not verify |
| `CAN_TX_FULL` | raw `tx` could not be queued |
| `CAN_REQ_QUEUE_FULL` | 8 one-shot requests already queued |
| `CAN_SNIFF_ACTIVE` | `req`/`sub`/`tx` while sniff is on |
| `INVALID_SLOT`, `SLOT_NOT_FOUND` | slot outside 0-15 / not in use |
| `INVALID_MODE`, `UNKNOWN_ACTION` | bad `m` / `a` |
| `BAD_CAN_ID`, `BAD_CAN_DATA`, `BAD_RESP_ID` | malformed CAN fields |
| `BAD_AVC_HEADER`, `BAD_AVC_DATA`, `BAD_AUTO_SLOT` | malformed AVC-LAN fields |
| `RS485_HOLD_ACTIVE` | RS485 TX refused while the bench hold is on |
| `JSON_PARSE` | malformed line (with the parser's `code`) |
| `TIMEOUT` | inside a `resp`: no answer within `t` |

## 6. Limits

| Item | Limit |
|---|---|
| subscriptions | 16 slots |
| one-shot queue | 8 requests |
| response ids per query | 8 |
| sniff id filter | 32 ids |
| ISO-TP payload | 256 bytes |
| request payload | one frame (8 bytes) |
| AVC-LAN auto-reply slots | 4 |
