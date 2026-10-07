#!/usr/bin/env python3
"""OFFLINE tests for truno_monitor.py — address-first matching, scheduled/cancelled writes,
date-cart skip, idempotency/baseline, ambiguity. No secrets / no network (gspread faked).

Run:  python3 tests/test_truno_offline.py
"""
import os, sys, types, json, tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = next((d for d in (_HERE, os.path.dirname(_HERE))
             if os.path.exists(os.path.join(d, "rsa_sheets.py"))), _HERE)
sys.path.insert(0, ROOT)

REG = {}
class _Cell:
    def __init__(self, row, col, value=""): self.row, self.col, self.value = row, col, value
def _grow(key, row, col):
    g = REG.setdefault(key, [])
    while len(g) < row: g.append([])
    r = g[row - 1]
    while len(r) < col: r.append("")
class _WS:
    def __init__(self, sid, name): self.sid, self.name = sid, name
    def get_all_values(self): return [list(r) for r in REG.get((self.sid, self.name), [])]
    def update_cells(self, cells):
        for c in cells:
            _grow((self.sid, self.name), c.row, c.col)
            REG[(self.sid, self.name)][c.row - 1][c.col - 1] = c.value
    def col_values(self, col): return [(r[col-1] if col-1 < len(r) else "") for r in REG.get((self.sid, self.name), [])]
    def cell(self, r, c):
        g = REG.get((self.sid, self.name), [])
        return types.SimpleNamespace(value=(g[r-1][c-1] if r-1 < len(g) and c-1 < len(g[r-1]) else None))
class _Book:
    def __init__(self, sid): self.sid = sid
    def worksheet(self, n): return _WS(self.sid, n)
class _GC:
    def open_by_key(self, sid): return _Book(sid)
gspread = types.ModuleType("gspread"); gspread.Cell = _Cell; gspread.authorize = lambda c: _GC()
gspread.exceptions = types.SimpleNamespace(APIError=type("APIError", (Exception,), {}))
sys.modules["gspread"] = gspread
g = types.ModuleType("google"); go = types.ModuleType("google.oauth2")
gsa = types.ModuleType("google.oauth2.service_account")
gsa.Credentials = type("C", (), {"from_service_account_file": staticmethod(lambda *a, **k: object())})
sys.modules.update({"google": g, "google.oauth2": go, "google.oauth2.service_account": gsa})
dv = types.ModuleType("dotenv"); dv.load_dotenv = lambda *a, **k: None; sys.modules["dotenv"] = dv

import rsa_sheets as S          # noqa: E402
import store_map as M           # noqa: E402
import truno_monitor as TM      # noqa: E402

M.OVERRIDE_FILE = "/nonexistent.json"
S.TRUNO_MAP_FILE = "/nonexistent.json"          # no explicit map by default → exercise inference
_pf = tempfile.NamedTemporaryFile(suffix=".json", delete=False); _pf.close()
TM.PROCESSED_FILE = _pf.name

RID, TID = S.RSA_SPREADSHEET_ID, S.TRUNO_SPREADSHEET_ID
PASS, FAIL = [], []
def chk(n, c, extra=""): (PASS if c else FAIL).append(n + (f"  [{extra}]" if extra and not c else ""))
def pad(r, n=23): return list(r) + [""] * (n - len(r))

# Caper deployment: the store is resolvable ONLY by ADDRESS (name/code carry no number).
M.load_caper = lambda: [
    {"name": "Sprouts 917", "address": "10 Vegas Blvd, Las Vegas, NV 89117", "store_id": "x", "number": "917"},
]

def rsa(cid, store, status="Pending RSA", sched="", vendor="TRUNO", dri="HW Ops"):
    r = [""] * 23
    r[S.C_CARTID-1]=cid; r[S.C_STORE-1]=store; r[S.C_RSASTATUS-1]=status
    r[S.C_RSASCHED-1]=sched; r[S.C_RSAVENDOR-1]=vendor; r[S.C_DRI-1]=dri
    return r
def trow(store, code, addr, cart, visit="", status=""):
    r = [""] * 13
    r[S.T_STORE-1]=store; r[M.T_TRUNO_CODE-1]=code; r[S.T_ADDRESS-1]=addr
    r[S.T_CART-1]=cart; r[S.T_VISIT-1]=visit; r[S.T_STATUS-1]=status
    return r
def colv(serial, col):
    for r in S._ws(RID, S.RSA_SHEET).get_all_values():
        if (r[0] if r else "") == serial:
            return r[col-1] if len(r) >= col else ""
    return ""
