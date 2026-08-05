# Rivian EVCC Bridge — Bug Remediation Plan

Status: **proposed, not implemented.** No code changes have been made.

This plan comes out of an audit of `rivian_bridge/`, the EVCC config snippets, and
the deployment files, checked against EVCC source and the unofficial Rivian API
documentation. Findings keep their original numbering; #23–#26 were added during
verification.

---

## What was verified against sources

Everything below was checked rather than inferred. Sources are listed at the end.

### EVCC (read from `evcc-io/evcc` source)

| Claim | Verdict |
|---|---|
| `maxcurrent` is an **IntSetter** (write-only); `enabled` is the BoolGetter, `enable` the BoolSetter, `status` a StringGetter parsed by `api.ChargeStatusString` | Confirmed — the repo's claim that "maxcurrent handles both reading and setting" is **false** (#20) |
| EVCC **never calls `MaxCurrent(0)`** — `setLimit` guards on `current >= effMinCurrent` and routes disable through `Enable(false)` | Confirmed — resolves an open question (#16) |
| If `Enabled()` returns an error, the loadpoint update aborts for that cycle | Confirmed — failing loudly is the correct, safe behavior (#13) |
| If the charger reports `enabled == false` while `lp.charging()`, EVCC logs **"charger logic error: disabled but charging"** and force-corrects | Confirmed — this is exactly the state the parked window produces (#2) |
| Otherwise EVCC **adopts** the charger's reported state via `setAndPublishEnabled` | Confirmed — so a spurious `false` from a failed API call really does flip EVCC's state (#13) |
| Status `A` = not connected, `B` = connected not charging, `C` = charging | Confirmed |

### Rivian (unofficial API docs + community reports)

| Claim | Verdict |
|---|---|
| `InputChargingSchedule` = `{weekDays, startTime, duration, location{latitude,longitude}, amperage, enabled}`; `setChargingSchedules` returns `{success}` | Confirmed — the bridge never reads `success` (#15) |
| **There is no reachable start/stop charging command for this bridge** | Confirmed with a nuance — see #26 |
| **Schedules are permissive windows.** Schedule on = charging restricted to the window; toggle off = the car charges immediately on plug-in | Confirmed by owner reports of exactly that behavior |
| **Schedules are geofenced** to the schedule's `location`, and the match is fragile — owners report GPS off by ~50 ft causing the car to ignore the schedule and charge immediately | Confirmed — this is a new P0, see #24/#25 |
| `distanceToEmpty` is in **kilometers**, not miles | Confirmed — see #11 |

---

## Design principle (governs the whole plan)

For this bridge, the charging schedule is the only available lever, and a
schedule **permits** charging rather than blocking it: the car charges when the
current time falls inside an enabled window whose location matches where the car
is parked.

| EVCC operation | Schedule edit | Status |
|---|---|---|
| Set rate (solar surplus) | 24h window (`startTime: 0`, `duration: 1440`) at the requested amperage | Correct — keep |
| Disable | A window placed so that "now" falls outside it | Right idea, broken maintenance |

> **Do not "fix" the disable path by sending `enabled: false`.** That clears the
> restriction, and the car charges freely on plug-in — the opposite of a disable.
> An earlier draft of this plan recommended exactly that; it was wrong, and owner
> reports of the app's toggle behaving this way confirm it.

Phase 1 is therefore **not** a mechanism change. The mechanism is right. What is
broken is that the parked window is left where it will eventually arrive, at the
highest amperage the charger supports — and that the window may not apply at all
(#24).

---

## Phase 1 — Charging control correctness (P0)

### 1. The parked "disable" window eventually arrives, at 48A
`rivian_api.py:265-286`

`set_charge_amps(0)` writes `"enabled": True` with `"amperage": AMPS_MAX` at a
time that isn't now, then leaves it while time advances toward it.

| EVCC disables at | Window written | Outcome |
|---|---|---|
| 10:00 | 18:00 today | noon shifter relocates to 06:00 → charges 06:00 tomorrow at 48A |
| 12:30 | 06:00 tomorrow | shifter already ran today; next run 12:00 tomorrow → charges 06:00 tomorrow at 48A |

Any overnight "disabled" state ends in an unattended 48A hour. The noon shifter
cannot close this — it always runs *after* the 06:00 window it targets.

**Fix — two independent changes:**

1. **Keep the window far from now.** Compute it as `now + 12h (mod 24h)` and
   re-apply on a cadence well under that margin (#3), so it never approaches
   firing while the bridge runs.
2. **Park at `AMPS_MIN`, not `AMPS_MAX`.** A window that should never fire has no
   business being set to the charger's maximum. If the bridge dies while
   disabled, the worst case becomes one hour at 8A rather than 48A. One line,
   no design questions attached.

### 24. The disable may not apply at all — empty `location`
`rivian_api.py:278-286`, `rivian_api.py:238-244`

Schedules are geofenced. `set_charge_amps` hardcodes `"location": {}`, and
`_apply_schedule` only overwrites it when `get_schedules()` returns a non-empty
list. So on a fresh account, after a failed schedule read, or any time the list
comes back empty, the bridge submits a schedule with **no location** — which the
car is unlikely to match against, leaving the "disable" with no effect while the
bridge reports `enabled: false`.

This is the same end state as #1 but reachable immediately rather than overnight.

**Fix:** obtain a real location and never submit an empty one. Options, in order
of preference: read it from the existing schedule (current behavior when it
works); fall back to a `charge-location` lat/long in `config.json`; refuse to
write and surface an error rather than submitting a geofence-less schedule.
Treat "no location available" as a hard failure of the disable path, not a
default.

### 25. The disable is best-effort by construction
`rivian_api.py` (design-level)

Even with a correct location, owners report the car ignoring its schedule when
GPS drifts ~50 ft, and charging immediately. The bridge cannot guarantee the car
will not charge; it can only make it unlikely.

**Fix:** this one is documentation and defense in depth, not code.
- The `AMPS_MIN` parking in #1 is what caps the cost when the geofence fails.
- Say plainly in the README that EVCC "off" is not a hard stop, and that anyone
  relying on this to avoid peak rates should also set a Rivian-side charge limit.
- Consider a guard: if the bridge believes charging is disabled but `/status`
  reports `C` across consecutive polls, log an error and re-assert the schedule.

### 3. Noon shifter runs on the wrong cadence
`server.py:84-109`

The intent is right and the thread should stay — something has to keep re-parking
the window. The defect is that it runs once per day at noon, structurally too
late for a 06:00 window, and only ever picks between two fixed slots.

**Fix:** replace the once-daily trigger with a periodic re-park every 30–60 min,
well inside the 12h margin. Drop the `last_shift_date` day-latch and the
hardcoded 18:00/06:00 pair; recompute `now + 12h` each cycle and re-apply when
the stored window has drifted closer. Confirm `_apply_schedule`'s unchanged-check
actually suppresses redundant writes (#27) or this becomes a write every cycle.
Rename to `_disable_window_keepalive`.

### 4. Schedule writes race with EVCC
`server.py:98-106` vs `161-190`

Worse under the corrected design: a keepalive firing every 30–60 min overlaps
EVCC traffic far more often than a daily job, and a re-park interleaved with
`POST /charger/current` can silently revert EVCC's enable.

**Fix:** a dedicated `_schedule_lock` held across the whole
`get_schedules()` → compare → write sequence in `_apply_schedule`, and across the
keepalive's read-then-write. Do not reuse `_refresh_lock`. The keepalive must
re-read state inside the lock rather than acting on a value read before it.

### 2. Reported state contradicts reality if the window fires
`server.py:148-158`, `rivian_api.py:227-236`

If the parked window arrives (or the geofence fails, #24/#25), the car charges
while `get_current_amps()` returns `0` — because it only counts
`duration == 1440` — so `/charger/enabled` says `false` and `/charger/status`
says `C`. Verified against EVCC source, this triggers
`"charger logic error: disabled but charging"` and a force-correct.

#1 makes this rarer, not impossible, so it needs its own fix.

**Fix:** derive `enabled` from whether an enabled window whose location matches
actually covers the current time — the honest question for a permissive-window
API — rather than from `duration == 1440`. Answers correctly for the 24h window
and a parked one alike.

### 23. `enabled: false` on a schedule is read as "not charging"
`rivian_api.py:234`

`get_current_amps()` requires `s.get('enabled', False)` and returns `0`
otherwise. Under the confirmed permissive model a disabled schedule imposes no
restriction, so the car is free to charge — the bridge reports `enabled: false`
while the car charges at full rate. Reachable today by toggling the schedule off
in the Rivian app.

**Fix:** covered by the #2 rewrite, but test the inverted case explicitly and log
a warning when the bridge finds its own schedule disabled underneath it.

### 14. Schedule writes replace the entire schedule list
`rivian_api.py:247-252`

`chargingSchedules: [schedule]` deletes any other schedule the owner set in the
Rivian app, on the first EVCC current change. `get_current_amps()` likewise only
inspects `schedules[0]`.

**Fix:** identify the bridge's own schedule by a stable marker and preserve the
rest. Log when a pre-existing schedule is found.

---

## Phase 2 — Crashes and availability (P1)

### 5. GraphQL errors return HTTP 200 with `data: null`
`rivian_api.py:96, 111, 171, 225`

The `status_code != 200` guards miss this shape. Reproduced each:

- `:171` `refresh()` → `AttributeError: 'NoneType' object has no attribute 'get'`
- `:225` `get_schedules()` → `TypeError: 'NoneType' object is not subscriptable`
- `:111` `init_user_info()` → `TypeError` (its `except (KeyError, IndexError)` does not cover it)
- `:96` `init_session()` → `TypeError`

`rivian.refresh()` at `server.py:56` runs at import with no `try`, so any of these
kills the process at startup.

**Fix:** one `_gql(operation, variables, query)` helper owning timeout, status
check, `errors` check, `data is not None` check, raising a typed
`RivianAPIError`. Route all call sites through it. Wrap the startup refresh so
the bridge boots degraded (serving 503s) rather than dying.

### 6. MFA login raises instead of directing to the OTP script
`rivian_api.py:96`, `RivianSessionInitOTP.py:75`

The query handles `MobileMFALoginResponse { otpToken }` but the code
unconditionally reads `userSessionToken` → `KeyError` for any MFA account,
escaping `login()` and `__init__` and making the intended "run
RivianSessionInitOTP.py" message at `:132` unreachable. Mirrored in the OTP
script, which assumes MFA is always required.

**Fix:** branch on `__typename` in both files.

### 7. No HTTP timeouts anywhere
`rivian_api.py:56, 60`

No `timeout=` on any `requests.post`. A hung connection stalls the refresh thread
indefinitely *while holding `_refresh_lock`* and pins Flask threads.

**Fix:** `(connect, read)` timeout on a module-level `requests.Session`.

### 8. Session tokens never re-authenticate; stale data served forever
`rivian_api.py:118-132, 147-185`

`login()` runs once in the constructor. On expiry `refresh()` logs and returns
forever, never clearing `charging_status` / `battery_level`, so EVCC keeps
receiving stale-but-plausible values.

**Fix:** detect auth failure, re-login once, retry. Stamp cached fields with
fetch time and treat data older than `N × cache_ttl` as absent.

### 9. Unknown state is served as "unplugged"
`rivian_api.py:190-199`, `server.py:127, 142`

`get_evcc_status()` falls through to `'A'` when `charging_status is None`; EVCC
reads A as disconnected and ends the session. `/soc` already 503s correctly.

**Fix:** return `None` for unknown and 503 from both status endpoints.

### 13. API failure is indistinguishable from "disabled"
`rivian_api.py:230`

`get_schedules()` returns `None` on error → `get_current_amps()` returns `0` →
EVCC adopts "disabled" (verified: `setAndPublishEnabled`) and re-enables next
cycle. Transient errors become schedule writes.

**Fix:** propagate the error and 503. Verified as safe: EVCC aborts that
loadpoint cycle on an `Enabled()` error rather than thrashing.

### 10. Client-name mismatch between the OTP script and the bridge
`RivianSessionInitOTP.py:32` vs `rivian_api.py:49`

Session minted as `com.rivian.ios.consumer-apollo-ios`, used as
`com.rivian.android.consumer`.

**Fix:** one shared constant for client name and User-Agent.

---

## Phase 3 — Data correctness (P2)

### 11. Range is double-converted — confirmed
`rivian_api.py:179-182`

The comment claims miles and multiplies by 1.60934. The Rivian API is metric:

- Documented sample: `distanceToEmpty: 307` with `batteryLevel: 59.4` → full-scale
  517. As **km** that is 517 km / 321 mi, right for an R1T. As **miles** it would
  be 517 mi / 832 km, well beyond any Rivian.
- `vehicleMileage: 14517537` is meters (14,518 km).
- The live-charging session field is `kilometersChargedPerHour`.

EVCC also works in km internally, so the value needs no conversion at all.

**Fix:** drop the multiplication, fix the comment. Still worth one glance at the
in-car display to confirm before shipping.

### 12. Every EVCC poll makes live Rivian API calls
`rivian_api.py:227-236`, `server.py:195-206`

Vehicle state is cached; schedules are not. `/charger/enabled` and
`/charger/current` each hit the API, `_apply_schedule` fetches again before
writing, `/health` calls `get_current_amps()` twice.

**Fix:** cache the schedule alongside vehicle state, invalidated on every
successful write. `/health` computes once.

### 15. Write success is never checked
`rivian_api.py:260-263`

HTTP 200 is logged as success without reading `setChargingSchedules.success` —
which the API does return — or `errors`.

**Fix:** check `success`; raise otherwise. Subsumed by `_gql` (#5).

### 27. Unchanged-schedule check is unreliable
`rivian_api.py:238-244`

`schedule == schedules[0]` compares a locally built dict against the API's
response. Any difference in `weekDays` ordering, numeric type, or an extra
returned field makes it always-unequal, turning the keepalive (#3) into a write
every cycle.

**Fix:** compare field-by-field on a normalized view (sorted `weekDays`, ints
coerced) rather than whole-dict equality.

### 16. `int(float(body))` isn't fully guarded
`server.py:184`

`"inf"` raises `OverflowError`, missed by `except ValueError` → 500.

**Fix:** catch `(ValueError, OverflowError)`, validate against
`AMPS_MIN..AMPS_MAX`, 400 on out-of-range. Note EVCC never sends 0 here
(verified), so a 0 body indicates a misconfiguration and should 400 rather than
being silently treated as a disable.

### 17. Silent current clamping
`rivian_api.py:288`

EVCC asking for 6A gets 8A with no feedback.

**Fix:** log at WARNING on clamp.

### 18. First vehicle wins
`rivian_api.py:111`

`vehicles[0]['id']` with no VIN config.

**Fix:** optional `vehicle-vin` in config; error clearly when ambiguous.

---

## Phase 4 — Config, packaging, deployment (P2)

### 19. Port is 7070 in three places and 7071 in six

7070: root `config-example.json`, `docker-compose.yml`, `install-mac.sh:96`.
7071: `rivian_bridge/config-example.json`, README, all three EVCC YAML snippets.
Under Docker this is a hard failure — the container listens on 7071 while compose
publishes `7070:7070`.

**Fix:** standardize on **7071**; delete the duplicate root `config-example.json`.

### 20. Docs claim `maxcurrent` is readable — confirmed wrong
`evcc-ui-charger.yaml:19`, `evcc-vehicle-config.yaml:31`, `README.md:152`

Verified against EVCC source: `maxcurrent` is an IntSetter. `GET /charger/current`
is never called by EVCC.

**Fix:** correct the comments; keep the GET endpoint as debugging-only. The
surrounding advice ("do not use `setcurrent`", "no `title:` in the UI body") is
correct and should stay.

### 26. START_CHARGING / STOP_CHARGING exist but are out of reach
`rivian_api.py` (design-level)

Worth recording so it is not rediscovered: `rivian-python-client` defines
`START_CHARGING` and `STOP_CHARGING` under `sendVehicleCommand`. They are **not**
usable here — that path requires HMAC signing with an enrolled phone key
(`vasPhoneId`, `deviceId`, private key), a different auth model than the session
tokens this bridge holds. They are also one-shot commands, not a persistent
state, so even with enrollment the schedule would remain the right tool for a
durable disable.

**Fix:** none. Document as a rejected alternative.

### 21. Unauthenticated control on `0.0.0.0`
`server.py:215`, `docker-compose.yml`

Anyone on the LAN can control the car's charging; `/health` discloses vehicle
state; credentials sit in plaintext `config.json`.

**Fix:** default the bind to `127.0.0.1`, configurable for Docker. Document the
exposure.

### 22. Module-level threads under a real WSGI server
`server.py:56-109`

Bootstrap runs at import, so multi-worker deployments get N keepalives racing on
one schedule — worse now that the keepalive is frequent.

**Fix:** guard bootstrap for single-worker use; document `--workers 1`.

---

## Remaining open questions

Most of the original list is now answered. What is left needs the car, not docs:

1. **Is there an upper bound on how far ahead a window can be parked?** #1 assumes
   `now + 12h` is freely settable. If Rivian normalizes or rejects some offsets,
   the keepalive cadence in #3 must tighten.
2. **What does the car report while plugged in with no window covering now** —
   `B`, or `C` at zero power? Determines what #2's test asserts.
3. **Does a schedule with an empty `location` get rejected, ignored, or applied
   globally?** (#24) Changes whether the fix is "fall back to configured
   lat/long" or "hard-fail the write".
4. **Should the bridge preserve the owner's other schedules (#14),** or is
   "the bridge owns the schedule list" acceptable if documented?

---

## Suggested sequencing

0. **#1 part 2 alone** — park the disable window at `AMPS_MIN`. One line, no open
   questions, caps the blast radius of every other bug here at 8A. Land now.
1. **#24 + #25** — a disable that silently does nothing is worse than one that
   fires late. Ranks with #1.
2. **Rest of Phase 1.**
3. **Phase 2** — `_gql` (#5) is the keystone; land it first within the phase.
4. **#19** — one line per file, unblocks Docker users; pull forward any time.
5. **Phase 3**, then the rest of **Phase 4**.

## Testing

- Unit tests with a mocked GraphQL transport: `data: null` + `errors`, HTTP 500,
  timeout, MFA login response, empty vehicle list. Each currently crashes or
  misreports.
- State-consistency test: `status == C` implies `enabled == true`, plus the
  inverted #23 case.
- Clock-driven keepalive test (#3): advance time across many cycles while
  disabled and assert the parked window never comes within a threshold of now,
  including across a midnight wrap.
- Normalization test for #27: an API response whose `weekDays` are reordered must
  compare equal and produce no write.
- On the real vehicle before declaring #1/#24 fixed: disable via EVCC, confirm in
  the Rivian app that the window is ~12h out at 8A **with a real location**, and
  confirm across the following morning that no charge occurs.

## Sources

- EVCC custom charger plugin keys and setter/getter types — `evcc-io/evcc`, `charger/charger.go`
- EVCC `setLimit` / `syncCharger` behavior — `evcc-io/evcc`, `core/loadpoint.go`
- EVCC charge status A/B/C — `evcc-io/evcc`, `api` package
- Rivian charging schedule schema and `success` response — RivDocs, `app/legacy/charging/charging-schedule`
- Rivian `GetVehicleState` sample values (`distanceToEmpty`, `vehicleMileage`, `chargerStatus`) — RivDocs, `app/legacy/vehicle-state`
- Rivian `sendVehicleCommand` command list and HMAC/phone-key requirement — RivDocs, `app/legacy/controls/send-vehicle-command`
- `START_CHARGING` / `STOP_CHARGING` command names — `bretterer/rivian-python-client`, `src/rivian/const.py`
- Schedule geofencing and toggle-off behavior — Rivian owner forum reports
