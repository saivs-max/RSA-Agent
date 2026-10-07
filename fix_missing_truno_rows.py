#!/usr/bin/env python3
"""
Fix: scan the RSA Tracker for ALL TRUNO carts stuck in "Pending Truno Scheduled"
with no RSA Scheduled Date, then create the missing Truno tracker rows.

Root cause: GAS_AddCartManual.gs blocks Truno row creation when no store address
is supplied at submission time:
    if (isTruno && !address) { return { error: '...', needAddress: true }; }

These carts appear in the RSA Tracker (status = "Pending Truno Scheduled") but
have NO corresponding row in the Truno RSA Tracker that Nicole uses to schedule.

Usage (run from the project folder):
    python3 fix_missing_truno_rows.py

Requires:
    - sa-key.json (service-account key) in the project folder, or
      GOOGLE_SA_KEY_FILE env var pointing to it
    - gspread, google-auth packages
"""
import sys
import os
import re
import csv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import gspread
import rsa_sheets as sheets

# ── Known Truno customer numbers and managers ──────────────────────────────────
# For stores that appear in the RSA Tracker but don't yet have a Truno row,
# these values are written to cols 2 (Truno Customer #) and 12 (Manager) so
# Nicole's sheet is complete from the start.
# Add any new stores here if they show up as missing.
KNOWN_TRUNO_INFO = {
    "Schnucks 105": {
        "customer_num": "ISC0050-MO-0105",
        "manager": "MDW",
    },
    # Add more stores as needed:
    # "Store Name": {"customer_num": "ISC0050-XX-XXXX", "manager": "XYZ"},
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def extract_cart_label(serial: str) -> str:
    """Extract the short cart label from a full RSA serial.

    Examples:
        'P_SCHNUCKS_105_M3_002A'  →  '2A'
        'P_SHOPRITE_433_M3_012'   →  '12'
        'P_BOWMAN_S_M3_008A'      →  '8A'
    """
    # Split on _M3_ and take the suffix
    parts = serial.upper().split("_M3_")
    if len(parts) == 2:
        suffix = parts[1]
        # Strip leading zeros while keeping trailing letters (e.g. '002A' → '2A')
        label = re.sub(r'^0+(?=[1-9])', '', suffix)
        return label or suffix
    # Fallback: use the last underscore-separated segment
    seg = serial.split("_")[-1]
    return re.sub(r'^0+(?=[1-9])', '', seg) or seg


def load_caper_addresses() -> dict:
    """Load store addresses from caper_stores.csv.

    Returns a dict keyed by lowercased store name fragment → address string.
    The CSV format is: store_label, address, store_num, deployment_id, cart_count
    """
    result = {}
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "caper_stores.csv")
    if not os.path.exists(csv_path):
        print(f"  [warn] caper_stores.csv not found at {csv_path}")
        return result
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                name = row[0].strip().strip('"')
                addr = row[1].strip().strip('"')
                if name and addr:
                    result[name.lower()] = addr
    return result


