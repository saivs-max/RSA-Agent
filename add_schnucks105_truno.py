#!/usr/bin/env python3
"""
SUPERSEDED — use fix_missing_truno_rows.py instead.

That script scans the full RSA Tracker for ALL TRUNO carts stuck in
"Pending Truno Scheduled" with no scheduled date (not just Schnucks 105)
and creates the missing rows in the Truno tracker in one pass.

    python3 fix_missing_truno_rows.py

This file is kept for reference only.
"""
import sys
import os

# Make sure we load from the project folder
sys.path.insert(0, os.path.dirname(__file__))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import gspread
import rsa_sheets as sheets

STORE   = "Schnucks 105"
CARTS   = "2A, 6A"
ADDRESS = "4333 Butler Hill Rd, St. Louis, MO 63128"
TRUNO_CUSTOMER_NUM = "ISC0050-MO-0105"
MANAGER = "MDW"
RSA_SERIALS = {
    "2A": "P_SCHNUCKS_105_M3_002A",
    "6A": "P_SCHNUCKS_105_M3_006A",
}

def main():
    print(f"Adding Truno row for {STORE} — Carts {CARTS}...")

    result = sheets._schedule_truno(
        cartId=CARTS,
        store=STORE,
        notes=(
            "Carts 2A (P_SCHNUCKS_105_M3_002A) and 6A (P_SCHNUCKS_105_M3_006A) — "
            "added via RSA Webapp but Truno row was blocked (no address at submission). "
            "Manually added as fix."
        ),
        submittedBy="Sai VS",
        address=ADDRESS,
        rsa_serials=RSA_SERIALS,
    )

    if result.get("blocked") or result.get("error"):
        print(f"ERROR: {result.get('error')}")
        sys.exit(1)

    row = result.get("trunoRow")
    print(f"✓ Truno row created at row {row}")
    print(f"  Address : {result.get('address')}")
    print(f"  Status  : {result.get('status')}")
    print(f"  URL     : {result.get('trunoUrl')}")

    # Also write Truno customer number (col 2) and Manager (col 12)
    ws = sheets._ws(sheets.TRUNO_SPREADSHEET_ID, sheets.TRUNO_SHEET)
    extra = [
        gspread.Cell(row, 2, TRUNO_CUSTOMER_NUM),
        gspread.Cell(row, 12, MANAGER),
    ]
    sheets._update(ws, extra)
    print(f"✓ Wrote customer number ({TRUNO_CUSTOMER_NUM}) and manager ({MANAGER})")
    print("\nDone. Nicole will see this row as 'Pending' and schedule accordingly.")

if __name__ == "__main__":
    main()
