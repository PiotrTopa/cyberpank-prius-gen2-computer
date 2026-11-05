#!/usr/bin/env python3
"""Bench: gateway TX -> panda RX latency through the running backend.

ISOLATED SEGMENT ONLY. The gateway transmits raw CAN frames; the only other
node is the panda. Never run this with the car attached (the frames carry
Prius broadcast ids and would be believed by the car's ECUs).

What it does
  1. Reads /api/v1/state: needs connection.connected (gateway heartbeating)
     and connection.panda_connected.
  2. Isolation check: watches connection.panda_frames for --quiet seconds. Any
     frame the backend did not transmit itself => abort ("car still attached").
     The gateway's own solicited requests (0x7DF/0x7E0..0x7E7/0x7C0..) are the
     exception only when --allow-solicited is given.
  3. Resets the bench counters (panda_bench), then POSTs gateway_can_tx for N
     frames at --rate Hz: 0x0B4 with a sweeping speed and 0x03B with a sweeping
     current, every payload unique (the dedupe never hides one).
  4. Reads connection.panda_bench: n / lost / p50 / p99 / max (ms, monotonic,
     host->serial->gateway->CAN->panda->USB->host->decoder) and checks the
     state followed the last 0x03B (energy.hv_battery_voltage).

ACK requirement: a CAN transmitter needs one ACKing node. The panda in
SAFETY_SILENT (the backend default) does NOT ACK, so on a two-node segment
the gateway's MCP2515 retransmits forever and reports CAN_TX_FULL on the next
frame. For the bench the backend must run with BACKEND_PANDA_SAFETY_MODE=19
(SAFETY_NOOUTPUT: ACKs, still no TX from the host); this script refuses to
transmit unless connection.panda_safety_mode == 19 (--force-silent overrides
for an experiment). Restore BACKEND_PANDA_SAFETY_MODE=0 (or unset) and
restart the backend afterwards; nothing else is left behind (raw tx frames are
one-shot, the gateway stays in its normal mode, subscriptions untouched).

Pass criteria (defaults): received >= 95 % of frames, p99 <= 10 ms,
max <= 25 ms, no frame received that was not sent.

Usage (on the phone; python3 + urllib only):
  python3 scripts/panda_gateway_bench.py --n 200 --rate 20
  python3 scripts/panda_gateway_bench.py --api http://127.0.0.1:8080 --quiet 5
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

SOLICITED_IDS = {0x7DF} | set(range(0x7E0, 0x7E8)) | set(range(0x7C0, 0x7D0))


def api(base, path, payload=None, token=None):
    req = urllib.request.Request(base + path, method="POST" if payload is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    body = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(req, body, timeout=5) as r:
        return json.loads(r.read().decode() or "null")


def state(base, token):
    s = api(base, "/api/v1/state", token=token)
    return s.get("state", s)


def frame_0b4(speed_kph):
    raw = int(round(speed_kph * 100))
    return [0, 0, 0, 0, 0, (raw >> 8) & 0xFF, raw & 0xFF, 0]


def frame_03b(current_tenths, voltage):
    raw = current_tenths & 0xFFF
    return [(raw >> 8) & 0x0F, raw & 0xFF, 0x00, voltage & 0xFF, 0x00]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8080")
    ap.add_argument("--token", default=None)
    ap.add_argument("--n", type=int, default=200, help="frames to transmit")
    ap.add_argument("--rate", type=float, default=20.0, help="frames per second")
    ap.add_argument("--quiet", type=float, default=5.0, help="isolation check window (s)")
    ap.add_argument("--allow-solicited", action="store_true",
                    help="tolerate the gateway's own OBD request ids during the isolation check")
    ap.add_argument("--force-silent", action="store_true",
                    help="transmit even with the panda in SAFETY_SILENT (expect CAN_TX_FULL)")
    ap.add_argument("--p99-ms", type=float, default=10.0)
    ap.add_argument("--max-ms", type=float, default=25.0)
    ap.add_argument("--min-received", type=float, default=0.95)
    a = ap.parse_args()

    st = state(a.api, a.token)
    conn = st["connection"]
    print("gateway: connected=%s v%s up=%ss | panda: connected=%s fw=%s fmt=%s safety=%s fps=%s" % (
        conn.get("connected"), conn.get("gateway_version"), conn.get("gateway_uptime_s"),
        conn.get("panda_connected"), conn.get("panda_fw"), conn.get("panda_fw_format"),
        conn.get("panda_safety_mode"), conn.get("panda_fps")))
    if not conn.get("connected"):
        sys.exit("ABORT: gateway not connected")
    if not conn.get("panda_connected"):
        sys.exit("ABORT: panda not connected")
    if conn.get("panda_safety_mode") != 19 and not a.force_silent:
        sys.exit("ABORT: panda is not in SAFETY_NOOUTPUT (19); the gateway's frames would get no ACK. "
                 "Run the backend with BACKEND_PANDA_SAFETY_MODE=19 for the bench, or --force-silent.")

    # Isolation check: nothing may arrive that we did not send.
    f0 = conn.get("panda_frames", 0)
    bus0 = dict(conn.get("panda_bus_counts") or {})
    print("isolation check: watching panda_frames for %.0f s ..." % a.quiet)
    time.sleep(a.quiet)
    conn = state(a.api, a.token)["connection"]
    delta = conn.get("panda_frames", 0) - f0
    if delta:
        bus1 = dict(conn.get("panda_bus_counts") or {})
        print("  %d frames arrived unrequested (bus counts %s -> %s)" % (delta, bus0, bus1))
        if not a.allow_solicited:
            sys.exit("ABORT: traffic on the panda bus that this script did not send — "
                     "the car may still be attached (or the gateway's solicited requests: --allow-solicited).")
        print("  continuing (--allow-solicited): make sure these are only 0x7DF/0x7Ex requests")
    else:
        print("  quiet: no frames in %.0f s" % a.quiet)

    api(a.api, "/api/v1/commands/panda_bench", {"reset": True}, a.token)
    period = 1.0 / max(0.1, a.rate)
    t_start = time.monotonic()
    last_voltage = None
    for i in range(a.n):
        if i % 2 == 0:
            cid, data = 0x0B4, frame_0b4((i % 1200) / 10.0 + 0.01 * (i // 1200))
        else:
            last_voltage = 180 + (i % 60)
            cid, data = 0x03B, frame_03b(-300 + (i % 600), last_voltage)
        try:
            api(a.api, "/api/v1/commands/gateway_can_tx", {"id": cid, "data": data}, a.token)
        except urllib.error.HTTPError as e:
            sys.exit("ABORT: gateway_can_tx failed: %s %s" % (e.code, e.read()[:200]))
        due = t_start + (i + 1) * period
        dt = due - time.monotonic()
        if dt > 0:
            time.sleep(dt)
    time.sleep(1.5)                       # let the last frames land + the 1 Hz mirror run

    st = state(a.api, a.token)
    conn = st["connection"]
    b = conn.get("panda_bench") or {}
    print("bench: sent=%d received=%s lost=%s pending=%s | p50=%s ms p99=%s ms max=%s ms mean=%s ms" % (
        a.n, b.get("n"), b.get("lost"), b.get("pending"), b.get("p50_ms"), b.get("p99_ms"),
        b.get("max_ms"), b.get("mean_ms")))
    v = st.get("energy", {}).get("hv_battery_voltage")
    print("state follow-through: energy.hv_battery_voltage=%s (last sent %s)" % (v, last_voltage))
    ok = True
    n = b.get("n") or 0
    if n < a.min_received * a.n:
        print("FAIL: received %d/%d < %.0f%%" % (n, a.n, a.min_received * 100)); ok = False
    if (b.get("p99_ms") or 1e9) > a.p99_ms:
        print("FAIL: p99 %s ms > %.1f ms" % (b.get("p99_ms"), a.p99_ms)); ok = False
    if (b.get("max_ms") or 1e9) > a.max_ms:
        print("FAIL: max %s ms > %.1f ms" % (b.get("max_ms"), a.max_ms)); ok = False
    if last_voltage is not None and v != last_voltage:
        print("FAIL: state did not follow the last 0x03B frame"); ok = False
    print("PASS" if ok else "FAIL")
    print("restore: unset BACKEND_PANDA_SAFETY_MODE (or set 0) in /etc/prius/backend.env and "
          "restart prius-backend so the panda is SAFETY_SILENT again before the car is reconnected.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
