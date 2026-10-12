#!/usr/bin/env python3
"""
Retrieve an Enphase local-access token and install it into EVCC.
Follows the approach from the official Enphase PDF documentation.

Usage:
    python3 refresh-enphase-token.py
"""

import getpass
import json
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import urllib.request
import urllib.parse
import urllib.error

# Python from python.org on macOS doesn't ship with system CA certs.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")

USERNAME      = "rjpease@gmail.com"
ENVOY_SERIAL  = "202334094017"
EVCC_DB       = "/Users/rjpease/.evcc/evcc.db"


def post(url, data=None, json_body=None):
    if json_body is not None:
        body    = json.dumps(json_body).encode()
        headers = {"Content-Type": "application/json"}
    else:
        body    = urllib.parse.urlencode(data).encode()
        headers = {}
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
        return r.read().decode()


def main():
    print("=== Enphase token refresh (per Enphase PDF documentation) ===")
    print()

    password = getpass.getpass(f"Enphase Enlighten password for {USERNAME}: ")
    print()

    # ── Step 1: Login (exactly as shown in the PDF) ───────────────────────────
    print("Step 1: Logging in to Enphase Enlighten...")
    try:
        resp_text = post(
            "https://enlighten.enphaseenergy.com/login/login.json",
            data={"user[email]": USERNAME, "user[password]": password},
        )
    except urllib.error.HTTPError as e:
        print(f"  Login failed: HTTP {e.code} — {e.read().decode()}")
        sys.exit(1)

    resp_data = json.loads(resp_text)
    if resp_data.get("message") != "success":
        print(f"  Login failed: {resp_data}")
        sys.exit(1)

    session_id = resp_data["session_id"]
    system_id  = resp_data.get("system_id", "unknown")
    print(f"  OK — session_id obtained, system_id={system_id}")
    print()

    # ── Step 2: Gateway serial ────────────────────────────────────────────────
    envoy_serial = ENVOY_SERIAL
    print(f"Step 2: Using IQ Gateway serial {envoy_serial}")
    print()

    # ── Step 3: Retrieve token (exactly as shown in the PDF) ─────────────────
    print("Step 3: Fetching token from entrez.enphaseenergy.com...")
    try:
        token_raw = post(
            "https://entrez.enphaseenergy.com/tokens",
            json_body={
                "session_id": session_id,
                "serial_num": envoy_serial,
                "username":   USERNAME,
            },
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  Token request failed: HTTP {e.code} — {body}")
        sys.exit(1)

    token = token_raw.strip()
    if not token.startswith("eyJ"):
        print(f"  Unexpected response (not a JWT): {token[:200]}")
        sys.exit(1)

    print(f"  Token obtained ({len(token)} chars).")
    print()

    # ── Step 4: Install token into EVCC database ──────────────────────────────
    print(f"Step 4: Installing token into EVCC database...")
    shutil.copy2(EVCC_DB, EVCC_DB + ".bak")
    print(f"  Backed up to {EVCC_DB}.bak")

    con = sqlite3.connect(EVCC_DB)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables  = [r[0] for r in cur.fetchall()]
    pattern = re.compile(r'"token"\s*:\s*"[^"]*"')
    updated = 0

    for table in tables:
        try:
            cur.execute(f"SELECT rowid, * FROM [{table}]")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        except Exception:
            continue
        for row in rows:
            rowid = row[0]
            for i, val in enumerate(row[1:], 1):
                if not isinstance(val, str):
                    continue
                if '"token"' not in val or "enphase" not in val.lower():
                    continue
                new_val = pattern.sub(f'"token": "{token}"', val)
                if new_val != val:
                    cur.execute(f"UPDATE [{table}] SET [{cols[i]}]=? WHERE rowid=?",
                                (new_val, rowid))
                    updated += 1
                    print(f"  Updated {table}.{cols[i]} (rowid={rowid})")

    if updated == 0:
        print("  ERROR: No matching Enphase rows found — token not installed.")
        con.close()
        sys.exit(1)

    con.commit()
    con.close()
    print(f"  {updated} row(s) updated.")
    print()

    # ── Step 5: Restart EVCC ──────────────────────────────────────────────────
    print("Step 5: Restarting EVCC...")
    subprocess.run(["brew", "services", "restart", "evcc"], check=True)
    print()
    print("Done! 401 errors in /opt/homebrew/var/log/evcc.log should stop within 30s.")


if __name__ == "__main__":
    main()