def seed_rsa():
    h = pad(["Cart ID", "Store", "State"])
    REG[(RID, S.RSA_SHEET)] = [h, h, h,
        rsa("P_SPROUTS_917_M3_001", "Sprouts 917"),
        rsa("P_SPROUTS_917_M3_003", "Sprouts 917", status="Scheduled RSA", sched="06/01/2026")]
def set_truno(rows): REG[(TID, S.TRUNO_SHEET)] = [["h"]*13] + rows
def reset_processed(seed_done=True):
    json.dump(["__baselined__"] if seed_done else [], open(TM.PROCESSED_FILE, "w"))
def cap_notify():
    msgs = []
    return msgs, (lambda m: msgs.append(m))

# ── 1. ADDRESS-primary scheduled (name/code have no number; only the address resolves 917) ──
seed_rsa(); reset_processed()
set_truno([trow("Sprouts Vegas", "ISC0050-SFM", "10 Vegas Blvd Las Vegas NV 89117", "1", "06/20/2026", "Scheduled")])
msgs, notify = cap_notify()
res = TM.process_truno(notify=notify)
chk("scheduled: cart 1 → Scheduled RSA", colv("P_SPROUTS_917_M3_001", S.C_RSASTATUS) == "Scheduled RSA", colv("P_SPROUTS_917_M3_001", S.C_RSASTATUS))
chk("scheduled: visit date written", colv("P_SPROUTS_917_M3_001", S.C_RSASCHED) == "06/20/2026", colv("P_SPROUTS_917_M3_001", S.C_RSASCHED))
chk("scheduled: vendor TRUNO", colv("P_SPROUTS_917_M3_001", S.C_RSAVENDOR) == "TRUNO")
chk("scheduled: resolved by ADDRESS (not name/code)", any("matched by address" in m for m in msgs), msgs)
chk("scheduled: other cart untouched", colv("P_SPROUTS_917_M3_003", S.C_RSASTATUS) == "Scheduled RSA")

# ── 2. CANCELLED resets + clears the scheduled date ──
seed_rsa(); reset_processed()
set_truno([trow("Sprouts Vegas", "ISC0050-SFM", "10 Vegas Blvd Las Vegas NV 89117", "3", "", "Cancelled")])
msgs, notify = cap_notify()
TM.process_truno(notify=notify)
chk("cancelled: cart 3 → Pending RSA", colv("P_SPROUTS_917_M3_003", S.C_RSASTATUS) == "Pending RSA", colv("P_SPROUTS_917_M3_003", S.C_RSASTATUS))
chk("cancelled: scheduled date cleared", colv("P_SPROUTS_917_M3_003", S.C_RSASCHED) == "")
chk("cancelled: DRI pinged + tracker link", any("Cancelled" in m and "Open in tracker" in m for m in msgs))

# ── 3. DATE in the Cart cell → skipped, never acted on ──
seed_rsa(); reset_processed()
set_truno([trow("Sprouts Vegas", "ISC0050-SFM", "10 Vegas Blvd Las Vegas NV 89117", "Sat Jan 03 2026 00:00:00", "", "Cancelled")])
msgs, notify = cap_notify()
res = TM.process_truno(notify=notify)
chk("date-cart: skipped (counted)", res.get("skipped") == 1, res)
chk("date-cart: no status change", colv("P_SPROUTS_917_M3_001", S.C_RSASTATUS) == "Pending RSA")
chk("date-cart: no cancellation alert", not any("Cancelled" in m for m in msgs))

# ── 4. IDEMPOTENT: re-running the same scheduled row acts 0 ──
seed_rsa(); reset_processed()
set_truno([trow("Sprouts Vegas", "ISC0050-SFM", "10 Vegas Blvd Las Vegas NV 89117", "1", "06/20/2026", "Scheduled")])
TM.process_truno(notify=None)
msgs2, notify2 = cap_notify()
res2 = TM.process_truno(notify=notify2)
chk("idempotent: 2nd pass schedules 0", res2.get("scheduled", 0) == 0, res2)
chk("idempotent: 2nd pass silent", msgs2 == [], msgs2)

