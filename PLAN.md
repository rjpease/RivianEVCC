# Rivian EVCC Bridge — Bug Remediation Plan

Status: **proposed, not implemented.** No code changes have been made.

This plan comes out of an audit of `rivian_bridge/`, the EVCC config snippets, and
the deployment files. Findings keep the numbering used in the audit so they can be
cross-referenced.

---

## Design principle (governs the whole plan)

The Rivian API supports **enable/disable as a first-class operation**. Charging
schedules exist to manage the **charging rate**, which is what lets EVCC track
surplus solar.

These are two separate concerns and the current code conflates them:

| Concern | Correct mechanism | What the code does today |
|---|---|---|
| Enable / disable charging | The API's real disable | Writes an *enabled* 48A schedule at a time that isn't "now" |
| Charge rate (solar surplus) | Schedule `amperage` on a 24h window | Correct — keep this |

Every P0 below follows from separating them. Rate control via a 24-hour schedule
stays exactly as it is; only the disable path changes.

**Verify before implementing:** confirm which disable the account/firmware
actually honors — schedule `enabled: false`, or a dedicated start/stop charging
mutation. Do not guess. Capture the working request/response in the PR
description so the next person doesn't have to rediscover it.

---

## Phase 1 — Charging control correctness (P0)

The bug the last two commits were chasing. Ship this phase alone if nothing else
ships.

### 1. "Disabled" is actually a scheduled 48A charge
`rivian_api.py:265-286`

`set_charge_amps(0)` writes `"enabled": True` with `"amperage": AMPS_MAX`, moved to
a time that isn't now. The car really does charge at 48A when the window arrives.

| EVCC disables at | Window written | Outcome |
|---|---|---|
| 10:00 | 18:00 today | noon shifter relocates to 06:00 → charges 06:00 tomorrow at 48A |
| 12:30 | 06:00 tomorrow | shifter already ran today; next run 12:00 tomorrow → charges 06:00 tomorrow at 48A |

Any overnight "disabled" state ends in an unattended 48A hour. The noon shifter
cannot close this — it always runs after the 6AM window it is meant to prevent.

**Fix:** `enable_charging(False)` calls the real disable. Delete the
`amps == 0` branch of `set_charge_amps` entirely; `set_charge_amps` becomes
rate-only and rejects 0 rather than reinterpreting it as a disable.

**Verification:** disable via EVCC, confirm in the Rivian app that no enabled
schedule remains, and leave it overnight to confirm no 6AM charge.

### 3. Noon shifter is removed by the above
`server.py:84-109`

It exists only to relocate the fake disable window. With a real disable there is
nothing to shift. Removing it also removes the read-then-write race in #4.

**Fix:** delete `_noon_window_shifter` and its thread. Note the removal in the
README so the behavior change is visible to existing users.

### 4. Schedule writes race with EVCC
`server.py:98-106` vs `161-190`

The shifter holds `_refresh_lock` around read-then-write; no Flask handler takes
that lock, and Flask serves requests threaded. A `POST /charger/current` landing
at 12:00:xx can be clobbered, or clobber, depending on interleaving.

**Fix:** mostly resolved by deleting the shifter. Keep the residual protection:
a dedicated `_schedule_lock` around every read-modify-write of the schedule, held
across the `get_schedules()` → compare → write sequence in `_apply_schedule`.
Do not reuse `_refresh_lock` — vehicle-state refresh and schedule writes should
not block each other.

### 2. Reported state contradicts reality during the disable window
`server.py:148-158`, `rivian_api.py:227-236`

While the fake window is live the car is charging, but `get_current_amps()` only
counts `duration == 1440`, so `/charger/enabled` reports `false` and
`/charger/current` reports `0` while `/charger/status` reports `C`. EVCC reads
that as out-of-sync and fights it.

**Fix:** falls out of #1 — once disable is real, "charging" and "enabled" can no
longer disagree. Add a regression test asserting `status == C` implies
`enabled == true`.

### 14. Schedule writes replace the entire schedule list
`rivian_api.py:247-252`

`chargingSchedules: [schedule]` deletes any other schedule the owner set in the
Rivian app, on the first EVCC current change. `get_current_amps()` likewise only
ever inspects `schedules[0]`.

**Fix:** identify the bridge's own schedule (a stable marker — dedicated
`startTime`/`duration` signature, or persisted index) and preserve the others in
the submitted list. Log loudly when a pre-existing schedule is found so users
learn the bridge is now sharing the list.

---

## Phase 2 — Crashes and availability (P1)

