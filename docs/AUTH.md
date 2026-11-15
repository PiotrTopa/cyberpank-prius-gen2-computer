# Network API access: firewall, token, browser sessions, pairing links

Code: `cyberpunk_computer/api/auth.py` (decision + routes), `api/auth_cli.py` (`prius-auth`),
`POCO/rootfs/etc/nftables.d/50_prius_backend.nft` (firewall). Tests: `tests/test_api_auth.py`.

## 1. Two layers

1. **Firewall** (`prius_guard` table, priority -10, before the pmOS filter chain): TCP 8080
   (backend), 8090 (roadsense) and 8081/8082 (ZeroMQ) are dropped on every interface except:

   | Interface | 8080 | 8090 | 8081/8082 | Who |
   |---|---|---|---|---|
   | `lo` | yes | yes | yes | processes on the phone (backend roadsense proxy, tooling) |
   | `wg*` (`wg-homelab`, 10.200.0.14) | yes | yes | yes | owner devices and laptop over WireGuard |
   | `usb*` (`usb0` = MFD Pi, 192.168.100.0/24) | yes | yes | yes | MFD frontend (ZMQ), pmOS USB lifeline |
   | `wlan*`, only to `10.42.0.1` from `10.42.0.0/24` | yes | yes | **no** | clients of the car AP `prius` |
   | anything else (Wi-Fi client mode, LTE `qmapmux*`, IPv6) | no | no | no | |

   In Wi-Fi client mode (home or another network) the phone has a different address on `wlan0`,
   so the ports are closed there. Sources in 192.168.100.0/24 are dropped unless they arrive on
   `usb*`. A drop in any input base chain is final, so roadsense's own `50_roadsense.nft`
   (`tcp dport 8090 accept` on every interface) no longer opens 8090 beyond this list.

2. **Authentication on 8080** when `BACKEND_AUTH_TOKEN` is set (no token = everything open, the
   old behaviour). A request gets in by the first of:

   | Credential | Used by |
   |---|---|
   | trusted peer address, `BACKEND_AUTH_TRUSTED` (default `127.0.0.0/8,::1`) | anything on the phone itself: `thermal_campaign.py`, `panda_gateway_bench.py`, `diag_cli.py --url http://127.0.0.1:8080` |
   | `Authorization: Bearer <token>` (WebSocket also `?token=`) | laptop tools over WireGuard (`diag_cli.py --token`/`PRIUS_API_TOKEN`), the head-unit GNSS feeder (`POST /api/v1/position/gnss`, [POSITION_FEEDER.md](POSITION_FEEDER.md) §4) |
   | session cookie `prius_session` | browsers: the dashboard, `/roadsense/` |

   Without credentials: `/api/...`, non-GET and non-browser requests get **401**
   (`WWW-Authenticate: Bearer`); a browser page request is redirected to `/login?next=...`;
   a WebSocket handshake is refused (HTTP 403). Public: `/health`, `/login`, `/logout`,
   `/auth/pair`, `/auth/status`, `/manifest.webmanifest`, `/favicon.svg`, `/icons/*`.

   Not covered by the token: **ZeroMQ 8081/8082** (the MFD frontend's state feed and command
   channel). It has no authentication; the firewall keeps it to `lo`, `usb*` and `wg*`.
   The MFD Pi needs no token (it talks ZMQ only). UDP 123 (backend NTP for the Pi) is answered
   on the gadget subnet only (`BACKEND_NTP_ALLOW`) and the base ruleset accepts UDP only on `usb*`.

## 2. Token

- Generated on the phone, 256 bit (`secrets.token_urlsafe(32)`), in
  `/etc/prius/backend-auth.env` (root 0600, one line `BACKEND_AUTH_TOKEN=...`). `provision.sh`
  creates it when the file is absent and never overwrites it; an empty value turns auth off.
- `/usr/local/sbin/prius-backend` sources that file and passes the token to Python through the
  environment only (not argv: argv is world-readable in `/proc` and the launcher echoes its command
  line into the journal). The backend removes it from its environment at start, so the processes it
  spawns do not inherit it.