def fuzzy_find_address(store: str, caper_addrs: dict) -> str:
    """Try to match a store name against the caper address dict.

    Tries exact match first, then substring containment in both directions.
    """
    s_lower = store.lower()
    # Exact match
    if s_lower in caper_addrs:
        return caper_addrs[s_lower]
    # store name appears in caper name (e.g. "Schnucks 105" in "Butler Hill Store 105")
    for name, addr in caper_addrs.items():
        if s_lower in name or name in s_lower:
            return addr
    # Numeric store-number match (e.g. "105" from "Schnucks 105")
    nums = re.findall(r'\d+', store)
    if nums:
        for num in nums:
            for name, addr in caper_addrs.items():
                if re.search(r'\b' + re.escape(num) + r'\b', name):
                    return addr
    return ""


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("RSA Agent — Missing Truno Row Fix")
    print("=" * 60)
    print()
    print("Connecting to RSA Tracker…")

    ws_rsa = sheets._ws(sheets.RSA_SPREADSHEET_ID, sheets.RSA_SHEET)
    all_rows = ws_rsa.get_all_values()

    # Rows 1-3 are header/title; data starts at RSA_DATA_START (row 4, index 3)
    data_rows = all_rows[sheets.RSA_DATA_START - 1:]
    print(f"  {len(data_rows)} data rows loaded from RSA Tracker.")

    # Find carts: TRUNO vendor + "Pending Truno Scheduled" status + no scheduled date
    missing = []
    for i, row in enumerate(data_rows, start=sheets.RSA_DATA_START):
        # Pad short rows to avoid IndexError
        padded = row + [""] * max(0, sheets.C_RSASTATUS - len(row))

        cart_id  = padded[sheets.C_CARTID   - 1].strip()
        store    = padded[sheets.C_STORE     - 1].strip()
        vendor   = padded[sheets.C_RSAVENDOR - 1].strip()
        sched    = padded[sheets.C_RSASCHED  - 1].strip()
        status   = padded[sheets.C_RSASTATUS - 1].strip()

        if (
            cart_id
            and vendor.upper() == "TRUNO"
            and status == "Pending Truno Scheduled"
            and not sched
        ):
            missing.append({
                "row_num": i,
                "serial":  cart_id,
                "store":   store,
                "label":   extract_cart_label(cart_id),
            })

    if not missing:
        print()
        print("✓ Nothing to fix — no 'Pending Truno Scheduled' TRUNO carts with a")
        print("  missing scheduled date were found.")
        return

    # Group by store
    by_store: dict[str, list] = {}
    for m in missing:
        by_store.setdefault(m["store"], []).append(m)

    print()
    print(f"Found {len(missing)} cart(s) across {len(by_store)} store(s) with no Truno row:")
    for store, carts in by_store.items():
        labels = ", ".join(c["label"] for c in carts)
        serials = ", ".join(c["serial"] for c in carts)
        print(f"  • {store}: carts {labels}")
        print(f"    Serials: {serials}")

    caper_addrs = load_caper_addresses()
    ws_truno = sheets._ws(sheets.TRUNO_SPREADSHEET_ID, sheets.TRUNO_SHEET)

    created = 0
    skipped = 0

    for store, carts in by_store.items():
        labels      = ", ".join(c["label"] for c in carts)
        rsa_serials = {c["label"]: c["serial"] for c in carts}
        serials_str = ", ".join(
            f"{c['label']} ({c['serial']})" for c in carts
        )

        print()
        print(f"── {store} ──")
        print(f"   Carts   : {labels}")

        # Resolve address: store_map first, then caper CSV fallback
        addr = sheets._resolve_store_address(store, None)
        if not addr:
            addr = fuzzy_find_address(store, caper_addrs)
        if not addr:
            print(f"   ✗ SKIPPED — could not resolve address.")
            print(f"     Fix: add '{store}' to store_map or caper_stores.csv,")
            print(f"     then rerun. Or call _schedule_truno with address=... directly.")
            skipped += 1
            continue

        print(f"   Address : {addr}")

        result = sheets._schedule_truno(
            cartId=labels,
            store=store,
            notes=(
                f"Carts {serials_str} — added via RSA Webapp but Truno row was blocked "
                "(no address at submission time). Added retroactively as fix. "
                "[fix_missing_truno_rows.py]"
            ),
            submittedBy="Sai VS",
            address=addr,
            rsa_serials=rsa_serials,
        )

        if result.get("blocked") or result.get("error"):
            print(f"   ✗ ERROR: {result.get('error')}")
            skipped += 1
            continue

        truno_row = result.get("trunoRow")
        print(f"   ✓ Truno row created at row {truno_row}")
        print(f"   Status  : {result.get('status')}")

        # Write Truno customer number (col 2) and manager (col 12) if known
        info = KNOWN_TRUNO_INFO.get(store, {})
        extra_cells = []
        if info.get("customer_num"):
            extra_cells.append(gspread.Cell(truno_row, 2, info["customer_num"]))
        if info.get("manager"):
            extra_cells.append(gspread.Cell(truno_row, 12, info["manager"]))
        if extra_cells:
            sheets._update(ws_truno, extra_cells)
            if info.get("customer_num"):
                print(f"   ✓ Customer # : {info['customer_num']}")
            if info.get("manager"):
                print(f"   ✓ Manager    : {info['manager']}")
        else:
            print(f"   [info] No customer # / manager in KNOWN_TRUNO_INFO for this store.")
            print(f"          Fill cols B and L in the Truno tracker row {truno_row} manually.")

        print(f"   Truno URL: {result.get('trunoUrl')}")
        created += 1

    print()
    print("=" * 60)
    print(f"Done.  Created {created} Truno row(s).  Skipped {skipped} store(s).")
    if skipped:
        print("Re-run after adding missing addresses to KNOWN_TRUNO_INFO or store_map.")
    else:
        print("Nicole will see the new row(s) as 'Pending' and schedule accordingly.")


if __name__ == "__main__":
    main()