# ── 5. BASELINE: first run (empty cache) snapshots, writes nothing, alerts nothing ──
seed_rsa(); reset_processed(seed_done=False)
set_truno([trow("Sprouts Vegas", "ISC0050-SFM", "10 Vegas Blvd Las Vegas NV 89117", "1", "06/20/2026", "Scheduled")])
msgs, notify = cap_notify()
res = TM.process_truno(notify=notify)
chk("baseline: flagged as baseline run", res.get("baseline") is True, res)
chk("baseline: no write (cart 1 stays Pending RSA)", colv("P_SPROUTS_917_M3_001", S.C_RSASTATUS) == "Pending RSA")
chk("baseline: no alerts", msgs == [])
# an identical re-run does nothing (the row was baselined as handled), but a NEW transition DOES act
set_truno([trow("Sprouts Vegas", "ISC0050-SFM", "10 Vegas Blvd Las Vegas NV 89117", "3", "", "Cancelled")])
msgs, notify = cap_notify()
TM.process_truno(notify=notify)
chk("post-baseline: a NEW transition acts (cart 3 cancelled → Pending RSA)",
    colv("P_SPROUTS_917_M3_003", S.C_RSASTATUS) == "Pending RSA", colv("P_SPROUTS_917_M3_003", S.C_RSASTATUS))

# ── 6. AMBIGUOUS cart → flag, no write ──
seed_rsa(); reset_processed()
set_truno([trow("Sprouts Vegas", "ISC0050-SFM", "10 Vegas Blvd Las Vegas NV 89117", "", "", "Cancelled")])  # no cart, store has 2
msgs, notify = cap_notify()
res = TM.process_truno(notify=notify)
chk("ambiguous: flagged for confirmation", any("needs confirmation" in m for m in msgs), msgs)
chk("ambiguous: no status change on cart 1", colv("P_SPROUTS_917_M3_001", S.C_RSASTATUS) == "Pending RSA")

# ── 7. RESCHEDULE re-fires (cancel clears the prior 'scheduled' key) ──
seed_rsa(); reset_processed()
addr = "10 Vegas Blvd Las Vegas NV 89117"
set_truno([trow("Sprouts Vegas", "ISC0050-SFM", addr, "1", "06/20/2026", "Scheduled")])
TM.process_truno(notify=None)                                  # schedule (processed)
set_truno([trow("Sprouts Vegas", "ISC0050-SFM", addr, "1", "", "Cancelled")])
TM.process_truno(notify=None)                                  # cancel → clears 'scheduled' key
set_truno([trow("Sprouts Vegas", "ISC0050-SFM", addr, "1", "07/15/2026", "Scheduled")])
msgs, notify = cap_notify()
res = TM.process_truno(notify=notify)
chk("reschedule: re-fires after a cancel", res.get("scheduled", 0) == 1, res)
chk("reschedule: new date written", colv("P_SPROUTS_917_M3_001", S.C_RSASCHED) == "07/15/2026", colv("P_SPROUTS_917_M3_001", S.C_RSASCHED))

# ── 8. PARTIAL MATCH: cart 2 schedules; cart 13A has no RSA cart (the Weis 226 case) ──
S.GAS_WEB_APP_URL = "https://fake.example/exec"    # so the link deep-links into the WEBAPP
h = pad(["Cart ID", "Store", "State"])
REG[(RID, S.RSA_SHEET)] = [h, h, h, rsa("P_WEIS_WEIS_226_M3_002A", "Weis 226 Selinsgrove")]
reset_processed()
set_truno([trow("Weis Markets Selinsgrove 226", "ISC0050-WEI-0226",
                "1 Market St, Selinsgrove, PA 17870", "2, 13A", "06/25/2026", "Scheduled")])
msgs, notify = cap_notify()
TM.process_truno(notify=notify)
blob = " ".join(msgs)
chk("partial: cart 2 matched → Scheduled RSA on _002A",
    colv("P_WEIS_WEIS_226_M3_002A", S.C_RSASTATUS) == "Scheduled RSA", colv("P_WEIS_WEIS_226_M3_002A", S.C_RSASTATUS))
chk("partial: visit date written to the matched cart", colv("P_WEIS_WEIS_226_M3_002A", S.C_RSASCHED) == "06/25/2026")
chk("partial: a confirmation is posted for the leftover cart", any("needs confirmation" in m for m in msgs), msgs)
chk("partial: cart 13A reported as 'no RSA cart found' (not padded with _002A)",
    "no RSA cart found for cart *13A*" in blob, blob)
chk("partial: the matched cart is shown as already handled, not a 'couldn't match' candidate",
    "already scheduled `P_WEIS_WEIS_226_M3_002A`" in blob, blob)
chk("partial: link is the WEBAPP deep-link, not the raw sheet",
    "fake.example/exec?cart=" in blob and "docs.google.com" not in blob, blob)
S.GAS_WEB_APP_URL = ""

