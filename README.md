# Rivian EVCC Bridge

Connects a Rivian vehicle to [EVCC](https://evcc.io) when using a **Tesla Wall
Connector** (or any charger that EVCC cannot directly control for current
adjustment).

Because the Tesla Wall Connector has no third-party current-control API, EVCC
adjusts the Rivian's charging rate by updating the **Rivian charging-schedule
API** directly. The bridge therefore acts as both the EVCC **vehicle plugin**
(SoC / status / range) and the EVCC **charger plugin** (enable/disable /
set amps).

> **Warning:** Provided as-is, without guarantees. Rivian's API is unofficial
> and may change without notice.

> **AI assistance:** This project was built with [Claude](https://claude.ai) (Claude Code).
> If you run into issues, Claude can help debug them — share this README and
> describe the problem.

---

## Architecture

```
Rivian Cloud API  (GraphQL — polled every 5 min)
        │
        ▼
rivian_evcc_bridge  (HTTP :7071)
   │                    │
   │  /soc              │  /charger/enable
   │  /status           │  /charger/current   (POST — sets amps)
   │  /range            │  /charger/status
   │                    │  /charger/enabled
   ▼                    ▼
EVCC vehicle plugin   EVCC charger plugin
         │
         │  writes charging schedule back to Rivian API
         ▼
   Tesla Wall Connector  ──►  Rivian vehicle
```

EVCC handles all solar-surplus logic, charging modes, and the UI.
No separate automation script is needed.

---

## Quick start (macOS, EVCC installed via Homebrew)

### 1. Go to the project directory

```bash
cd ~/github/RivianEVCC
```

### 2. Set up credentials

```bash
cp rivian_bridge/config-example.json rivian_bridge/config.json
```

Edit `rivian_bridge/config.json` with your Rivian email and password:

```json
{
    "rivian-user": "your-email@example.com",
    "rivian-pass": "your-rivian-password",
    "cache-ttl-seconds": 300,
    "bridge-port": 7071
}
```

### 3. Add your Rivian session tokens

Create `rivian_bridge/rivian-session.json` with your tokens:

```json
{
    "appSessionToken": "your-app-session-token",
    "userSessionToken": "your-user-session-token",
    "csrfToken": "your-csrf-token"
}
```

Or generate a fresh session via OTP (needs `config.json` filled in first):

```bash
pip3 install requests
python3 RivianSessionInitOTP.py
# Enter the code sent to your Rivian-registered email/phone
```

### 4. Test the bridge manually first

```bash
cd ~/github/RivianEVCC/rivian_bridge
pip3 install -r requirements.txt
python3 server.py
```

In another terminal:

```bash
curl http://localhost:7071/health
```

Expect a JSON response with `soc`, `evcc_status`, `range_km`, and charger info.
Once that works, stop the server (Ctrl-C) and proceed to install it as a service.

### 5. Install as a background service

```bash
cd ~/github/RivianEVCC
./install-mac.sh
```

This installs a LaunchAgent that starts the bridge at login and restarts it
automatically. Logs go to `~/Library/Logs/rivian-evcc-bridge/`.

### 6. Configure EVCC

Newer versions of EVCC store device configuration in a SQLite database
(`~/.evcc/evcc.db`) managed through the EVCC UI, rather than directly in
`evcc.yaml`. Use whichever method matches your setup.

#### Option A — EVCC UI (recommended for recent versions)

**Add the charger** (Devices → Chargers → Add → Custom):

```yaml
status:
  source: http
  uri: http://localhost:7071/charger/status

enabled:
  source: http
  uri: http://localhost:7071/charger/enabled

maxcurrent:
  source: http
  uri: http://localhost:7071/charger/current
  method: POST
  body: '{{.maxcurrent}}'

enable:
  source: http
  uri: http://localhost:7071/charger/enable
  method: POST
  body: '{{.enable}}'
```

> **Note:** Do not include a `title:` key or a `chargers:` wrapper — the UI
> adds those itself. `setcurrent` is not a valid key; `maxcurrent` handles
> both reading and setting the current.

**Add the vehicle** (Devices → Vehicles → Add → Custom):

```yaml
title: Rivian
icon: car
capacity: 140 # kWh

soc:
  source: http
  uri: http://localhost:7071/soc

status:
  source: http
  uri: http://localhost:7071/status

range:
  source: http
  uri: http://localhost:7071/range
```

> **Note:** Do not include a `vehicles:` wrapper — paste only the body above.

**Add a loadpoint** (Devices → Loadpoints → Add), select the Rivian Bridge
charger and Rivian vehicle, and set:
- Min current: `8`
- Max current: `48`

#### Option B — evcc.yaml (older / manual installs)

Add the following to your `evcc.yaml`:

```yaml
chargers:
  - name: rivian-bridge
    type: custom
    status:
      source: http
      uri: http://localhost:7071/charger/status
    enabled:
      source: http
      uri: http://localhost:7071/charger/enabled
    maxcurrent:
      source: http
      uri: http://localhost:7071/charger/current
      method: POST
      body: '{{.maxcurrent}}'
    enable:
      source: http
      uri: http://localhost:7071/charger/enable
      method: POST
      body: '{{.enable}}'

vehicles:
  - name: rivian
    type: custom
    title: Rivian
    icon: car
    capacity: 140
    soc:
      source: http
      uri: http://localhost:7071/soc
    status:
      source: http
      uri: http://localhost:7071/status
    range:
      source: http
      uri: http://localhost:7071/range

loadpoints:
  - title: Garage
    charger: rivian-bridge
    vehicle: rivian
    mincurrent: 8
    maxcurrent: 48
```

Then restart EVCC:

```bash
brew services restart evcc
```

---

## How current control works

EVCC calls `POST /charger/current` with an integer amps value whenever it
wants to adjust the charging rate (e.g. to use excess solar). The bridge
translates this into a Rivian charging-schedule update via GraphQL:

| EVCC command             | Rivian schedule result                              |
|--------------------------|-----------------------------------------------------|
| `maxcurrent` 16A         | 24-hour all-day schedule at 16A                     |
| `maxcurrent` 0 / disable | 1-hour off-peak window (never covers current time)  |

The charger supports **8A – 48A** in any increment. EVCC will respect whatever
`mincurrent`/`maxcurrent` you set in the loadpoint.

## EVCC status codes

| Rivian charger state                  | Bridge returns | Meaning           |
|---------------------------------------|----------------|-------------------|
| `chrgr_sts_connected_charging`        | C              | Charging          |
| `chrgr_sts_connected_no_chrg`         | B              | Connected, idle   |
| `chrgr_sts_connected_not_charging`    | B              | Connected, idle   |
| anything else / not connected         | A              | Not connected     |

---

## Managing the bridge service

```bash
# View logs
tail -f ~/Library/Logs/rivian-evcc-bridge/bridge.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.rivian.evcc.bridge.plist

# Start
launchctl load ~/Library/LaunchAgents/com.rivian.evcc.bridge.plist

# Remove
launchctl unload ~/Library/LaunchAgents/com.rivian.evcc.bridge.plist
rm ~/Library/LaunchAgents/com.rivian.evcc.bridge.plist
```

## Running manually (no LaunchAgent)

```bash
cd ~/github/RivianEVCC/rivian_bridge
pip3 install -r requirements.txt
python3 server.py
```

---

## Running the tests

The test suite is fully offline — it stubs the Rivian GraphQL gateway and
refuses any real outbound request, so it never contacts Rivian and never
touches a vehicle.

```bash
cd ~/github/RivianEVCC
python3 -m venv .venv && source .venv/bin/activate
pip install -r rivian_bridge/requirements.txt -r requirements-dev.txt
pytest
```

Tests carry markers so you can target them:

```bash
pytest -m charging   # anything that touches charging control
pytest -m defect     # tests pinning known-wrong behaviour
```

Most tests assert the **current** behaviour on purpose. Those marked `defect`
document a known bug and are expected to fail when that bug is fixed — update
them deliberately as part of the fix rather than adjusting them to pass.

---

## Troubleshooting

### EVCC fails to start: `cannot create circuit: missing name`

This can happen if the EVCC UI previously saved a loadpoint with a `circuit`
reference into the `settings` table. Check for it and remove it:

```bash
sqlite3 ~/.evcc/evcc.db "SELECT * FROM settings WHERE key='circuits';"
sqlite3 ~/.evcc/evcc.db "DELETE FROM settings WHERE key='circuits';"
```

Also check for DB loadpoints with an empty `circuit` field:

```bash
sqlite3 ~/.evcc/evcc.db "SELECT id, value FROM configs WHERE value LIKE '%circuit%';"
# If any rows have "circuit":"", remove the key:
sqlite3 ~/.evcc/evcc.db "UPDATE configs SET value = json_remove(value, '$.circuit') WHERE id=<id>;"
```

### EVCC UI charger config rejected: `invalid keys: setcurrent, title`

- Do not use `setcurrent` — use `maxcurrent` with a `method: POST` and
  `body: '{{.maxcurrent}}'` to handle both reading and setting.
- Do not include `title:` in the charger body — set the name in the UI field.

### EVCC UI vehicle config rejected: `invalid keys: vehicles`

Do not paste the `vehicles:` wrapper. Paste only the vehicle body directly
into the UI text area.

---

## Files

```
~/github/RivianEVCC/
├── rivian_bridge/
│   ├── server.py              HTTP bridge (Flask) — vehicle + charger endpoints
│   ├── rivian_api.py          Rivian GraphQL client (read + schedule control)
│   ├── config.py              Config loader
│   ├── config-example.json    → copy to config.json and fill in
│   ├── rivian-session.json    (generated — gitignored)
│   └── requirements.txt
├── tests/                     Offline regression suite (see "Running the tests")
├── RivianSessionInitOTP.py    One-time 2FA auth (run from project root)
├── install-mac.sh             macOS LaunchAgent installer
├── evcc-vehicle-config.yaml   evcc.yaml snippet for YAML-based installs
├── evcc-ui-charger.yaml       Charger config snippet for EVCC UI
├── evcc-ui-vehicle.yaml       Vehicle config snippet for EVCC UI
├── requirements-dev.txt       Test-only dependencies
├── pyproject.toml             pytest configuration
├── Dockerfile                 For Docker-based deployment
├── .dockerignore              Keeps credentials out of image layers
└── docker-compose.yml
```

---

## Credits

- [rivian-charging-automation](https://github.com/ostap-korkuna/rivian-charging-automation) by ostap-korkuna — Rivian API implementation this is based on
- [kaedenbrinkman/rivian-api](https://github.com/kaedenbrinkman/rivian-api) — Rivian API documentation
- [evcc-io/evcc](https://github.com/evcc-io/evcc) — solar charging controller