### 5. GraphQL errors return HTTP 200 with `data: null`
`rivian_api.py:96, 111, 171, 225`

The `status_code != 200` guards miss this shape entirely. Confirmed failure mode
for each site:

- `:171` `refresh()` → `AttributeError: 'NoneType' object has no attribute 'get'`
- `:225` `get_schedules()` → `TypeError: 'NoneType' object is not subscriptable`
- `:111` `init_user_info()` → `TypeError` (the `except (KeyError, IndexError)` does not cover it)
- `:96` `init_session()` → `TypeError`

Because `rivian.refresh()` at `server.py:56` runs at import with no `try`, any of
these kills the process at startup instead of logging.

**Fix:** one `_gql(operation, variables, query)` helper that owns the whole
contract — timeout, status check, `errors` check, `data is not None` check — and
raises a typed `RivianAPIError`. Route all five call sites through it. Wrap the
startup refresh so the bridge boots degraded (serving 503s) instead of dying.

### 6. MFA login raises instead of directing to the OTP script
`rivian_api.py:96`, `RivianSessionInitOTP.py:75`

The query handles `MobileMFALoginResponse { otpToken }` but the code
unconditionally reads `userSessionToken` → `KeyError` for any MFA-enabled
account. The intended `'Run RivianSessionInitOTP.py'` message at `:132` is
unreachable — the exception escapes `login()` and `__init__`. Mirrored in the OTP
script, which assumes MFA is always required and breaks on the non-MFA path.

**Fix:** branch on `__typename` in both files. On `MobileMFALoginResponse`, log
the actionable message and leave the bridge unauthenticated-but-alive.

### 7. No HTTP timeouts anywhere
`rivian_api.py:56, 60`

No `timeout=` on any `requests.post`. A hung connection stalls the refresh thread
indefinitely *while it holds `_refresh_lock`*, and pins Flask threads on the
charger endpoints.

**Fix:** `(connect, read)` timeout on a module-level `requests.Session` (which
also gets connection reuse). Default ~(5, 15)s, configurable.

### 8. Session tokens never re-authenticate; stale data served forever
`rivian_api.py:118-132, 147-185`

`login()` runs once in the constructor. On expiry `refresh()` logs and returns
forever, and never clears `charging_status` / `battery_level` — so EVCC keeps
receiving stale-but-plausible SoC and status indefinitely.

**Fix:** detect auth failure, re-run `login()` once, retry the call. Separately,
stamp every cached field with its fetch time and treat data older than
`N × cache_ttl` as absent.

### 9. Unknown state is served as "unplugged"
`rivian_api.py:190-199`, `server.py:127, 142`

`get_evcc_status()` falls through to `'A'` when `charging_status is None`. EVCC
reads A as disconnected and terminates the session. `/soc` already 503s correctly
in this situation.

**Fix:** return `None` for unknown and have both status endpoints 503, matching
`/soc`.

### 13. API failure is indistinguishable from "disabled"
`rivian_api.py:230`

`get_schedules()` returns `None` on error → `get_current_amps()` returns `0` →
EVCC sees "disabled" and tries to re-enable. Transient errors become schedule
writes.

