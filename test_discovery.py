#!/usr/bin/env python3
"""
Standalone unit tests for USB serial role discovery + hotplug monitor.

No pytest dependency (matches the repo's other test_*.py scripts). Run with:

    python3 test_discovery.py

Exercises the pure/injectable logic only — no real serial hardware needed.
"""

import sys
import time

from cyberpunk_computer.io.discovery import (
    ROLE_GATEWAY,
    ROLE_POWERBOX,
    classify_lines,
    discover_roles,
    discover_roles_by_port,
    discover_roles_combined,
    identify_port,
    parse_hub_port,
)
from cyberpunk_computer.io.usb_monitor import UsbSerialMonitor

_failures = []


def check(name, cond):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        _failures.append(name)


def test_classify_lines():
    print("classify_lines")
    # Explicit IDENT role.
    check(
        "ident-gateway",
        classify_lines(['{"id":0,"d":{"msg":"IDENT","role":"gateway","ver":"2.27.0"}}'])
        == ROLE_GATEWAY,
    )
    check(
        "ident-powerbox",
        classify_lines(['{"id":0,"ts":1,"seq":2,"d":{"msg":"IDENT","role":"powerbox"}}'])
        == ROLE_POWERBOX,
    )
    # READY banner fallback (no explicit role field).
    check(
        "banner-gateway",
        classify_lines(['{"id":0,"d":{"msg":"GATEWAY_READY","ver":"2.27.0","can":"CAN_READY"}}'])
        == ROLE_GATEWAY,
    )
    check(
        "banner-powerbox",
        classify_lines(['{"id":0,"d":{"msg":"POWERBOX_READY","ver":"1.1.0"}}'])
        == ROLE_POWERBOX,
    )
    # Telemetry on id 1/2 must NOT decide identity (both devices use them).
    check(
        "ignore-telemetry",
        classify_lines([
            '{"id":1,"d":{"v":13.2}}',
            '{"id":2,"d":{"acc":true}}',
        ])
        is None,
    )
    # Identity is taken from the first conclusive SYSTEM line amid noise.
    check(
        "amid-noise",
        classify_lines([
            "garbage line",
            "",
            '{"id":2,"d":{"acc":true}}',
            '{"id":0,"d":{"msg":"IDENT","role":"powerbox"}}',
        ])
        == ROLE_POWERBOX,
    )
    # Malformed JSON is ignored, not fatal.
    check("bad-json", classify_lines(['{"id":0,"d":{']) is None)
    check("empty", classify_lines([]) is None)


def _fake_reader(table):
    """Build a discovery reader returning canned lines per path."""

    def reader(path, baudrate, probe_timeout, send_whoami):
        return table.get(path, [])

    return reader


def test_identify_and_discover():
    print("identify_port / discover_roles")
    table = {
        "/dev/serial/by-id/usb-MicroPython_Board_AAA-if00": [
            '{"id":2,"d":{"acc":true}}',
            '{"id":0,"d":{"msg":"IDENT","role":"powerbox","ver":"1.1.0"}}',
        ],
        "/dev/serial/by-id/usb-MicroPython_Board_BBB-if00": [
            '{"id":1,"d":{"f":2}}',
            '{"id":0,"d":{"msg":"IDENT","role":"gateway","ver":"2.27.0"}}',
        ],
        "/dev/serial/by-id/usb-Some_Other_Device-if00": [
            "random noise",
        ],
    }
    reader = _fake_reader(table)

    check(
        "identify-powerbox",
        identify_port(
            "/dev/serial/by-id/usb-MicroPython_Board_AAA-if00", reader=reader
        )
        == ROLE_POWERBOX,
    )
    check(
        "identify-unknown",
        identify_port(
            "/dev/serial/by-id/usb-Some_Other_Device-if00", reader=reader
        )
        is None,
    )

    roles = discover_roles(candidates=list(table.keys()), reader=reader)
    check("discover-gateway", roles.get(ROLE_GATEWAY).endswith("BBB-if00"))
    check("discover-powerbox", roles.get(ROLE_POWERBOX).endswith("AAA-if00"))

    # Enumeration order must not matter (resilient to acm renumbering).
    roles_rev = discover_roles(candidates=list(reversed(list(table.keys()))), reader=reader)
    check("discover-order-independent", roles_rev == roles)

    # skip: a held-open port is not reprobed.
    roles_skip = discover_roles(
        candidates=list(table.keys()),
        skip=["/dev/serial/by-id/usb-MicroPython_Board_AAA-if00"],
        reader=reader,
    )
    check("discover-skip", ROLE_POWERBOX not in roles_skip and ROLE_GATEWAY in roles_skip)


def test_monitor():
    print("UsbSerialMonitor")
    seq = [
        ["/dev/ttyACM0"],
        ["/dev/ttyACM0"],
        ["/dev/ttyACM0", "/dev/ttyACM1"],  # add
        ["/dev/ttyACM1"],                   # remove ACM0
    ]
    idx = {"i": 0}

    def enumerator():
        i = min(idx["i"], len(seq) - 1)
        idx["i"] += 1
        return seq[i]

    events = []

    def on_change(added, removed, current):
        events.append((sorted(added), sorted(removed)))

    mon = UsbSerialMonitor(on_change, interval=0.05, enumerator=enumerator)
    mon.prime(["/dev/ttyACM0"])
    mon.start()
    time.sleep(0.5)
    mon.stop()

    check("monitor-detected-add", (["/dev/ttyACM1"], []) in events)
    check("monitor-detected-remove", ([], ["/dev/ttyACM0"]) in events)


def test_port_mapping():
    print("port-based discovery")
    check("parse-1-1.1", parse_hub_port("1-1.1") == ("1-1", 1))
    check("parse-1-1.2", parse_hub_port("1-1.2") == ("1-1", 2))
    check("parse-nested", parse_hub_port("1-1.3.2") == ("1-1.3", 2))
    check("parse-roothub", parse_hub_port("1-1") == ("usb1", 1))

    # Devices maintained on dedicated ports: powerbox=1, gateway=2.
    topo = {
        "/dev/serial/by-id/usb-MicroPython_Board_AAA-if00": ("1-1", 1),  # powerbox
        "/dev/serial/by-id/usb-MicroPython_Board_BBB-if00": ("1-1", 2),  # gateway
        "/dev/serial/by-id/usb-Other-if00": ("1-1", 3),                  # unmapped
    }
    resolver = lambda p: topo.get(p, (None, None))  # noqa: E731

    roles = discover_roles_by_port(candidates=list(topo.keys()), resolver=resolver)
    check("port-powerbox", roles.get(ROLE_POWERBOX).endswith("AAA-if00"))
    check("port-gateway", roles.get(ROLE_GATEWAY).endswith("BBB-if00"))

    # Order independence (resilient to acm renumbering).
    roles_rev = discover_roles_by_port(
        candidates=list(reversed(list(topo.keys()))), resolver=resolver
    )
    check("port-order-independent", roles_rev == roles)

    # Hub filter excludes other hubs.
    roles_hub = discover_roles_by_port(
        candidates=list(topo.keys()), resolver=resolver, hub="9-9"
    )
    check("port-hub-filter", roles_hub == {})

    # A silent/wedged device still maps purely from topology (no probing).
    silent = discover_roles_by_port(
        candidates=["/dev/serial/by-id/usb-MicroPython_Board_AAA-if00"],
        resolver=resolver,
    )
    check("port-silent-device", silent.get(ROLE_POWERBOX) is not None)


def main():
    test_classify_lines()
    test_identify_and_discover()
    test_port_mapping()
    test_monitor()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} -> {_failures}")
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