# ── 9. EXCLUSION: a serial matched to one cart is never offered as a candidate for another ──
REG[(RID, S.RSA_SHEET)] = [h, h, h,
    rsa("P_WEIS_WEIS_226_M3_002",  "Weis 226 Selinsgrove"),
    rsa("P_WEIS_WEIS_226_M3_002A", "Weis 226 Selinsgrove")]
reset_processed()
set_truno([trow("Weis 226", "ISC0050-WEI-0226", "1 Market St, Selinsgrove, PA 17870", "2, 2A", "06/25/2026", "Scheduled")])
msgs, notify = cap_notify()
TM.process_truno(notify=notify)
blob = " ".join(msgs)
chk("exclusion: cart 2A uniquely scheduled on _002A",
    colv("P_WEIS_WEIS_226_M3_002A", S.C_RSASTATUS) == "Scheduled RSA", colv("P_WEIS_WEIS_226_M3_002A", S.C_RSASTATUS))
chk("exclusion: cart 2 still flagged with its real tie (_002)",
    "cart *2* could be" in blob and "P_WEIS_WEIS_226_M3_002`" in blob, blob)
chk("exclusion: the already-matched _002A is NOT offered as a candidate for cart 2",
    "could be `P_WEIS_WEIS_226_M3_002A`" not in blob, blob)

# ── 10. EXPLICIT MAP: monitor uses the RSA↔Truno map written at row creation ──
# The Truno store name/code here are deliberately un-inferable, so ONLY the explicit map
# can resolve it — proving the map-first path.
_mapf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
_addr = "77 Mapped Ave, Nowhere, ZZ 00000"
json.dump({M.addr_key(_addr): {"store": "ShopRite 999", "address": _addr,
          "carts": {"5": "P_WF_SHOPRITE_999_M3_005"}}}, _mapf); _mapf.close()
S.TRUNO_MAP_FILE = _mapf.name
h = pad(["Cart ID", "Store", "State"])
REG[(RID, S.RSA_SHEET)] = [h, h, h, rsa("P_WF_SHOPRITE_999_M3_005", "ShopRite 999")]
reset_processed()
set_truno([trow("Totally Different Truno Name", "", _addr, "5", "07/01/2026", "Scheduled")])
msgs, notify = cap_notify()
TM.process_truno(notify=notify)
chk("map: exact match schedules the mapped serial",
    colv("P_WF_SHOPRITE_999_M3_005", S.C_RSASTATUS) == "Scheduled RSA", colv("P_WF_SHOPRITE_999_M3_005", S.C_RSASTATUS))
chk("map: message says matched by map", any("matched by map" in m for m in msgs), msgs)
S.TRUNO_MAP_FILE = "/nonexistent.json"
try: os.unlink(_mapf.name)
except Exception: pass

# ── 11. NOT FOUND: a scheduled row that maps to nothing → alert, write nothing ──
REG[(RID, S.RSA_SHEET)] = [h, h, h, rsa("P_SPROUTS_917_M3_001", "Sprouts 917")]
reset_processed()
set_truno([trow("Ghost Store 404", "ISC0050-GHO-0404", "999 Nowhere Rd", "1", "07/01/2026", "Scheduled")])
msgs, notify = cap_notify()
res = TM.process_truno(notify=notify)
chk("not-found: nothing scheduled", colv("P_SPROUTS_917_M3_001", S.C_RSASTATUS) == "Pending RSA",
    colv("P_SPROUTS_917_M3_001", S.C_RSASTATUS))
chk("not-found: alert posted to Slack", any("not found in the RSA Tracker" in m for m in msgs), msgs)
chk("not-found: counted in the summary", res.get("notFound") == 1, res)
# second pass with the same row does not re-alert (idempotent)
msgs2, notify2 = cap_notify()
TM.process_truno(notify=notify2)
chk("not-found: does not re-alert on the next pass", not any("not found" in m for m in msgs2), msgs2)

# ── 12. All monitor notifications default to the ops channel ──
chk("notifications default to channel C0ARJQWG7RP", TM.MONITOR_CHANNEL == "C0ARJQWG7RP", TM.MONITOR_CHANNEL)

# ── report ──
for p in PASS: print("  ✓", p)
if FAIL:
    print("\nFAILURES:")
    for f in FAIL: print("  ✗", f)
print("\n" + "=" * 60)
print(f"  RESULT: {len(PASS)} passed, {len(FAIL)} failed")
print("=" * 60)
try: os.unlink(TM.PROCESSED_FILE)
except Exception: pass
sys.exit(1 if FAIL else 0)
