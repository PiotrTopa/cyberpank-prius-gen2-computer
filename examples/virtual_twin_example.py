#!/usr/bin/env python3
"""
Virtual Twin Usage Example

This example demonstrates how to use the new Virtual Twin architecture
for the CyberPunk Prius computer.

Run with: python examples/virtual_twin_example.py
"""

import sys
import time
sys.path.insert(0, '.')

from cyberpunk_computer.state.store import Store, StateSlice
from cyberpunk_computer.state.app_state import AppState, GearPosition
from cyberpunk_computer.state.actions import (
    ActionSource, SetVolumeAction, SetSpeedAction, SetGearAction
)
from cyberpunk_computer.state.rules import StateRule, RulesEngine, RulePriority
from cyberpunk_computer.io import (
    MockInputPort, MockOutputPort, LogOutputPort,
    IngressController, EgressController,
    RawMessage, OutgoingCommand,
    DEVICE_CAN, DEVICE_SATELLITE_BASE
)


def main():
    """Demonstrate Virtual Twin architecture."""
    
    print("=" * 60)
    print("  Virtual Twin Architecture Demo")
    print("=" * 60)
    print()
    
    # ─────────────────────────────────────────────────────────────────────
    # Step 1: Create the Store (Virtual Twin state)
    # ─────────────────────────────────────────────────────────────────────
    print("[1] Creating Store (Virtual Twin)...")
    store = Store(verbose=True)
    
    # Subscribe to state changes
    def on_vehicle_change(state: AppState):
        print(f"    📡 Vehicle state changed: gear={state.vehicle.gear.name}, speed={state.vehicle.speed_kmh} km/h")
    
    store.subscribe(StateSlice.VEHICLE, on_vehicle_change)
    
    # ─────────────────────────────────────────────────────────────────────
    # Step 2: Create IO Ports (mock for demo)
    # ─────────────────────────────────────────────────────────────────────
    print("[2] Creating IO ports (mock for demo)...")
    input_port = MockInputPort()
    output_port = LogOutputPort(prefix="[EGRESS]")
    
    # ─────────────────────────────────────────────────────────────────────
    # Step 3: Create Controllers
    # ─────────────────────────────────────────────────────────────────────
    print("[3] Creating Ingress and Egress controllers...")
    ingress = IngressController(store, input_port)
    egress = EgressController(store, output_port)
    
    # ─────────────────────────────────────────────────────────────────────
    # Step 4: Create Rules Engine with a sample rule
    # ─────────────────────────────────────────────────────────────────────
    print("[4] Creating Rules Engine...")
    rules_engine = RulesEngine(store)
    rules_engine.set_debug(True)
    
    # Example: Auto-mute when speed > 100 km/h (safety rule)
    class HighSpeedWarningRule(StateRule):
        @property
        def name(self) -> str:
            return "HighSpeedWarningRule"
        
        @property
        def watches(self):
            return {StateSlice.VEHICLE}
        
        def evaluate(self, old_state, new_state, store):
            if old_state is None:
                return
            
            # Check if speed just crossed 100 km/h threshold
            old_speed = old_state.vehicle.speed_kmh
            new_speed = new_state.vehicle.speed_kmh
            
            if old_speed < 100 and new_speed >= 100:
                print("    ⚠️  HIGH SPEED WARNING: Speed exceeded 100 km/h!")
    
    rules_engine.register(HighSpeedWarningRule())
    
    # ─────────────────────────────────────────────────────────────────────
    # Step 5: Start the system
    # ─────────────────────────────────────────────────────────────────────
    print("[5] Starting Virtual Twin...")
    ingress.start()
    
    print()
    print("-" * 60)
    print("  Simulating data flow...")
    print("-" * 60)
    print()
    
    # ─────────────────────────────────────────────────────────────────────
    # Simulation: Inject messages via input port (simulating gateway)
    # ─────────────────────────────────────────────────────────────────────
    
    # Simulate gear change from gateway
    print("[SIM] Gateway reports: Gear changed to DRIVE")
    input_port.inject_gateway_json({
        "id": DEVICE_CAN,
        "d": {
            "id": "120",  # Simulated gear CAN ID
            "data": [0x20]  # D gear
        },
        "ts": int(time.time() * 1000)
    })
    
    # Process the message
    ingress.update()
    
    # Simulate speed updates
    for speed in [30, 60, 90, 105]:
        print(f"\n[SIM] Gateway reports: Speed = {speed} km/h")
        store.dispatch(SetSpeedAction(speed, ActionSource.GATEWAY))
        time.sleep(0.1)
    
    print()
    print("-" * 60)
    print("  Simulating UI interaction...")
    print("-" * 60)
    print()
    
    # Simulate user changing volume via UI
    print("[UI] User changed volume to 35")
    store.dispatch(SetVolumeAction(35, ActionSource.UI))
    
    print()
    print("=" * 60)
    print("  Demo Complete!")
    print("=" * 60)
    print()
    print("Key points demonstrated:")
    print("  ✓ Store as single source of truth")
    print("  ✓ InputPort → IngressController → Store (data ingress)")
    print("  ✓ Store → EgressController → OutputPort (command egress)")
    print("  ✓ Rules Engine for reactive logic")
    print("  ✓ State subscriptions for UI updates")
    print()
    print("See docs/VIRTUAL_TWIN_ARCHITECTURE.md for full documentation.")


if __name__ == "__main__":
    main()