- Operator copy: dotinfra vault key `prius_backend_auth_token`. Use it without printing:
  `dotinfra vault exec prius_backend_auth_token -- sh -c 'read -r T; python3 scripts/diag_cli.py --url http://10.200.0.14:8080 --token "$T" report'`
  (the token is then on that laptop process's argv for the run; `PRIUS_API_TOKEN=$T` avoids even that).

**Rotate** (old token stops working at the restart; browser sessions are separate credentials and
stay valid unless revoked):

```sh
ssh user@10.200.0.14 'sudo prius-auth token new --stdout' | dotinfra vault set prius_backend_auth_token
ssh user@10.200.0.14 'sudo systemctl restart prius-backend'
ssh user@10.200.0.14 'sudo prius-auth revoke-all'      # if the old token or a device leaked
```

(with the sudo password from the vault: `dotinfra vault exec poco_user_password -- ssh ... "sudo -S -p '' ..."`;
a backend restart with ACC off is harmless; with ACC on it re-adopts roadsense and the MFD.)

## 3. Browser sessions

- `POST /login` with the token (form on `/login`) or a redeemed pairing code creates a session:
  a random 256-bit id in the cookie, only its SHA-256 on disk
  (`/var/lib/prius/auth/sessions.json`, root 0600, dir 0700; `BACKEND_AUTH_STATE_DIR`).
  Sessions survive backend restarts; at most 64 are kept (oldest dropped); each lasts 365 days.
- Cookie: `prius_session=<id>; HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000`. **Not `Secure`**:
  the API is plain HTTP, and only reachable over WireGuard (encrypted), the WPA2 car AP and the USB
  cable (section 1). Anyone who can sniff the AP after joining it with the PSK could copy a cookie;
  WireGuard traffic cannot be sniffed. The cookie is per host name: a browser paired at
  `10.200.0.14` is not logged in at `10.42.0.1` (pair or log in once per address).
- WebSockets (`/api/v1/stream`, `/api/v1/roadsense/ws`, `/roadsense/ws`) honour the cookie; the
  dashboard is same-origin, so its `fetch`/`WebSocket`/`<img>` requests carry it with no code change.
  When its stream drops and `/auth/status` says the session is gone, it goes to the login page.
- Logout: `/logout` (button page) or `POST /logout`: revokes that session server-side.
- Revoke: `sudo prius-auth sessions` (list: id prefix, how created, user agent),
  `sudo prius-auth revoke <id-prefix>`, `sudo prius-auth revoke-all`, or
  `POST /auth/revoke-all` with any credential. The running backend re-reads the file within 1 s.
- Failed logins: 1 s delay each; after 10 failures in 60 s, `/login` answers 429 for a minute.

## 4. Pairing a device (Android phone)

On the car computer, as root:

```sh
sudo prius-auth link                      # host = WireGuard address if up, else the AP address
sudo prius-auth link --host 10.42.0.1     # for a device on the car AP
sudo prius-auth link --ttl 1h             # validity (default 15m, max 24h)
prius-auth hosts                          # addresses a link may use
```

It prints `http://<host>:8080/auth/pair?code=<code>`: a 128-bit, single-use, short-lived code
(never the token). On disk only `pair/<sha256(code)>` with the expiry (root 0600). Open the link on
the device (it must reach that address: WireGuard app on, or joined to the `prius` AP); the page asks
to confirm with one tap (a GET does not consume the code, so chat-app link previews cannot burn it);
the confirmation sets the session cookie and opens the dashboard. Reused or expired: "This link has
expired or was already used".

**Home screen:** Chrome menu, "Add to Home screen". The dashboard ships a web manifest (name
"Prius computer", 192/512 px icons, standalone display). Over plain HTTP Chrome treats the origin as
insecure, so it adds a shortcut that opens in a browser tab rather than a standalone installed app
(true PWA install needs HTTPS, or `chrome://flags/#unsafely-treat-insecure-origin-as-secure` with
`http://10.200.0.14:8080` on that phone). The cookie is shared with the tab, so the shortcut is logged in.

## 5. roadsense through 8080

roadsense's API (8090) has no authentication. Browsers use the backend front instead, behind the
same session:

| Backend path | roadsense |
|---|---|
| `/roadsense/` | status page `/` (absolute links rewritten to `/roadsense/...`) |
| `/roadsense/v1/<name>`, `/api/v1/roadsense/<name>` | `GET /v1/<name>` for a whitelist (health, road, signs, signs/history, env, speed_limit, rules, lights, camera, map, fused) |
| `/roadsense/v1/frame.jpg`, `/api/v1/roadsense/frame.jpg` | `/v1/frame.jpg` |
| `/roadsense/preview`, `/api/v1/roadsense/preview` | `/preview` MJPEG, at most 2 relays at a time (429 beyond) |
| `/roadsense/ws`, `/api/v1/roadsense/ws` | `/ws` (read-only relay, topic filter sanitised) |

No POST passes through (`/v1/position` stays loopback-only). With the stack stopped (ACC off)
`/roadsense/` shows "roadsense is not running" (503).

## 6. Gaps

- ZeroMQ 8081/8082 is unauthenticated (firewall only). Closing it needs CURVE keys or a token in
  the REQ envelope plus an MFD frontend deploy.
- roadsense still binds 8090 on all addresses with its own `50_roadsense.nft`; the guard confines it.
  Cleaner: reinstall roadsense with `API_HOST=127.0.0.1` (its `install-phone.sh` then binds loopback
  and removes `50_roadsense.nft`); the backend proxy uses 127.0.0.1:8090 already. Owner's call.
- The planned head-unit USB (RNDIS) link will appear under another interface name (`enx...`) unless a
  udev rule names it `usb*` like `70-prius-mfd-usb0.rules`; until then it is firewalled.
- No TLS: cookies and the bearer token cross the AP unencrypted at the HTTP layer (WPA2 below).