**Fix:** propagate the error (once #5 lands, this is automatic) and 503 rather
than reporting a confident `0`.

### 10. Client-name mismatch between the OTP script and the bridge
`RivianSessionInitOTP.py:32` vs `rivian_api.py:49`

The session is minted as `com.rivian.ios.consumer-apollo-ios` and then used as
`com.rivian.android.consumer`. Rivian binds sessions to the client, so a session
created by the documented flow may be rejected by the bridge.

**Fix:** single shared constant for client name and User-Agent, used by both.

---

## Phase 3 — Data correctness (P2)

### 11. Range is probably double-converted
`rivian_api.py:179-182`

The comment claims miles and multiplies by 1.60934; Rivian's `distanceToEmpty`
appears to be kilometers already, which would make EVCC show ~1.6× the real
range.

**Fix:** verify against the in-car display before touching. Then either drop the
conversion or fix the comment — and state which was true in the commit message.

### 12. Every EVCC poll makes live Rivian API calls
`rivian_api.py:227-236`, `server.py:195-206`

Vehicle state is cached; schedules are not. `/charger/enabled` and
`/charger/current` each hit the API, `_apply_schedule` fetches again before
writing, and `/health` calls `get_current_amps()` twice. At EVCC's default
cadence that is several calls per 30s against an unofficial API, with network
latency inside EVCC's control loop.

**Fix:** cache the schedule alongside vehicle state, invalidated on every
successful write. `/health` computes once and reuses.

### 15. Write success is never checked
`rivian_api.py:260-263`

HTTP 200 is logged as "updated successfully" without reading
`data.setChargingSchedules.success` or `errors`. Failed writes report as
successes.

**Fix:** check `success`; raise otherwise. Subsumed by the `_gql` helper in #5.

### 16. `int(float(body))` isn't fully guarded
`server.py:184`

`"inf"` raises `OverflowError`, which `except ValueError` misses → 500. No range
validation before `set_charge_amps`.

**Fix:** catch `(ValueError, OverflowError)`, validate against
`AMPS_MIN..AMPS_MAX`, 400 on out-of-range.

### 17. Silent current clamping
`rivian_api.py:288`

EVCC asking for 6A gets 8A — more current than its surplus calculation intends,
with no feedback. `mincurrent: 8` mostly hides it today.

**Fix:** log at WARNING whenever a clamp occurs.

### 18. First vehicle wins
`rivian_api.py:111`

`vehicles[0]['id']` with no VIN config. Multi-vehicle accounts may control the
wrong car, and the choice can change between calls.

**Fix:** optional `vehicle-vin` in config; match on it, error clearly if absent
and more than one vehicle exists. Keep `[0]` only for the single-vehicle case.

---

## Phase 4 — Config, packaging, deployment (P2)

### 19. Port is 7070 in three places and 7071 in six

7070: root `config-example.json`, `docker-compose.yml`, `install-mac.sh:96`.
7071: `rivian_bridge/config-example.json`, README, all three EVCC YAML snippets.

Under Docker this is a hard failure — the copied example listens on 7071 while
compose publishes `7070:7070`. `install-mac.sh:22` also points users at the root
example, which is the 7070 one.

**Fix:** standardize on **7071** (six of nine references, and what the working
EVCC snippets use). Delete the duplicate root `config-example.json` so there is
one source of truth.

### 20. Docs claim `maxcurrent` is readable
`evcc-ui-charger.yaml:19`, `evcc-vehicle-config.yaml:31`, `README.md:152`

In EVCC's custom charger, `maxcurrent` is a write-only setter; `enabled` is the
read side. `GET /charger/current` is never called by EVCC.

**Fix:** correct the comments. Keep the GET endpoint, documented as
debugging-only.

### 21. Unauthenticated control on `0.0.0.0`
`server.py:215`, `docker-compose.yml`

Anyone on the LAN can `POST /charger/enable` and `/charger/current` to control
the car, and `/health` discloses vehicle state. Credentials sit in plaintext
`config.json`.

**Fix:** default the bind address to `127.0.0.1` (EVCC runs on the same host in
the documented setup), configurable for the Docker case. Document that exposing
the port means exposing unauthenticated car control.

### 22. Module-level threads under a real WSGI server
`server.py:56-109`

Login, initial refresh, and the background threads run at import, so a
multi-worker deployment gets N copies racing on the same schedule. `app.run()` is
the Flask dev server.

**Fix:** move bootstrap behind an explicit init guarded for single-worker use,
and document `--workers 1` if a production WSGI server is recommended.

---

## Open questions to resolve before coding

1. **Which disable does the API expose?** Schedule `enabled: false`, or a
   dedicated start/stop mutation? Phase 1 depends on the answer; everything else
   is independent of it.
2. **Is `distanceToEmpty` km or miles?** Needs one real-vehicle reading (#11).
3. **Does EVCC ever send `maxcurrent: 0`,** or is disable always routed through
   `enable`? Determines whether `set_charge_amps(0)` should raise or no-op.
4. **Should the bridge preserve user schedules (#14),** or is "the bridge owns
   the schedule list" acceptable if documented? Preserving is more work and more
   failure modes.

---

## Suggested sequencing

1. **Phase 1** — the actual reported problem. Small, self-contained, shippable alone.
2. **Phase 2** — hardening; the `_gql` helper (#5) is the keystone and should land first within the phase.
3. **Phase 4 #19** — the port fix is a one-line-per-file change that unblocks Docker users; can be pulled forward and shipped independently at any time.
4. **Phase 3**, then the rest of **Phase 4**.

## Testing

- Unit tests with a mocked GraphQL transport covering: `data: null` + `errors`,
  HTTP 500, timeout, MFA login response, and empty vehicle list. Every one of
  these currently crashes or silently misreports.
- A state-consistency test asserting `status == C` implies `enabled == true`.
- Manual, on the real vehicle, before declaring #1 fixed: disable via EVCC and
  confirm across the following morning that no 6AM charge occurs.
