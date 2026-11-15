// Shape of the AppState stream from /api/v1/stream (subset used by the UI).
import type { FeederStatus } from './lib/mapLayer';

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
  fan_duty_pct?: number | null;
  /** Manual fan override in % (null/undefined = automatic control) */
  fan_override_pct?: number | null;
  /** Chassis fan tach RPM (firmware >= 2.0) */
  fan_rpm?: number | null;
  /** BMP280 @0x76 — cabin ambient (the only air sensor; the computer has no
   *  box any more, so the BMP1/AHT20 box sensors are gone, see THERMAL_MODEL.md) */
  bmp2_t?: number | null;
  bmp2_p?: number | null;
  energy_mah?: number;
  undervoltage?: boolean;
  shutdown_requested?: boolean;
  shutdown_reason?: string;
  uv_threshold?: number | null;
  uv_recover?: number | null;
  poco_alive?: boolean | null;
  /** Legacy firmware "p2" echo; there is no second POCO (single computer since 2026-11-04) */
  poco2_alive?: boolean | null;
  /** Powerbox state machine (fw >= 2.0): normal | shutdown | sleep */
  pm_state?: string;
  powerbox_hb?: number | null;
  /** Relay board states [ch1..ch8], true = load powered, null = unknown.
   *  ch1 = powerbox restart relay (always null, not a load), ch2 = MFD Pi,
   *  ch3 = CAN hub (gateway + panda), ch4 = camera hub (roadsense), ch5 = RS485
   *  satellite rail, ch6-8 spare. Read back from the PCF8574 by
   *  the firmware (STATUS "rly", fw >= 2.1). */
  relays?: (boolean | null)[] | null;
  /** fw >= 2.1 boot diagnostics: machine.reset_cause() (1 power-on, 3 watchdog),
   *  firmware uptime, boot banners seen, relay drift repairs / I2C errors. */
  reset_cause?: number | null;
  uptime_s?: number | null;
  boot_count?: number;
  relay_fix_count?: number | null;
  relay_err_count?: number | null;
  /** RS485 satellite rail = relay ch5 (derived by the backend) */
  rs485_power?: boolean | null;
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
  /** MFD health/debug (backend.mfd_power; wall-clock timestamps) */
  mfd_state_since?: number | null;
  mfd_last_ok_ping?: number | null;
  mfd_power_cycles?: number | null;
  mfd_last_boot_s?: number | null;
}

export type SatTransport = 'rs485' | 'ble' | 'wifi';

/** Enrolled satellite (keychain). Fields after `reported_config` arrive with
 *  the multi-transport registry (docs/SATELLITES.md §5); all optional so the
 *  RS485-only backend still renders. */
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
  /** rs485 | ble | wifi (absent = rs485, old backend) */
  transport?: SatTransport | string;
  address?: string | null;
  /** Ed25519 fingerprint, 26-char base32 (null for RS485 auth "none") */
  fp?: string | null;
  /** in keychain */
  trusted?: boolean;
  /** handshake done on the current link */
  authenticated?: boolean;
  /** link up */
  linked?: boolean;
  rssi?: number | null;
  caps?: string[] | null;
}

export interface SatTransportStatus {
  available?: boolean;
  state?: string;
  detail?: string;
  last_discovery?: number;
  devices_seen?: number;
  queue_depth?: number;
  active_job?: string;
}

/** Seen but not enrolled ("available"). */
export interface DiscoveredSatellite {
  transport: SatTransport | string;
  address: string;
  name?: string | null;
  fp?: string | null;
  rssi?: number | null;
  last_seen?: number;
  linked?: boolean;
  pubkey?: string | null;
  trusted?: boolean;
}

export interface SatPairing {
  open?: boolean;
  /** backend epoch seconds */
  until?: number;
  transport?: SatTransport | string | null;
}

export interface Satellites {
  nodes?: Record<string, SatelliteNode>;
  power_holders?: string[];
  power_requested?: boolean;
  queue_depth?: number;
  active_job?: string;
  transports?: Partial<Record<SatTransport, SatTransportStatus>> & Record<string, SatTransportStatus>;
  discovered?: DiscoveredSatellite[];
  pairing?: SatPairing;
  last_update_time?: number;
}

/** roadsense perception stack on this computer (backend.roadsense_power):
 *  started with ACC, stopped after the key-off grace; its own API lives at
 *  api_url (port api_port, 8090 on the car computer). temps = every thermal
 *  zone of the phone in C (heat-model input). */
