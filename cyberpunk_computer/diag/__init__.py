"""
All-ECU diagnostics for the Prius NHW20 (docs/TOYOTA_DIAGNOSTICS.md).

    isotp       ISO 15765-2 framing, normal and extended addressing
    dtc         DTC number encoding
    services    request builders / response parsers / safety allow-list
    ecus        ECU registry (address, bus, confidence)
    dictionary  DTC dictionary from the repair manual (data/dtc_nhw20.json)
    engine      scheduler: one request at a time through the gateway
    autoclear   auto-clear of whitelisted recurring codes
    report      scan report files and the clear log
    sim         ECU + gateway simulator (tests, dry runs)
    commands    REST commands (diag_scan, diag_clear, ...)
    wiring      backend integration
"""
