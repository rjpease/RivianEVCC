# Rivian EVCC Bridge — Bug Remediation Plan

Status: **proposed, not implemented.** No code changes have been made.

This plan comes out of an audit of `rivian_bridge/`, the EVCC config snippets, and
the deployment files. Findings keep the numbering used in the audit so they can be
cross-referenced; #23 was added afterward.

---

## Design principle (governs the whole plan)

The Rivian API supports **enable/disable as a first-class operation**. Charging
Rivian has **no start/stop charging command**. The charging schedule is the only
lever, and a schedule is a *permissive window*: the car charges when the current
time falls inside an enabled window, at that window's amperage.

Both EVCC operations therefore map onto schedule edits, and the existing code
already uses the right primitive for each:

| EVCC operation | Schedule edit | Status |
|---|---|---|
| Set rate (solar surplus) | 24h window (`startTime: 0`, `duration: 1440`) at the requested amperage | Correct — keep |
| Disable | A window placed so that "now" falls outside it | Right idea, broken maintenance |

> **Do not "fix" the disable path by sending `enabled: false`.** Clearing the
> restriction is expected to let the car charge freely whenever it is plugged in
> — the opposite of a disable. An earlier draft of this plan recommended exactly
> that; it was wrong. See open question 1.

So Phase 1 is **not** a mechanism change. The mechanism is correct. What is
broken is that the parked window is left where it will eventually arrive, at the
highest amperage the charger supports.

---

## Phase 1 — Charging control correctness (P0)

The bug the last two commits were chasing. Ship this phase alone if nothing else
ships.

### 1. The parked "disable" window eventually arrives, at 48A
`rivian_api.py:265-286`

`set_charge_amps(0)` writes `"enabled": True` with `"amperage": AMPS_MAX` at a
time that isn't now. Placing the window away from now is the correct way to stop
charging on this API — but the window is then left alone, and time moves toward
it. When it arrives, the car really does charge at 48A for an hour.

| EVCC disables at | Window written | Outcome |
|---|---|---|
| 10:00 | 18:00 today | noon shifter relocates to 06:00 → charges 06:00 tomorrow at 48A |
| 12:30 | 06:00 tomorrow | shifter already ran today; next run 12:00 tomorrow → charges 06:00 tomorrow at 48A |

Any overnight "disabled" state ends in an unattended 48A hour. The noon shifter
cannot close this — it always runs *after* the 6AM window it is meant to prevent.

**Fix — two independent changes:**

