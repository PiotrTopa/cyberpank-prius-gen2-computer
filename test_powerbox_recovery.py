import time
import os
import sys

from cyberpunk_computer.backend.service import BackendService, BackendConfig
from cyberpunk_computer.state.actions import SetPowerboxConnectionAction


def test_powerbox_recovery():
    print("Testing Powerbox Auto-Recovery...")
    
    config = BackendConfig(
        powerbox_auto_recover=True,
        powerbox_enabled=True,
        powerbox_stale_s=1.0,
        powerbox_recover_cooldown_s=2.0,
        powerbox_recover_requires_acc=False,
        replay_file="fake.ndjson",  # Force replay mode so it uses test IO instead of serial auto-discover
        api_port=18080
    )
    
    # Replay mode creates VirtualTwin but skips the powerbox serial seam.
    # To test watchdog, we need a backend service.
    # Actually, replay mode ignores powerbox_port in BackendService.
    # Let's not use replay_file, let's use a dummy port.
    config = BackendConfig(
        powerbox_auto_recover=True,
        powerbox_enabled=True,
        powerbox_stale_s=1.0,
        powerbox_recover_cooldown_s=2.0,
        powerbox_recover_requires_acc=False,
        gateway_port="/dev/null", 
        powerbox_port="/dev/null",
        api_port=18080,
        auto_discover=False
    )
    
    svc = BackendService(config)
    svc.build()
    
    # Mock the powerbox serial so force_reconnect doesn't actually interact with /dev/null
    class MockSerial:
        def force_reconnect(self):
            self.reconnected = True
            print("  [Mock] force_reconnect called")
    
    mock_serial = MockSerial()
    mock_serial.reconnected = False
    svc._powerbox_serial = mock_serial
    
    store = svc.twin.store
    
    # 1. Simulate READY arrives (link connected)
    print("Simulating POWERBOX_READY...")
    store.dispatch(SetPowerboxConnectionAction(connected=True))
    
    pb = store.state.powerbox
    assert pb.connected == True
    assert pb.last_update_time > 0, "Bug: last_update_time was not set by SetPowerboxConnectionAction"
    
    # 2. Wait for it to become stale
    print("Waiting for link to become stale (1.1s)...")
    time.sleep(1.1)
    
    # 3. Trigger watchdog
    svc._powerbox_watchdog_tick()
    
    pb = store.state.powerbox
    assert pb.connected == False, "Watchdog should have disconnected the powerbox"
    assert svc._pb_stale == True, "Watchdog should have marked _pb_stale as True"
    assert svc._pb_recover_attempts == 1, "Watchdog should have attempted recovery"
    assert mock_serial.reconnected == True, "force_reconnect should have been called"
    
    # 4. Simulate READY arrives again after reboot
    print("Simulating READY after reboot...")
    store.dispatch(SetPowerboxConnectionAction(connected=True))
    pb = store.state.powerbox
    assert pb.connected == True
    
    # Watchdog tick immediately after should NOT trip (because READY updated last_update_time)
    svc._powerbox_watchdog_tick()
    pb = store.state.powerbox
    assert pb.connected == True, "Watchdog tripped again falsely! Core issue is present if this fails."
    
    print("Test Passed: Watchdog handles recovery correctly without false loops.")

if __name__ == "__main__":
    test_powerbox_recovery()
