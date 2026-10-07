#!/usr/bin/env python3
"""Offline unit tests for truno_failures.parse_verdicts (no network).

Covers the real note/result strings from the live Truno tracker, including the
Test-Results-plus-Notes combination the parser must get right before it is ever
allowed to write "Failed RSA" to the source-of-truth tracker."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import truno_failures as F

PASS, FAIL = [], []
def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name + (("  [%s]" % extra) if extra and not cond else ""))

def verdicts(carts, result, notes):
    v, reason = F.parse_verdicts(carts, result, notes)
    return v, reason

# 1) per-cart attribution from Notes, Test Results neutral
v, _ = verdicts(["7", "9"], "Tested up to 30 lbs", "Cart 9 failed, Cart 7 passed.")
chk("Sprouts 507: cart 9 fail / cart 7 pass", v == {"9": "fail", "7": "pass"}, str(v))

# 2) 'cannot be located' is an exception, not a fail; sibling cart still fails
v, _ = verdicts(["1", "7"], "Pending", "Cart 1 cannot be located. Cart 7 failed shift test. Pending form.")
chk("Kroger 11: cart 1 exception, cart 7 fail", v == {"1": "exception", "7": "fail"}, str(v))

# 3) revisit-tab plain notes
v, _ = verdicts(["1", "7"], "", "Cart 1 failed. Cart 7 passed.")
chk("Sprouts 505: cart 1 fail / cart 7 pass", v == {"1": "fail", "7": "pass"}, str(v))

# 4) 'out of tolerence' (H) + 'N passed, M did not' (I) → cart numbers, not counts
v, r = verdicts(["2", "6"], "out of tolerence", "2 passed, 6 did not.")
chk("Sprouts 509: cart 2 pass / cart 6 fail", v == {"2": "pass", "6": "fail"}, str(v))
chk("Sprouts 509: fully attributed → no row reason", r is None, repr(r))

# 5) 'out of tolerence' with NO per-cart note → ambiguous (row reason, no fail verdicts)
v, r = verdicts(["1", "6", "9", "10"], "out of tolerence",
                "Reminded technician of tolerances and requested form be re-submitted.")
chk("Sprouts 558: no fail verdicts", all(x != "fail" for x in v.values()), str(v))
chk("Sprouts 558: row flagged ambiguous", bool(r), repr(r))

# 6) single-cart row + out-of-tolerance → that cart fails
v, _ = verdicts(["1"], "out of tolerence", "")
chk("single cart out-of-tol → fail", v == {"1": "fail"}, str(v))

# 7) bare 'Passed' in Test Results applies to the row's carts
v, _ = verdicts(["3", "6"], "Passed", "")
chk("Test Results 'Passed' → all pass", v == {"3": "pass", "6": "pass"}, str(v))

# 8) 'did not pass' must read as FAIL, never pass
v, _ = verdicts(["9"], "", "Cart 9 did not pass.")
chk("'did not pass' → fail", v == {"9": "fail"}, str(v))

# 9) count/date/line numbers are NOT mistaken for carts
v, r = verdicts(["5"], "Tested up to 30 lbs", "See line 72")
chk("no spurious verdict from 'line 72' / '30 lbs'", v == {} and r is None, "%s / %r" % (v, r))

# 10) multiple carts under one verdict ('carts 9, 10 failed')
v, _ = verdicts(["7", "9", "10"], "", "Carts 9, 10 failed. Cart 7 passed.")
chk("carts 9,10 fail / 7 pass", v == {"9": "fail", "10": "fail", "7": "pass"}, str(v))

# 11) conflict in the same row (both pass and fail for one cart) → 'conflict'
v, _ = verdicts(["4"], "", "Cart 4 failed. Later cart 4 passed.")
chk("same-row pass+fail → conflict", v.get("4") == "conflict", str(v))

# 12) LEAD-list grouping: "passed: <list> failed: <list>" (the Weis 68 shape)
weis = ["1","2","3","5","6","7","8","9","10","11","12","13","18","20"]
v, _ = verdicts(weis, "See Note",
    "Carts passed: 2, 3, 5, 6, 7, 8, 10, 11, 13, 20 Carts failed: 1, 9, 12, 18 - out of tolerence")
chk("Weis68: only 1,9,12,18 fail", {c for c,x in v.items() if x=="fail"} == {"1","9","12","18"}, str(v))
chk("Weis68: listed pass carts are pass", v.get("2")=="pass" and v.get("20")=="pass", str(v))

# 13) "X Failed ... Y actually passed" in ONE clause → X fail, Y pass (ShopRite 297 7/24)
v, _ = verdicts(["19","26"], "", "Cart 19 Failed - out of maintenance tolerance Cart 26 actually passed.")
chk("297: cart 19 fail, cart 26 PASS", v == {"19":"fail","26":"pass"}, str(v))

# 14) literal 'Failed' in Test Results, single cart (ShopRite 297 9/25)
v, _ = verdicts(["17"], "Failed", "Failed due to fluctuating weight. Cart needs calibration.")
chk("297: cart 17 Failed (H column)", v == {"17":"fail"}, str(v))

# 15) date-serial cart cell → not acted on, flagged to fix
v, r = verdicts(["46029"], "", "Cart 1 failed")
chk("date-serial cart cell → no verdict + reason", v == {} and bool(r), "%s / %r" % (v, r))

# 16) hedged language ("please advise") → not auto-failed
v, r = verdicts(["23","32","47"], "",
    "Cart 47 wasn't able to recognize 80lbs. For cart 23 numbers out of tolerance. Please advise.")
chk("hedged 'please advise' → no auto-fail", not any(x=="fail" for x in v.values()) and bool(r), "%s / %r" % (v, r))

# 17) bare note numbers map to lettered row carts when unique (ShopRite 217: 5B/9A)
v, _ = verdicts(["5B","7B","9A","17A","22A"], "", "Carts 5, 9 failed. Others passed.")
chk("217: bare 5,9 → 5B,9A fail", v.get("5B")=="fail" and v.get("9A")=="fail", str(v))
chk("217: unnamed 'others' not marked fail", v.get("7B") != "fail", str(v))

print("\n".join("  ✓ " + p for p in PASS))
if FAIL:
    print("\nFAILURES:")
    for f in FAIL:
        print("  ✗", f)
print("\n  RESULT: %d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
