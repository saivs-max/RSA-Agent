#!/usr/bin/env python3
"""
fix_dri.py — inspect and fix DRI values for specific stores or all carts.

Usage:
    python3 fix_dri.py                     # dry-run: show what would change
    python3 fix_dri.py --apply             # write changes to the sheet
    python3 fix_dri.py --store "457"       # filter to one store (substring match)
    python3 fix_dri.py --store "457" --apply

The script finds carts where DRI doesn't match the expected value based on RSA Status,
OR where you want to force-override DRI regardless of the normalizer rule.
"""
import os, sys, warnings, argparse
warnings.filterwarnings("ignore")

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import gspread
import rsa_sheets as sheets

CSM   = os.environ.get("CSM_DRI_NAME", "CSM")
HWOPS = os.environ.get("HWOPS_DRI_NAME", "HW Ops")

ap = argparse.ArgumentParser()
ap.add_argument("--apply",  action="store_true", help="write changes (default is dry-run)")
ap.add_argument("--store",  default="",          help="filter by store substring")
ap.add_argument("--force-hwops", action="store_true",
                help="force DRI=HW Ops for matching carts regardless of RSA status")
args = ap.parse_args()

rows = sheets._rsa_data(force=True)
ws   = sheets._ws(sheets.RSA_SPREADSHEET_ID, sheets.RSA_SHEET)

print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
if args.store:
    print(f"Filter: store contains {args.store!r}")
print()

to_fix = []
for i, r in enumerate(rows):
    cid = sheets._cell(r, sheets.C_CARTID)
    if not cid:
        continue
    store = sheets._cell(r, sheets.C_STORE)
    rsa   = sheets._cell(r, sheets.C_RSASTATUS)
    dri   = sheets._cell(r, sheets.C_DRI)

    if args.store and args.store.lower() not in store.lower():
        continue

    if args.force_hwops:
        want = HWOPS
    else:
        want = CSM if rsa == "Completed RSA" else HWOPS

    if dri != want:
        row_num = sheets.RSA_DATA_START + i
        to_fix.append((row_num, cid, store, rsa, dri, want))

print(f"{'Cart ID':<45} {'Store':<30} {'RSA Status':<25} {'DRI now':<15} {'DRI want'}")
print("-" * 130)
for row_num, cid, store, rsa, dri, want in to_fix:
    print(f"{cid:<45} {store:<30} {rsa:<25} {dri:<15} → {want}")

print(f"\nTotal rows to fix: {len(to_fix)}")

if not to_fix:
    print("Nothing to do.")
    sys.exit(0)

if not args.apply:
    print("\nDry-run — no changes written. Re-run with --apply to commit.")
    sys.exit(0)

cells = [gspread.Cell(row_num, sheets.C_DRI, want) for row_num, cid, store, rsa, dri, want in to_fix]
ws.update_cells(cells)
sheets._bust_rsa_cache()
print(f"\nDone — updated {len(cells)} row(s).")
