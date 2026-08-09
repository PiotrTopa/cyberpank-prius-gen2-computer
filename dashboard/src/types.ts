// Shape of the AppState stream from /api/v1/stream (subset used by the UI).

export interface Powerbox {
  connected?: boolean;
  acc_on?: boolean;
  batt_present?: boolean;
  system_voltage?: number;
  current_draw_a?: number;
  power_draw_w?: number | null;
  poco_power_w?: number | null;
  poco_core_temp?: number | null;
  poco_gpu_temp?: number | null;
  poco_ema_temp?: number | null;
  fan_duty_pct?: number | null;
  /** BMP280 @0x77 — INSIDE the computer box (chassis air) */
  bmp_t?: number;
  bmp_p?: number;
  /** BMP280 @0x76 — OUTSIDE the box (cabin ambient) */
  bmp2_t?: number | null;
  bmp2_p?: number | null;
  /** AHT20 on the powerbox board — inside the box */
  aht_t?: number;
  aht_h?: number;
  energy_mah?: number;
  undervoltage?: boolean;
  shutdown_requested?: boolean;
  shutdown_reason?: string;
  uv_threshold?: number | null;
  uv_recover?: number | null;
  out1?: boolean | null;
  out2?: boolean | null;
  out3?: boolean | null;
  poco_alive?: boolean | null;
  pm_state?: string;
  powerbox_hb?: number | null;
  /** USB-port VBUS relay states [ch1..ch4], true = port powered.
   *  ch4=gateway (socket 2), ch3=MFD Pi (socket 3), ch2=RTL-SDR (socket 4),
   *  ch1=spare. From the powerbox STATUS "rly" telemetry (fw >= 1.7). */
  relays?: (boolean | null)[] | null;
  last_update_time?: number | null;
  power_mode?: string;
}

export interface Vehicle {
  speed_kmh: number;
  gear: string;
  ice_running: boolean;
  ready_mode?: boolean;
  ev_mode?: boolean;
  ig_on?: boolean;
  rpm?: number | null;
  ice_coolant_temp?: number | null;
  fuel_level?: number;
  lpg_level?: number;
  active_fuel?: string;
}

export interface Energy {
  hv_battery_voltage?: number | null;
  battery_soc: number;
  motor_power_kw?: number;
  generator_power_kw?: number;
  ice_power_kw?: number;
  regen_active?: boolean;
  charging?: boolean;
}

export interface Climate {
  inside_temp?: number | null;
  outside_temp?: number | null;
  ac_on?: boolean;
  fan_speed?: number;
  target_temp?: number;
}

export interface Connection {
  connected: boolean;
  gateway_version?: string | null;
  can_ready?: boolean;
  avc_ready?: boolean;
  last_message_time?: number | null;
  gateway_hb?: number | null;
  gateway_uptime_s?: number | null;
  last_heartbeat_time?: number | null;
  gateway_usb_power?: boolean | null;
  gateway_usb_power_desired?: boolean | null;
  mfd_state?: string | null;
  mfd_usb_power?: boolean | null;
  mfd_reachable?: boolean | null;
}

export interface SatelliteNode {
  device_id: number;
  name?: string;
  online?: boolean;
  last_seen?: number;
  boot_id?: number;
  fw_version?: string;
  config_synced?: boolean;
  desired_config?: Record<string, unknown>;
  reported_config?: Record<string, unknown>;
}

export interface Satellites {
  nodes?: Record<string, SatelliteNode>;
  power_holders?: string[];
  power_requested?: boolean;
  queue_depth?: number;
  active_job?: string;
  last_update_time?: number;
}

export interface AppState {
  powerbox: Powerbox;
  vehicle: Vehicle;
  energy: Energy;
  climate?: Climate;
  connection?: Connection;
  satellites?: Satellites;
}

export type TimeRange = '1h' | '24h' | '7d';
export type Tab = 'dashboard' | 'charts' | 'controls';
export type Series = { time: string; value: number }[];
