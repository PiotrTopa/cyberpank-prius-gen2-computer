# Dashboard

Web UI of the backend (React 19, TypeScript, Vite, Tailwind, Recharts). The backend serves the
built `dist/` at `/` on port 8080 when `dashboard/dist` exists next to `cyberpunk_computer/`
(`api/app.py`); `POCO/sync.sh --app` copies it to the phone.

```bash
npm ci
npm run dev       # dev server; talks to the backend at the address in src/lib/api.ts
npm run build     # tsc + vite build -> dist/
npm run lint
npm test          # unit tests (camera preview, map layer, battery view model)
```

Purpose: the dashboard is for the car **parked / away from it** (what is running, power, thermal,
environment, camera, remote control) and is built for a phone over LTE. The live driving display is
the MFD in the car.

Data: one WebSocket to `/api/v1/stream` (`hooks/useAppStream.ts`) and REST
`POST /api/v1/commands/<name>` for actions (`lib/api.ts`). The socket subscribes to only the state
sections the open tab reads (`KEYS` in `App.tsx`) as merge-patch deltas (`?keys=...&interval=...&delta=1`,
backend `api/stream_filter.py`): every 5 s with the car parked, 2 s with ACC on, 1 s on Drive with ACC on,
in the debug drawer, and for 30 s after a command. A tab change re-subscribes over the open socket; the
socket closes after the page has been hidden for 20 s. Home's power numbers come from one
`GET /api/v1/summary/power` per minute (averages, 24 h energy, 12 V sparkline); only the History tab
queries `/api/v1/metrics`. In production all of it uses the page's own host; in dev mode the backend
address is hard-coded in `src/lib/api.ts`. Ages are computed against the backend clock (the POCO has no
reliable RTC).

Tabs (bottom bar on a phone, `src/tabs/`; all but Home load on demand):

| Tab | Contents |
|---|---|
| Home | 12 V, car on/off, draw now, energy used in 24 h; what is running (poco, poco2 with mode/restart, MFD Pi, gateway + panda, roadsense/chris with mode/restart, powerbox, head unit); power; thermal with fan Auto/On/Off; environment; camera preview; USB hub relays (ch2 MFD Pi, ch3 CAN hub, ch4 camera hub, ch5 RS485 rail); satellites |
| Drive | car-on data: vehicle, hybrid energy, roadsense, map layer |
| History | metrics charts (12 V, draw, cabin) and the battery health view ([docs/BATTERY_HEALTH.md](../docs/BATTERY_HEALTH.md)) |
| Car | all-ECU diagnostics ([docs/TOYOTA_DIAGNOSTICS.md](../docs/TOYOTA_DIAGNOSTICS.md)), alerts, fuel |
| System | satellite rail and pings, under-voltage protection, system status, satellite transports and pairing, debug view |

Old links (`#map`, `#controls`, ...) open the tab that now holds that content.
