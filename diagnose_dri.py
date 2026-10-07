#!/usr/bin/env python3
"""
diagnose_dri.py — inspect actual RSA Status and DRI values in the tracker
and preview what normalize_tracker would change.

Run from the RSA Agent directory:
    python3 diagnose_dri.py
"""
import os, sys, warnings
warnings.filterwarnings("ignore")

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import rsa_sheets as sheets

CSM    = os.environ.get("CSM_DRI_NAME", "CSM")
HWOPS  = os.environ.get("HWOPS_DRI_NAME", "HW Ops")

rows = sheets._rsa_data(force=True)   # bypass cache, always fresh

from collections import Counter
rsa_counts = Counter()
dri_counts = Counter()
would_change = []

for r in rows:
    cid = sheets._cell(r, sheets.C_CARTID)
    if not cid:
        continue
    rsa = sheets._cell(r, sheets.C_RSASTATUS)
    dri = sheets._cell(r, sheets.C_DRI)
    rsa_counts[repr(rsa)] += 1
    dri_counts[repr(dri)] += 1

    if rsa == "Completed RSA" and dri != CSM:
        would_change.append((cid, rsa, dri, f"→ DRI={CSM!r}"))
    elif rsa != "Completed RSA" and dri != HWOPS:
        would_change.append((cid, rsa, dri, f"→ DRI={HWOPS!r}"))

print("=== RSA Status values in the tracker ===")
for v, n in rsa_counts.most_common():
    print(f"  {n:3}x  {v}")

print("\n=== DRI values in the tracker ===")
for v, n in dri_counts.most_common():
    print(f"  {n:3}x  {v}")

print(f"\n=== Would normalize_tracker change? ({len(would_change)} rows) ===")
if would_change:
    for cid, rsa, dri, action in would_change[:40]:
        print(f"  {cid[:42]:<44} RSA={rsa:<28} DRI={dri!r:<20} {action}")
    if len(would_change) > 40:
        print(f"  ... and {len(would_change)-40} more")
else:
    print("  Nothing to change — all DRI values already match the rules.")
    print("\n  NOTE: If you expected changes, the RSA Status strings in the sheet may")
    print("  not exactly match 'Completed RSA'. Check the values printed above.")
