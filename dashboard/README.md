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

Data: one WebSocket to `/api/v1/stream` (state snapshots, `hooks/useAppStream.ts`) and REST
`POST /api/v1/commands/<name>` for actions (`lib/api.ts`). In production both use the page's own host;
in dev mode the backend address is hard-coded in `src/lib/api.ts`. Ages are computed against the
backend clock (the POCO has no reliable RTC).

Tabs (`src/tabs/`): Overview, Satellites, Telemetry (metrics history charts), Battery (long-run health of
the HV pack and the 12 V battery from `/api/v1/battery/*`, [docs/BATTERY_HEALTH.md](../docs/BATTERY_HEALTH.md)), Diagnostics (all-ECU
diagnostics, [docs/TOYOTA_DIAGNOSTICS.md](../docs/TOYOTA_DIAGNOSTICS.md)), Controls. The header shows aux
voltage, computer draw, power mode and cabin temperature from the powerbox state.
