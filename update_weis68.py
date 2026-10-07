#!/usr/bin/env python3
"""
Update Weis 68 RSA tracker with scale test results from Truno visit (8/19-8/20/2026).

Run from the RSA Agent project folder:
  python3 update_weis68.py

Results extracted from:
  - Weis 68 - 26.08.19.pdf  (Carts 1-15, service date 08/19/2026, Truno Call #2744979)
  - Weis 68 - 26.08.20.pdf  (Carts 16-20, service date 08/20/2026, Truno Call #2744979)
  Technician: Daniel Brion (08229)

FAILED carts (set to "Failed RSA" + note "Failed RSA Inspection — Pending Reschedule"):
  Cart 1  — reads .01 lbs under 1st 5-lb reading
  Cart 9  — reads .02 lbs under 1st 5-lb reading
  Cart 12 — reads .02 lbs under 1st 5-lb reading
  Cart 18 — reads .02 lbs under 1st 5-lb reading
"""

import rsa_sheets

STORE        = "Weis 68"
TRUNO_CALL   = "2744979"
TECH         = "Daniel Brion (08229)"

# Cart number → (passed, caper_serial, service_date, failure_comment)
RESULTS = {
    1:  (False, "1MI247060",  "08/19/2026",
         "Cart consistently reads .01 lbs under the 1st reading of 5lbs. "
         "Per Instacart SOP the 1st reading of 5lbs must be exactly 5.00 lbs."),
    2:  (True,  "1MI247035",  "08/19/2026", ""),
    3:  (True,  "1MI247095",  "08/19/2026", ""),
    4:  (True,  "1MI247077",  "08/19/2026", ""),
    5:  (True,  "1MI247032",  "08/19/2026", ""),
    6:  (True,  "1MI247031",  "08/19/2026", ""),
    7:  (True,  "1MI247094",  "08/19/2026", ""),
    8:  (True,  "1MI247097",  "08/19/2026", ""),
    9:  (False, "1MI247000",  "08/19/2026",
         "Cart consistently reads .02 lbs under the 1st reading of 5lbs. "
         "Per Instacart SOP the 1st reading of 5lbs must be exactly 5.00 lbs."),
    10: (True,  "1MI247065",  "08/19/2026", ""),
    11: (True,  "1MI247068",  "08/19/2026", ""),
    12: (False, "1MI247098",  "08/19/2026",
         "Cart consistently reads .02 lbs under the 1st reading of 5lbs. "
         "Per Instacart SOP the 1st reading of 5lbs must be exactly 5.00 lbs."),
    13: (True,  "1MI247091",  "08/19/2026", ""),
    14: (True,  "1MI247001",  "08/19/2026", ""),
    15: (True,  "1MI247084",  "08/19/2026", ""),
    16: (True,  "1MI2470693", "08/20/2026", ""),
    17: (True,  "1MI2470195", "08/20/2026", ""),
    18: (False, "1MI2470317", "08/20/2026",
         "Cart consistently reads .02 lbs under the 1st reading of 5 lbs. "
         "Per Instacart SOP the 1st reading of 5 lbs must be exactly 5.00 lbs."),
    19: (True,  "1MI2470997", "08/20/2026", ""),
    20: (True,  "1MI2470356", "08/20/2026", ""),
}

# Load cart ID mapping from truno_map.json
truno_data = rsa_sheets._load_truno_map()
weis68     = truno_data.get("100|18202", {}).get("carts", {})

passed_carts, failed_carts, errors = [], [], []

for cart_num, (passed, serial, svc_date, fail_note) in sorted(RESULTS.items()):
    cart_id = weis68.get(str(cart_num))
    if not cart_id:
        errors.append(f"Cart {cart_num}: NOT FOUND in truno_map.json")
        continue

    status_text = "Passed" if passed else "Failed"
    result_text = (
        f"Truno Scale Test | {svc_date} | Tech: {TECH} | "
        f"Truno Call: {TRUNO_CALL} | Caper Serial: {serial} | Result: {status_text}"
    )
    if fail_note:
        result_text += f" | Failure: {fail_note}"

    # Write RSA Test Results column (U) + set pass/fail RSA Status
    r = rsa_sheets.set_test_result(cart_id, store=STORE, passed=passed, result_text=result_text)

    if r.get("error"):
        errors.append(f"Cart {cart_num} ({cart_id}): {r['error']}")
        continue

    if not passed:
        # Append reschedule note to Notes column (S)
        rsa_sheets.update_cart(
            cart_id,
            storeHint=STORE,
            notes="Failed RSA Inspection — Pending Reschedule",
            appendNotes=True,
        )
        failed_carts.append((cart_num, cart_id, serial))
    else:
        passed_carts.append((cart_num, cart_id, serial))

# ── Print summary ─────────────────────────────────────────────────────────────
print("=" * 65)
print(f"WEIS 68 RSA TRACKER UPDATE — Truno Call #{TRUNO_CALL}")
print("=" * 65)

print(f"\n✅ PASSED ({len(passed_carts)} carts)  →  Completed RSA / Passed")
for cn, cid, serial in passed_carts:
    print(f"   Cart {cn:>2}: {cid}  (Serial: {serial})")

print(f"\n❌ FAILED ({len(failed_carts)} carts)  →  Failed RSA / Pending Reschedule")
for cn, cid, serial in failed_carts:
    print(f"   Cart {cn:>2}: {cid}  (Serial: {serial})")

if errors:
    print(f"\n⚠️  ERRORS ({len(errors)}):")
    for e in errors:
        print(f"   {e}")

total = len(passed_carts) + len(failed_carts)
print(f"\nTotal updated: {total} / {len(RESULTS)}")
print(f"Tracker: {rsa_sheets.webapp_url()}")
