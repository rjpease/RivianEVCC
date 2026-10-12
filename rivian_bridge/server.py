"""
Rivian → EVCC HTTP Bridge

Acts as BOTH the EVCC vehicle plugin (SoC / status / range) AND the EVCC
charger plugin (current control via Rivian's charging-schedule API).

This is necessary because the Tesla Wall Connector has no API for
third-party current control — EVCC adjusts charging rate by updating the
Rivian charging schedule instead.

─────────────────────────────────────────────────────────────────────────────
Vehicle endpoints  (polled by EVCC vehicle plugin)
  GET  /soc              → integer 0–100
  GET  /status           → "A" | "B" | "C"
  GET  /range            → integer km

Charger endpoints  (called by EVCC charger plugin)
  GET  /charger/status   → "A" | "B" | "C"
  GET  /charger/enabled  → "true" | "false"
  GET  /charger/current  → integer amps (0 if disabled)
  POST /charger/enable   → body: true / false
  POST /charger/current  → body: amps integer

Utility
  GET  /health           → JSON summary
─────────────────────────────────────────────────────────────────────────────
"""

import logging
import sys
import threading
import time
from datetime import datetime

from flask import Flask, request, jsonify
from config import Config
from rivian_api import RivianAPI

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s: %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

config = Config('config.json')
rivian = RivianAPI(config, session_file='rivian-session.json')
rivian.refresh()

_last_refresh = time.time()
_refresh_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Staleness guard
# ---------------------------------------------------------------------------
# The Rivian session can expire (API starts returning UNAUTHENTICATED) while the
# last good values sit happily in the cache. Before 2026-09-09 every endpoint
# below kept answering 200 with those cached values, so evcc had no way to know
# it was flying blind.
#
# What that cost, 8-9 Sep 2026: session died 16:10:45, last good refresh 16:06:34.
# /soc kept returning 70% for 14 h while the car was actually at 58.9%. evcc
# concluded "limitSoc reached", stopped managing the loadpoint, and could not
# issue stop commands either (they failed the same silent way). The car resumed
# on its own schedule at 48 A / ~11 kW at 06:00 and had to be stopped by hand.
#
# A 503 is strictly better than a confident wrong number: evcc treats the
# vehicle/charger as unavailable and does not act on fiction.
STALE_AFTER = max(900, config.cache_ttl * 3)


def _data_age():
    """Seconds since the last SUCCESSFUL vehicle-state refresh."""
    return time.time() - _last_refresh


def _is_stale():
    return _data_age() > STALE_AFTER


def _stale_response():
    age = round(_data_age())
    logger.warning(
        'Refusing request: vehicle data is %ds old (limit %ds). The Rivian '
        'session has probably expired - re-run RivianSessionInitOTP.py.',
        age, STALE_AFTER)
    return jsonify({
        'error': 'Rivian data stale - session probably expired',
        'last_refresh_ago_seconds': age,
        'stale_after_seconds': STALE_AFTER,
    }), 503

# ---------------------------------------------------------------------------
# Background vehicle-state refresh
# ---------------------------------------------------------------------------

def _background_refresher():
    global _last_refresh
    while True:
        time.sleep(config.cache_ttl)
        logger.info('Background refresh: fetching Rivian vehicle state...')
        with _refresh_lock:
            try:
                rivian.refresh()
                _last_refresh = time.time()
            except Exception as e:
                logger.error('Background refresh failed: %s', e)


threading.Thread(target=_background_refresher, daemon=True).start()

# ---------------------------------------------------------------------------
# Background job — keeps the off-peak disable window at least 6 h away
# ---------------------------------------------------------------------------

def _noon_window_shifter():
    """
    Polls every minute. Twice per day — first tick after midnight and first
    tick after noon — if charging is currently disabled (off-peak window),
    re-applies the disable schedule. set_charge_amps(0) places the window at
    18:00 before noon and 06:00 after noon, so it is always >= 6 h away.
    Without the midnight flip, a window parked at 06:00 in the afternoon fires
    the next morning (CB-3).

    A half-day is only marked done once the window has been written (or
    charging is active and nothing needs moving), so a failure is retried on
    the next tick (C-027). Polling also avoids missing a trigger when the
    system was asleep.
    """
    last_done = None
    while True:
        time.sleep(60)
        now = datetime.now()
        half_day = (now.date(), now.hour >= 12)
        if half_day == last_done:
            continue
        target = '6AM' if now.hour >= 12 else '6PM'
        logger.info('Window shifter: checking if disable window needs moving to %s...', target)
        # Hold the schedule lock across the read and the write, so an EVCC
        # enable can't land in between and be overwritten (CH-002).
        with _refresh_lock, rivian.schedule_lock:
            try:
                # A failed read would look like "disabled" to get_current_amps()
                # and trigger a write with an empty location (E-003). Retry instead.
                if rivian.get_schedules() is None:
                    logger.error('Window shifter: schedule read failed — will retry')
                    continue
                if rivian.get_current_amps() != 0:
                    logger.info('Window shifter: charging active — no action needed')
                    last_done = half_day
                elif rivian.set_charge_amps(0):
                    logger.info('Window shifter: disable window moved to %s', target)
                    last_done = half_day
                else:
                    logger.error('Window shifter: write failed — will retry')
            except Exception as e:
                logger.error('Window shifter failed: %s — will retry', e)


