#!/usr/bin/env python3
"""
Install a new Enphase local-access JWT into the EVCC database and restart EVCC.

Usage:
    python3 install-enphase-token.py "eyJraWQi..."
"""

import re
import shutil
import sqlite3
import subprocess
import sys

EVCC_DB = "/Users/rjpease/.evcc/evcc.db"


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 install-enphase-token.py \"eyJraWQi...\"")
        print("Paste the full JWT token from the Enphase token page as the argument.")
        sys.exit(1)

    token = sys.argv[1].strip().strip('"').strip("'")

    if not token.startswith("eyJ"):
        print(f"ERROR: This doesn't look like a JWT token (should start with 'eyJ').")
        print(f"Got: {token[:40]}...")
        sys.exit(1)

    print(f"Token looks valid ({len(token)} chars).")

    # Back up the database
    backup = EVCC_DB + ".bak"
    shutil.copy2(EVCC_DB, backup)
    print(f"Database backed up to {backup}")

    # Find and update every Enphase device config row in the DB
    con = sqlite3.connect(EVCC_DB)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]

    token_pattern = re.compile(r'"token"\s*:\s*"[^"]*"')
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
                new_val = token_pattern.sub(f'"token": "{token}"', val)
                if new_val != val:
                    col = cols[i]
                    cur.execute(f"UPDATE [{table}] SET [{col}]=? WHERE rowid=?",
                                (new_val, rowid))
                    updated += 1
                    print(f"  Updated {table}.{col} (rowid={rowid})")

    if updated == 0:
        print("WARNING: No matching Enphase rows found in the database.")
        print("The token was NOT installed. Check the DB manually.")
        con.close()
        sys.exit(1)

    con.commit()
    con.close()
    print(f"  {updated} row(s) updated.")

    # Restart EVCC
    print("Restarting EVCC...")
    subprocess.run(["brew", "services", "restart", "evcc"], check=True)
    print("Done. Watch /opt/homebrew/var/log/evcc.log — 401 errors should stop within 30s.")


if __name__ == "__main__":
    main()