export interface Roadsense {
  enabled?: boolean;
  /** off | camera_on | starting | running | grace | stopping | preview (camera hub only) */
  state?: string;
  /** auto (follow ACC) | on | off */
  mode?: string;
  desired?: boolean;
  camera_power?: boolean | null;
  api_ok?: boolean | null;
  api_url?: string;
  api_port?: number;
  health?: Record<string, unknown> | null;
  units?: Record<string, string> | null;
  temps?: Record<string, number> | null;
  hottest_zone?: string | null;
  hottest_c?: number | null;
  state_since?: number | null;
  last_api_ok?: number | null;
  restarts?: number;
  last_error?: string | null;
  /** on-demand dashboard camera preview (backend.camera_preview) */
  camera_preview?: CameraPreview | null;
  /** position feeder -> roadsense mapd (backend.position_feeder); null while the feeder is off */
  position?: FeederStatus | null;
}

/** Camera preview lease status (roadsense.camera_preview). */
export interface CameraPreview {
  enabled?: boolean;
  active?: boolean;
  /** off | powering | enumerating | waiting_for_frame | live | error | grace */
  phase?: string;
  /** the camera path itself (also during the grace) */
  camera_phase?: string;
  /** no lease, camera kept on for a resume (relay 4 still on) */
  grace?: boolean;
  grace_left_s?: number | null;
  grace_s?: number;
  session_left_s?: number | null;
  reason?: string | null;
  /** stack (roadsense running) | direct (camera hub powered for the preview) */
  source?: string | null;
  leases?: number;
  started_at?: number | null;
  ttl_s?: number;
  max_session_s?: number;
  scaling?: boolean;
}

export interface AppState {
  powerbox: Powerbox;
  vehicle: Vehicle;
  energy: Energy;
  climate?: Climate;
  connection?: Connection;
  satellites?: Satellites;
  roadsense?: Roadsense;
  diagnostics?: Diagnostics;
}

/** One trouble code of an ECU, annotated from the repair-manual dictionary. */
export interface DiagDtc {
  code: string;
  status: string[];
  sources?: string[];
  description?: string;
  system?: string;
  manual_page?: string | null;
  manual_url?: string | null;
  trouble_area?: string[];
  known?: boolean;
  other_ecu?: boolean;
  master_warning?: 'all' | 'some' | 'none';
  inf_codes?: string[];
}

export interface DiagEcu {
  key: string;
  name: string;
  address?: string | null;
  response_address?: string | null;
  sub_address?: string | null;
  protocol?: string;
  bus?: string;
  confidence?: string;
  probe?: boolean;
  note?: string;
  can_clear?: boolean;
  status: string;
  responded?: boolean;
  dtcs?: DiagDtc[];
  mil?: boolean | null;
  dtc_count?: number | null;
  monitors?: { complete: string[]; incomplete: string[] } | null;
  freeze_frame?: { dtc?: string; values?: Record<string, { value: number; unit: string }> } | null;
  ident?: Record<string, unknown>;
  last_scan_ts?: number | null;
  last_clear?: { ts: number; ok: boolean; result: string; cleared?: string[]; remaining?: string[] | null; origin?: string } | null;
  error?: string | null;
  partial?: boolean;
  log?: string[];
}

export interface DiagScan {
  state: string;
  op?: string | null;
  total?: number;
  done?: number;
  current?: string | null;
  started_ts?: number | null;
  finished_ts?: number | null;
  report_file?: string | null;
  error?: string | null;
  kinds?: string[];
}

export interface DiagAutoClearEntry { ecu: string; code: string; enabled?: boolean }
export interface DiagAutoClearAction { ts: number; ecu: string; codes: string[]; ok: boolean; result: string; deferred?: boolean }

export interface DiagAutoClear {
  enabled: boolean;
  codes: DiagAutoClearEntry[];
  trigger?: string;
  speed_policy?: string;
  min_interval_s?: number;
  max_per_day?: number;
  poll_s?: number;
  last_check_ts?: number | null;
  last_action?: DiagAutoClearAction | null;
  recent?: DiagAutoClearAction[];
  blocked?: Record<string, { by: string[]; codes: string[]; reason: string; since: number }>;
  waiting?: Record<string, string>;
  counters?: Record<string, number>;
}

export interface Diagnostics {
  ecus?: Record<string, DiagEcu> | null;
  scan?: DiagScan | null;
  vehicle?: { vin?: string } | null;
  last_read?: Record<string, unknown> | null;
  autoclear?: DiagAutoClear | null;
  mil_on?: boolean;
  last_scan_time?: number | null;
}

export type TimeRange = '1h' | '24h' | '7d';
export type Tab = 'dashboard' | 'map' | 'satellites' | 'charts' | 'diagnostics' | 'alerts' | 'controls';
export type Series = { time: string; value: number }[];