1. **Keep the window far from now.** Compute the parked window as
   `now + 12h (mod 24h)` and re-apply it on a cadence well under that margin
   (see #3). The window is then never within ~11h of firing while the bridge is
   running, instead of drifting into range.
2. **Park at `AMPS_MIN`, not `AMPS_MAX`.** The amperage of a window that is
   never supposed to fire should be the least damaging value, not the most. If
   the bridge dies while disabled, the worst case becomes one hour at 8A rather
   than 48A. This is the defense-in-depth half of the fix and is worth landing
   even on its own — it is a one-line change.

**Verification:** disable via EVCC; confirm in the Rivian app that the window
sits ~12h out at 8A, watch it get re-parked on schedule, and confirm across the
following morning that no charge occurs.

### 3. Noon shifter runs on the wrong cadence
`server.py:84-109`

The intent is right and the thread should stay — with the permissive-window
model, *something* has to keep re-parking the window. The defect is that it runs
once per day at noon, which is structurally too late for a 06:00 window, and it
only ever chooses between two fixed slots (18:00 / 06:00).

**Fix:** replace the once-daily noon trigger with a periodic re-park — every
30–60 min, well inside the 12h margin from #1. Drop the `last_shift_date`
day-latch and the hardcoded 18:00/06:00 pair; recompute `now + 12h` each time
and re-apply only when the stored window has drifted meaningfully closer.
`_apply_schedule`'s unchanged-schedule short-circuit already suppresses
redundant writes, but confirm it does (see #23) or the re-park loop becomes a
write every cycle.

Rename it to something that says what it does — `_disable_window_keepalive`.

### 4. Schedule writes race with EVCC
`server.py:98-106` vs `161-190`

The shifter holds `_refresh_lock` around read-then-write; no Flask handler takes
that lock, and Flask serves requests threaded. A `POST /charger/current` landing
at 12:00:xx can be clobbered, or clobber, depending on interleaving.

This one gets **worse**, not better, under the corrected design: the keepalive in
#3 fires every 30–60 min instead of once a day, so the read-modify-write window
overlaps EVCC traffic far more often. A keepalive re-park that interleaves with a
`POST /charger/current` can silently revert EVCC's enable.

**Fix:** a dedicated `_schedule_lock` held across the entire
`get_schedules()` → compare → write sequence in `_apply_schedule`, and across the
keepalive's read-then-write. Do not reuse `_refresh_lock` — vehicle-state refresh
and schedule writes should not block each other. The keepalive must also re-read
state inside the lock rather than acting on a value read before acquiring it.

### 2. Reported state contradicts reality if the window does fire
`server.py:148-158`, `rivian_api.py:227-236`

If the parked window ever arrives, the car is charging but `get_current_amps()`
only counts `duration == 1440`, so `/charger/enabled` reports `false` and
`/charger/current` reports `0` while `/charger/status` reports `C`. EVCC reads
that as out-of-sync and fights it.

#1 makes this rare rather than impossible — the window still exists, it is just
kept away from now. So this needs its own fix rather than falling out.

**Fix:** derive `enabled` from whether an enabled window actually covers the
current time, not from `duration == 1440`. That is the honest question for a
permissive-window API, and it answers correctly for both the 24h window and a
parked one. Add a regression test asserting `status == C` implies
`enabled == true`.

### 23. `enabled: false` on a schedule is read as "not charging"
`rivian_api.py:234`

`get_current_amps()` requires `s.get('enabled', False)` and returns `0`
otherwise. Under the permissive-window model a disabled schedule imposes no
restriction, so the car is free to charge — the bridge would report
`enabled: false` while the car charges at full rate. Reachable today if the owner
toggles the schedule off in the Rivian app.

**Fix:** covered by the #2 rewrite — "does an enabled window cover now?" — but
call out the inverted case explicitly in a test, and log a warning when the
bridge finds its schedule disabled underneath it.

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

1. **Confirm the permissive-window model.** This plan assumes: a schedule
   *permits* charging inside its window, and `enabled: false` (or no schedule at
   all) means no restriction, so the car charges freely when plugged in. That
   assumption drives all of Phase 1, and the opposite reading inverts #1, #2 and
   #23. Confirm with one deliberate test on the car — set `enabled: false` while
   plugged in and observe whether charging starts — before writing any code.
2. **Is there an upper bound on how far out a window can be parked?** #1 assumes
   `now + 12h` is freely settable. If Rivian normalizes or rejects certain
   offsets, the margin shrinks and the keepalive cadence in #3 has to tighten.
3. **What does the car do while plugged in with no schedule covering now** —
   idle at `B`, or draw nothing but report `C`? Determines the expected status
   during a normal disabled period, and therefore what #2's test asserts.
4. **Is `distanceToEmpty` km or miles?** Needs one real-vehicle reading (#11).
5. **Does EVCC ever send `maxcurrent: 0`,** or is disable always routed through
   `enable`? Determines whether `set_charge_amps(0)` should raise or no-op.
6. **Should the bridge preserve user schedules (#14),** or is "the bridge owns
   the schedule list" acceptable if documented? Preserving is more work and more
   failure modes.

---

## Suggested sequencing

0. **#1 part 2 alone** — park the disable window at `AMPS_MIN` instead of
   `AMPS_MAX`. One line, no design questions attached, and it caps the blast
   radius of every other bug here at 8A. Worth landing immediately.
1. **Rest of Phase 1** — the actual reported problem, gated on open question 1.
2. **Phase 2** — hardening; the `_gql` helper (#5) is the keystone and should land first within the phase.
3. **Phase 4 #19** — the port fix is a one-line-per-file change that unblocks Docker users; can be pulled forward and shipped independently at any time.
4. **Phase 3**, then the rest of **Phase 4**.

## Testing

- Unit tests with a mocked GraphQL transport covering: `data: null` + `errors`,
  HTTP 500, timeout, MFA login response, and empty vehicle list. Every one of
  these currently crashes or silently misreports.
- A state-consistency test asserting `status == C` implies `enabled == true`,
  and its inverse for #23 (schedule disabled underneath the bridge).
- A clock-driven test for the keepalive (#3): advance time across many cycles
  while "disabled" and assert the parked window never comes within a threshold
  of now, including across a midnight wrap.
- Manual, on the real vehicle, before declaring #1 fixed: disable via EVCC and
  confirm across the following morning that no 6AM charge occurs.