threading.Thread(target=_noon_window_shifter, daemon=True).start()

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

# The bridge listens on all interfaces because HA reads /health over the LAN, but the
# write endpoints start/stop charging and set amps. Only evcc (same machine) may call them.
LOCAL_ADDRS = {'127.0.0.1', '::1'}


@app.before_request
def _writes_local_only():
    if request.method != 'GET' and request.remote_addr not in LOCAL_ADDRS:
        logger.warning('Rejected %s %s from %s (writes are localhost-only)',
                       request.method, request.path, request.remote_addr)
        return jsonify({'error': 'writes are only accepted from localhost'}), 403

# ── Vehicle plugin endpoints ─────────────────────────────────────────────────

@app.get('/soc')
def soc():
    if _is_stale():
        return _stale_response()
    value = rivian.get_soc()
    if value is None:
        return jsonify({'error': 'SoC not available'}), 503
    return str(round(float(value)))


@app.get('/status')
def status():
    if _is_stale():
        return _stale_response()
    return rivian.get_evcc_status()


@app.get('/range')
def range_km():
    if _is_stale():
        return _stale_response()
    value = rivian.get_range_km()
    if value is None:
        return jsonify({'error': 'Range not available'}), 503
    return str(value)


# ── Charger plugin endpoints ─────────────────────────────────────────────────

@app.get('/charger/status')
def charger_status():
    """Same IEC 61851 status used for vehicle, also serves as charger status."""
    if _is_stale():
        return _stale_response()
    return rivian.get_evcc_status()


@app.get('/charger/enabled')
def charger_enabled():
    """True if a 24-hour charging schedule is currently active."""
    if _is_stale():
        return _stale_response()
    amps = rivian.get_current_amps()
    return 'true' if amps >= rivian.AMPS_MIN else 'false'


@app.get('/charger/current')
def charger_current():
    """Return current schedule amperage (0 if disabled)."""
    if _is_stale():
        return _stale_response()
    return str(rivian.get_current_amps())


@app.post('/charger/enable')
def charger_enable():
    """
    Enable or disable charging.
    Body: plain text "true" or "false"
    EVCC sends the raw boolean value as the request body.
    """
    if _is_stale():
        return _stale_response()
    body = request.get_data(as_text=True).strip().lower()
    enabled = body == 'true'
    logger.info('charger/enable called: enabled=%s', enabled)
    rivian.enable_charging(enabled)
    return ('', 204)


@app.post('/charger/current')
def set_charger_current():
    """
    Set charging current.
    Body: plain integer amps (e.g. "16")
    EVCC sends the raw integer value as the request body.
    """
    if _is_stale():
        return _stale_response()
    body = request.get_data(as_text=True).strip()
    try:
        amps = int(float(body))
    except ValueError:
        return jsonify({'error': f'Invalid current value: {body!r}'}), 400

    logger.info('charger/current called: amps=%d', amps)
    rivian.set_charge_amps(amps)
    return ('', 204)


# ── Health / debug ───────────────────────────────────────────────────────────

@app.get('/health')
def health():
    return jsonify({
        'status': 'stale' if _is_stale() else 'ok',
        'stale': _is_stale(),
        'soc': rivian.get_soc(),
        'evcc_status': rivian.get_evcc_status(),
        'range_km': rivian.get_range_km(),
        'rivian_charging_status': rivian.charging_status,
        'charger_enabled': rivian.get_current_amps() >= rivian.AMPS_MIN,
        'charger_current_amps': rivian.get_current_amps(),
        'last_refresh_ago_seconds': round(_data_age()),
        'stale_after_seconds': STALE_AFTER,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    logger.info('Starting Rivian EVCC Bridge on port %d', config.port)
    app.run(host='0.0.0.0', port=config.port)
