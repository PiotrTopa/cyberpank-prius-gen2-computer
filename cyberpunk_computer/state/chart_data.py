"""
Chart Data Store - Mutable time-series buffer for chart visualizations.

Holds time-series history data that is continuously collected by the
ChartDataRule regardless of which UI screen is active. UI screens
read from this store for rendering charts — no business logic in UI.

Architecture:
    State changes -> ChartDataRule -> ChartDataStore (writes)
    UI Screens -> ChartDataStore (reads only)
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple


# Type alias for time-series sample: (timestamp, value)
Sample = Tuple[float, float]

# Color severity thresholds for delta-V / deviation values
DELTA_V_GREEN_MAX = 0.2    # Green: <= 0.2V
DELTA_V_AMBER_MAX = 0.8    # Amber: > 0.2V and <= 0.8V
                            # Red:   > 0.8V


@dataclass
class ChartStats:
    """Computed statistics for a time-series within a time window."""
    avg: float = 0.0
    min_val: float = 0.0
    max_val: float = 0.0
    count: int = 0
    current: Optional[float] = None


class ChartDataStore:
    """
    Mutable time-series storage for chart data.

    Collected continuously by ChartDataRule, independent of active screen.
    UI screens only read from here — never compute chart data themselves.

    Buffers:
      - delta_v_history:       Max block voltage deviation (battery health)
      - fuel_flow_history:     Fuel flow rate (L/h)
      - ice_temp_history:      ICE coolant temperature (°C)
      - inverter_temp_history: Inverter temperature (°C)
    """

    HISTORY_MAX = 3600  # 1 hour @ 1 sample/sec
    SAMPLE_INTERVAL = 1.0  # seconds between samples (default, at 1x speed)

    def __init__(self) -> None:
        # Time-series buffers: deque of (timestamp, value)
        self.delta_v_history: Deque[Sample] = deque(maxlen=self.HISTORY_MAX)
        self.fuel_flow_history: Deque[Sample] = deque(maxlen=self.HISTORY_MAX)
        self.ice_temp_history: Deque[Sample] = deque(maxlen=self.HISTORY_MAX)
        self.inverter_temp_history: Deque[Sample] = deque(maxlen=self.HISTORY_MAX)

        # Effective sample interval (scaled by replay speed)
        self._sample_interval: float = self.SAMPLE_INTERVAL

        # Last sample timestamps (per series)
        self._last_sample_times = {
            "delta_v": 0.0,
            "fuel_flow": 0.0,
            "ice_temp": 0.0,
            "inverter_temp": 0.0,
        }

        # Fuel flow accumulator (average multiple updates per second)
        self._fuel_flow_accumulator: List[float] = []

    def set_replay_speed(self, speed: float) -> None:
        """Adjust sample interval for replay speed.

        At 10x speed we sample 10x more often (wall-clock) so the chart
        data density stays the same in virtual time.
        """
        self._sample_interval = self.SAMPLE_INTERVAL / max(0.1, speed)

    # ─── Writers (called by ChartDataRule) ────────────────────────────

    def record_delta_v(self, block_voltages: Optional[tuple]) -> None:
        """Record delta-V sample from block voltages."""
        now = time.time()
        if now - self._last_sample_times["delta_v"] < self._sample_interval:
            return
        self._last_sample_times["delta_v"] = now

        if block_voltages and len(block_voltages) >= 2:
            delta = max(block_voltages) - min(block_voltages)
            self.delta_v_history.append((now, delta))

    def accumulate_fuel_flow(self, flow_rate: float, ice_running: bool) -> None:
        """Accumulate fuel flow sample (may be called many times per second)."""
        flow = flow_rate if ice_running else 0.0
        self._fuel_flow_accumulator.append(flow)

    def flush_fuel_flow(self) -> None:
        """Flush accumulated fuel flow into a 1-second average sample."""
        now = time.time()
        if now - self._last_sample_times["fuel_flow"] < self._sample_interval:
            return
        self._last_sample_times["fuel_flow"] = now

        if self._fuel_flow_accumulator:
            avg_flow = sum(self._fuel_flow_accumulator) / len(self._fuel_flow_accumulator)
        else:
            avg_flow = 0.0
        self._fuel_flow_accumulator.clear()
        self.fuel_flow_history.append((now, avg_flow))

    def record_ice_temp(self, temp: Optional[float]) -> None:
        """Record ICE coolant temperature sample."""
        now = time.time()
        if now - self._last_sample_times["ice_temp"] < self._sample_interval:
            return
        self._last_sample_times["ice_temp"] = now

        if temp is not None:
            self.ice_temp_history.append((now, temp))

    def record_inverter_temp(self, temp: Optional[float]) -> None:
        """Record inverter temperature sample."""
        now = time.time()
        if now - self._last_sample_times["inverter_temp"] < self._sample_interval:
            return
        self._last_sample_times["inverter_temp"] = now

        if temp is not None:
            self.inverter_temp_history.append((now, temp))

    # ─── Readers (called by UI screens) ───────────────────────────────

    def get_delta_v_stats(self, time_window: int) -> ChartStats:
        """Get delta-V statistics for a time window."""
        return self._compute_stats(self.delta_v_history, time_window)

    def get_current_delta_v(self, block_voltages: Optional[tuple]) -> Optional[float]:
        """Compute current delta-V from live block voltages."""
        if block_voltages and len(block_voltages) >= 2:
            return max(block_voltages) - min(block_voltages)
        return None

    def _compute_stats(
        self, data: Deque[Sample], time_window: int
    ) -> ChartStats:
        """Compute statistics for samples within time_window seconds."""
        now = time.time()
        visible = [v for ts, v in data if now - ts <= time_window]
        if not visible:
            return ChartStats()
        return ChartStats(
            avg=sum(visible) / len(visible),
            min_val=min(visible),
            max_val=max(visible),
            count=len(visible),
            current=visible[-1] if visible else None,
        )

    @staticmethod
    def severity_color_key(value: float) -> str:
        """
        Return color severity key for a delta-V value.

        Returns:
            'green', 'amber', or 'red'
        """
        if value <= DELTA_V_GREEN_MAX:
            return "green"
        elif value <= DELTA_V_AMBER_MAX:
            return "amber"
        return "red"
