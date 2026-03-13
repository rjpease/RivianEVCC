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
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

# ── Vehicle plugin endpoints ─────────────────────────────────────────────────

@app.get('/soc')
def soc():
    value = rivian.get_soc()
    if value is None:
        return jsonify({'error': 'SoC not available'}), 503
    return str(round(float(value)))


@app.get('/status')
def status():
    return rivian.get_evcc_status()


@app.get('/range')
def range_km():
    value = rivian.get_range_km()
    if value is None:
        return jsonify({'error': 'Range not available'}), 503
    return str(value)


# ── Charger plugin endpoints ─────────────────────────────────────────────────

@app.get('/charger/status')
def charger_status():
    """Same IEC 61851 status used for vehicle, also serves as charger status."""
    return rivian.get_evcc_status()


@app.get('/charger/enabled')
def charger_enabled():
    """True if a 24-hour charging schedule is currently active."""
    amps = rivian.get_current_amps()
    return 'true' if amps >= rivian.AMPS_MIN else 'false'


@app.get('/charger/current')
def charger_current():
    """Return current schedule amperage (0 if disabled)."""
    return str(rivian.get_current_amps())


@app.post('/charger/enable')
def charger_enable():
    """
    Enable or disable charging.
    Body: plain text "true" or "false"
    EVCC sends the raw boolean value as the request body.
    """
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
        'status': 'ok',
        'soc': rivian.get_soc(),
        'evcc_status': rivian.get_evcc_status(),
        'range_km': rivian.get_range_km(),
        'rivian_charging_status': rivian.charging_status,
        'charger_enabled': rivian.get_current_amps() >= rivian.AMPS_MIN,
        'charger_current_amps': rivian.get_current_amps(),
        'last_refresh_ago_seconds': round(time.time() - _last_refresh),
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    logger.info('Starting Rivian EVCC Bridge on port %d', config.port)
    app.run(host='0.0.0.0', port=config.port)
