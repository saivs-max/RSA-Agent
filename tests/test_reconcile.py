#!/usr/bin/env python3
"""OFFLINE test for rsa_reconcile.py — proves the Truno→RSA reconciliation adds
missing carts, copies results, and creates NO duplicates (incl. idempotent re-runs).
No secrets / no network: gspread is replaced with an in-memory mutable fake.

Run:  python3 tests/test_reconcile.py
"""
import os, sys, types

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = next((d for d in (_HERE, os.path.dirname(_HERE))
             if os.path.exists(os.path.join(d, "rsa_sheets.py"))), _HERE)
sys.path.insert(0, ROOT)

REG = {}

class _Cell:
    def __init__(self, row, col, value=""):
        self.row, self.col, self.value = row, col, value

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
    def col_values(self, col):
        return [(r[col-1] if col-1 < len(r) else "") for r in REG.get((self.sid, self.name), [])]
    def cell(self, r, c):
        g = REG.get((self.sid, self.name), [])
        v = g[r-1][c-1] if r-1 < len(g) and c-1 < len(g[r-1]) else None
        return types.SimpleNamespace(value=v)

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

import rsa_sheets as S          # noqa: E402
import rsa_reconcile as R       # noqa: E402
import store_map as M           # noqa: E402

M.OVERRIDE_FILE = "/nonexistent_default.json"   # hermetic: earlier sections resolve to identity
M.CAPER_CSV = "/nonexistent_caper.csv"          # hermetic: no Caper file unless a section injects one

RID, TID = S.RSA_SPREADSHEET_ID, S.TRUNO_SPREADSHEET_ID
PASS, FAIL = [], []
def chk(n, c, extra=""): (PASS if c else FAIL).append(n + (f"  [{extra}]" if extra and not c else ""))
def pad(r, n=21): return list(r) + [""] * (n - len(r))


def seed():
    REG.clear()
    hdr = pad(["Cart ID", "Store", "State"])
    # One cart already present: ShopRite 113 cart 1
    r1 = pad(["P_WF_SHOPRITE_113_M3_001", "ShopRite #113 Wallington", "NJ", "Launch",
              "06/01/2026", "Yes", "TRUNO", "", "", "Scheduled RSA", "", "Yes",
              "Pending W&M", "", "Pending RSA", "5", "HW Ops", "06/01/2026", "", "", ""])
    REG[(RID, S.RSA_SHEET)] = [hdr, hdr, ["Cart ID", "Store", "State"], r1]

    # Truno rows
    def trow(store, cart, visit="", result="", status="", forms=""):
        r = [""] * 11
        r[S.T_STORE-1] = store; r[S.T_CART-1] = cart; r[S.T_VISIT-1] = visit
        r[R.T_TEST_RESULT-1] = result; r[S.T_STATUS-1] = status; r[S.T_FORMS-1] = forms
        return r
    REG[(TID, S.TRUNO_SHEET)] = [
        ["hdr"] + [""]*10,
        trow("ShopRite #113 Wallington", "1, 2", "06/10/2026", "Pass", "Completed", "Yes"),  # cart1 exists, cart2 new+pass
        trow("Sprouts 558", "1, 6", "06/03/2026", "", "Scheduled", "Yes"),                    # both new, scheduled
        trow("Sprouts 558", "1",   "06/03/2026", "Fail", "Completed", "Yes"),                 # cart1 again → must NOT duplicate
        trow("Kroger 250",  "5",   "06/05/2026", "Fail", "Completed", "Yes"),                 # new, fail
    ]


def cart_count():
    return len([r for r in S._rsa_data() if S._cell(r, S.C_CARTID)])


def serials():
    return [S._cell(r, S.C_CARTID) for r in S._rsa_data() if S._cell(r, S.C_CARTID)]


# ── Dry run writes nothing ──────────────────────────────────────────────────────
seed()
before = cart_count()
plan = R.reconcile(commit=False)
chk("dry-run plans adds", len(plan["add"]) >= 1, len(plan["add"]))
chk("dry-run writes NOTHING", cart_count() == before, cart_count())
chk("dry-run dedups cart already present (ShopRite 1)",
    not any(a["cart"] == "1" and "SHOPRITE_113" in a["serial"] for a in plan["add"]))
chk("dry-run plans Sprouts 558 cart 1 only once",
    sum(1 for a in plan["add"] if a["serial"].endswith("_M3_001") and "SPROUTS" in a["serial"].upper()) == 1)

# ── Commit ──────────────────────────────────────────────────────────────────────
seed()
plan = R.reconcile(commit=True)
sl = serials()
chk("ShopRite cart 2 added", any(s == "P_WF_SHOPRITE_113_M3_002" for s in sl), sl)
chk("ShopRite cart 1 NOT duplicated", sl.count("P_WF_SHOPRITE_113_M3_001") == 1)
chk("Sprouts 558 cart 1 added exactly once",
    len([s for s in sl if "SPROUTS_558_M3_001" in s.upper()]) == 1, sl)
chk("Sprouts 558 cart 6 added", any("SPROUTS_558_M3_006" in s.upper() for s in sl), sl)
chk("Kroger 250 cart 5 added", any("KROGER_250_M3_005" in s.upper() for s in sl), sl)
chk("no duplicate serials overall", len(sl) == len(set(sl)), sl)

# Results copied
q2 = S.query_cart("P_WF_SHOPRITE_113_M3_002")
chk("ShopRite cart 2 → Completed RSA (pass)", q2["rsaStatus"] == "Completed RSA", q2["rsaStatus"])
chk("ShopRite cart 2 → Passed overall (W&M out of scope)", q2["overallStatus"] == "Passed", q2["overallStatus"])
# read col U (RSA Test Results) directly
def colU(serial):
    for r in S._ws(RID, S.RSA_SHEET).get_all_values():
        if (r[0] if r else "") == serial:
            return r[S.C_RSA_RESULTS-1] if len(r) >= S.C_RSA_RESULTS else ""
    return ""
chk("ShopRite cart 2 result text in col U", "Pass" in colU("P_WF_SHOPRITE_113_M3_002"), colU("P_WF_SHOPRITE_113_M3_002"))

kfail = [s for s in sl if "KROGER_250_M3_005" in s.upper()][0]
chk("Kroger fail → Failed RSA", S.query_cart(kfail)["rsaStatus"] == "Failed RSA", S.query_cart(kfail)["rsaStatus"])

# Scheduled cart carries the visit date
spr6 = [s for s in sl if "SPROUTS_558_M3_006" in s.upper()][0]
chk("Sprouts scheduled → Scheduled RSA", S.query_cart(spr6)["rsaStatus"] == "Scheduled RSA", S.query_cart(spr6)["rsaStatus"])

# ── Idempotent re-run ────────────────────────────────────────────────────────────
n_after = cart_count()
plan2 = R.reconcile(commit=True)
chk("re-run adds 0 (idempotent)", len(plan2["add"]) == 0, len(plan2["add"]))
chk("re-run leaves cart count unchanged", cart_count() == n_after, cart_count())
chk("re-run creates no duplicate serials", len(serials()) == len(set(serials())))

# ── mirror: existing cart's status is overwritten to the LATEST Truno status ─────
seed()  # ShopRite 113 cart 1 exists as "Scheduled RSA"; Truno row A is Completed + Pass
R.reconcile(commit=True)   # mirror is the default
chk("mirror updated existing ShopRite cart 1 → Completed RSA (from Scheduled RSA)",
    S.query_cart("P_WF_SHOPRITE_113_M3_001")["rsaStatus"] == "Completed RSA",
    S.query_cart("P_WF_SHOPRITE_113_M3_001")["rsaStatus"])
chk("mirror filled col U on existing cart 1", "Pass" in colU("P_WF_SHOPRITE_113_M3_001"))
chk("--no-mirror leaves existing carts untouched",
    (seed() or True) and R.reconcile(commit=True, mirror=False) is not None
    and S.query_cart("P_WF_SHOPRITE_113_M3_001")["rsaStatus"] == "Scheduled RSA",
    S.query_cart("P_WF_SHOPRITE_113_M3_001")["rsaStatus"])

# ── Report ───────────────────────────────────────────────────────────────────────
# ── Address-resolved naming + dedup (store_map.resolve wired into reconcile) ─────
import json as _json, tempfile as _tf
_ov = _tf.NamedTemporaryFile(suffix=".json", delete=False); _ov.close()
_json.dump({"aliases": {"Weis 226 Selinsgrove": "Weis 226",          # existing in RSA → dedup
                        "Brand New Town 999": "Newbie Market 999"}}, # net-new → Caper name
           open(_ov.name, "w"))
M.OVERRIDE_FILE = _ov.name

REG.clear()
_hdr = pad(["Cart ID", "Store", "State"])
REG[(RID, S.RSA_SHEET)] = [_hdr, _hdr, _hdr,
    pad(["P_WEIS_226_M3_001", "Weis 226", "PA", "", "", "Yes", "TRUNO", "", "", "Scheduled RSA",
         "", "Yes", "Pending W&M", "", "Pending RSA", "", "HW Ops", "", "", "", ""])]
def _tr(store, cart, status, result="", forms="Yes"):
    r = [""] * 11
    r[S.T_STORE-1] = store; r[S.T_CART-1] = cart; r[S.T_STATUS-1] = status
    r[R.T_TEST_RESULT-1] = result; r[S.T_FORMS-1] = forms
    return r
REG[(TID, S.TRUNO_SHEET)] = [["h"] + [""]*10,
    _tr("Weis 226 Selinsgrove", "1", "Completed", "Pass"),   # → resolves to "Weis 226" → DEDUP
    _tr("Brand New Town 999",   "1", "Scheduled", "")]       # → resolves to "Newbie Market 999" → add
R.reconcile(commit=True)
_sl = serials()
chk("address-resolve DEDUPES the Weis dup (no Selinsgrove row, one Weis 226)",
    not any("SELINSGROVE" in s.upper() for s in _sl) and _sl.count("P_WEIS_226_M3_001") == 1, _sl)
_new = [s for s in _sl if "NEWBIE_MARKET_999" in s.upper()]
chk("net-new added under the CAPER deployment name", len(_new) == 1, _sl)
chk("net-new RSA store text = Caper name 'Newbie Market 999'",
    _new and S.query_cart(_new[0])["store"] == "Newbie Market 999",
    (S.query_cart(_new[0])["store"] if _new else None))

# ── latest-entry-wins + false-match guard + every-pair-covered ───────────────────
M.OVERRIDE_FILE = "/nonexistent_default.json"
REG.clear()
_h = pad(["Cart ID", "Store", "State"])
REG[(RID, S.RSA_SHEET)] = [_h, _h, _h,
    pad(["P_SPROUTS_5610_M3_003", "Sprouts 5610", "CA"])]   # DIFFERENT store (5610) already in RSA
def _t2(store, cart, visit, status, result="", forms="Yes"):
    r = [""] * 11
    r[S.T_STORE-1] = store; r[S.T_CART-1] = cart; r[S.T_VISIT-1] = visit
    r[S.T_STATUS-1] = status; r[R.T_TEST_RESULT-1] = result; r[S.T_FORMS-1] = forms
    return r
REG[(TID, S.TRUNO_SHEET)] = [["h"] + [""]*10,
    _t2("Sprouts 561", "3", "06/02/2026", "Completed", "Pass"),   # net-new; must NOT be absorbed by 5610
    _t2("Sprouts 917", "1", "06/01/2026", "Scheduled", ""),       # earlier entry
    _t2("Sprouts 917", "1", "06/15/2026", "Completed", "Pass")]   # LATER entry → should win
_plan = R.reconcile(commit=True)
_sl = serials()
chk("Sprouts 561 ADDED (false match to Sprouts 5610 guarded)", any("SPROUTS_561_M3_003" in s.upper() for s in _sl), _sl)
chk("Sprouts 5610 left intact", any("SPROUTS_5610" in s.upper() for s in _sl))
_917 = [s for s in _sl if "SPROUTS_917_M3_001" in s.upper()]
chk("Sprouts 917 added once (2 Truno rows deduped)", len(_917) == 1, _sl)
chk("Sprouts 917 uses the LATER row → Completed RSA",
    _917 and S.query_cart(_917[0])["rsaStatus"] == "Completed RSA",
    (S.query_cart(_917[0])["rsaStatus"] if _917 else None))
chk("totalPairs = 2 unique store+cart", _plan["totalPairs"] == 2, _plan["totalPairs"])
chk("every Truno pair covered (added+matched == total, 0 errors)",
    len(_plan["errors"]) == 0 and (len(_plan["add"]) + len(_plan["exists"])) == _plan["totalPairs"])

# ── check ALL Caper identifiers (name + store_id) before assuming net-new ────────
M.OVERRIDE_FILE = "/nonexistent_default.json"
# Inject Caper data: address → canonical name + store id, for two physical stores.
M.load_caper = lambda: [
    {"name": "Lyndhurst 113", "address": "540 New York Ave, Lyndhurst, NJ 07071",
     "store_id": "prod-wakefern-50", "number": "113"},
    {"name": "Brand New Mart 42", "address": "9 New Rd, Trenton, NJ 08601",
     "store_id": "prod-newco-42", "number": "42"}]
REG.clear()
_h = pad(["Cart ID", "Store", "State"])
REG[(RID, S.RSA_SHEET)] = [_h, _h, _h,
    # In RSA under the CAPER NAME (not the Truno label):
    pad(["P_WF_LYNDHURST_113_M3_001", "Lyndhurst 113", "NJ", "", "", "Yes", "TRUNO", "", "", "Scheduled RSA",
         "", "Yes", "Pending W&M", "", "Pending RSA", "", "HW Ops", "", "Added via RSA Agent bot", "", ""]),
    # In RSA identifiable only by the Caper STORE ID in its notes:
    pad(["P_X_M3_002", "Some Other Label", "NJ", "", "", "Yes", "TRUNO", "", "", "Scheduled RSA",
         "", "Yes", "Pending W&M", "", "Pending RSA", "", "HW Ops", "", "ref prod-newco-42", "", ""])]
def _ta(store, cart, addr, status="Scheduled"):
    r = [""] * 11
    r[S.T_STORE-1] = store; r[S.T_CART-1] = cart; r[R.T_ADDRESS - 1] = addr
    r[S.T_STATUS-1] = status; r[S.T_FORMS-1] = "Yes"
    return r
REG[(TID, S.TRUNO_SHEET)] = [["h"] + [""]*10,
    _ta("ShopRite Lyndhurst", "1", "540 New York Avenue Lyndhurst NJ 07071"),   # → Caper "Lyndhurst 113" → exists
    _ta("Newco Trenton", "2", "9 New Road Trenton NJ 08601")]                   # → store_id prod-newco-42 → exists
_n0 = cart_count()
_plan = R.reconcile(commit=True)
chk("Caper NAME match: nothing added for the Lyndhurst store", not any(e["cart"] == "1" for e in _plan["add"]), _plan["add"])
chk("Caper STORE_ID match: nothing added for the Newco store", not any(e["cart"] == "2" for e in _plan["add"]), _plan["add"])
chk("no new rows created (both matched by Caper identifiers)", cart_count() == _n0, cart_count())
chk("both recognised as existing", len(_plan["exists"]) == 2, _plan["exists"])

# ── WF-78 nomenclature: "WF 78" / "WF78" / "WF-78" all match; chain-precise ──────
# Caper names this Wakefern store just "Aberdeen" (no number) but RSA labels it WF 78.
M.OVERRIDE_FILE = "/nonexistent_default.json"
M.load_caper = lambda: [
    {"name": "Aberdeen", "address": "949 Beards Hill Rd, Aberdeen, MD 21001",
     "store_id": "prod-wakefern-78", "number": "78"}]
REG.clear()
_h = pad(["Cart ID", "Store", "State"])
REG[(RID, S.RSA_SHEET)] = [_h, _h, _h,
    pad(["P_WF_SHOPRITE_78_M3_001", "WF 78", "NJ", "", "", "Yes", "TRUNO", "", "", "Scheduled RSA",
         "", "Yes", "Pending W&M", "", "Pending RSA", "", "HW Ops", "", "", "", ""]),     # "WF 78"
    pad(["P_AAA_M3_002", "WF78", "NJ", "", "", "Yes", "TRUNO", "", "", "Scheduled RSA",
         "", "Yes", "Pending W&M", "", "Pending RSA", "", "HW Ops", "", "", "", ""]),      # "WF78"
    pad(["P_BBB_M3_003", "WF-78", "NJ", "", "", "Yes", "TRUNO", "", "", "Scheduled RSA",
         "", "Yes", "Pending W&M", "", "Pending RSA", "", "HW Ops", "", "", "", ""]),      # "WF-78"
    pad(["P_SPROUTS_78_M3_009", "Sprouts 78", "CA", "", "", "Yes", "TRUNO", "", "", "Scheduled RSA",
         "", "Yes", "Pending W&M", "", "Pending RSA", "", "HW Ops", "", "", "", ""])]      # different chain, #78
_addr = "949 Beards Hill Road Aberdeen MD 21001"
REG[(TID, S.TRUNO_SHEET)] = [["h"] + [""]*10,
    _ta("ShopRite Aberdeen", "1", _addr),    # → Caper "Aberdeen" #78 → matches "WF 78"
    _ta("ShopRite Aberdeen", "2", _addr),    # → matches "WF78"
    _ta("ShopRite Aberdeen", "3", _addr),    # → matches "WF-78"
    _ta("ShopRite Aberdeen", "9", _addr)]    # → "Sprouts 78" is a DIFFERENT chain → net-new
_n0 = cart_count()
_plan = R.reconcile(commit=True)
_ex = {e["cart"]: e["serial"] for e in _plan["exists"]}
chk("WF 78  (space) matched for cart 1", _ex.get("1") == "P_WF_SHOPRITE_78_M3_001", _ex.get("1"))
chk("WF78   (none)  matched for cart 2", _ex.get("2") == "P_AAA_M3_002", _ex.get("2"))
chk("WF-78  (dash)  matched for cart 3", _ex.get("3") == "P_BBB_M3_003", _ex.get("3"))
chk("carts 1/2/3 NOT added (already present as WF-78)", not any(a["cart"] in {"1","2","3"} for a in _plan["add"]), _plan["add"])
_n9 = [a for a in _plan["add"] if a["cart"] == "9"]
chk("cross-chain: 'Sprouts 78' NOT mistaken for WF #78 → cart 9 net-new", len(_n9) == 1, _plan["add"])
chk("net-new cart 9 reuses the existing WF-78 store (not the dirty 'Aberdeen' Caper text)",
    _n9 and "ABERDEEN" not in _n9[0]["serial"].upper() and "_78_" in _n9[0]["serial"]
    and _n9[0]["serial"].upper().endswith("_M3_009"), _n9)
chk("Sprouts 78 row left intact", "P_SPROUTS_78_M3_009" in serials())

# ── dashboard OVERALL (col O) corrected even when the RSA status is already right ─
M.OVERRIDE_FILE = "/nonexistent_default.json"
M.load_caper = lambda: []          # force name/number matching (no Caper file)
REG.clear()
_h = pad(["Cart ID", "Store", "State"])
REG[(RID, S.RSA_SHEET)] = [_h, _h, _h,
    # RSA status already "Completed RSA" but the dashboard OVERALL is STALE "Pending RSA"
    pad(["P_WF_SHOPRITE_113_M3_009", "WF 113 - Wallington NJ", "NJ", "", "", "Yes", "TRUNO", "", "",
         "Completed RSA", "", "Yes", "Pending W&M", "", "Pending RSA", "", "HW Ops", "", "", "", ""])]
def _tov(store, cart, status, result=""):
    r = [""] * 11
    r[S.T_STORE-1] = store; r[S.T_CART-1] = cart; r[S.T_STATUS-1] = status
    r[R.T_TEST_RESULT-1] = result; r[S.T_FORMS-1] = "Yes"
    return r
REG[(TID, S.TRUNO_SHEET)] = [["h"] + [""]*10, _tov("ShopRite 113", "9", "Completed", "Pass")]
_plan = R.reconcile(commit=True)
def colO(serial):
    for r in S._ws(RID, S.RSA_SHEET).get_all_values():
        if (r[0] if r else "") == serial:
            return r[S.C_OVERALL-1] if len(r) >= S.C_OVERALL else ""
    return ""
chk("dashboard overall corrected: stale 'Pending RSA' → 'Passed'",
    colO("P_WF_SHOPRITE_113_M3_009") == "Passed", colO("P_WF_SHOPRITE_113_M3_009"))
chk("report records overall from→to for the dashboard",
    any(u["serial"] == "P_WF_SHOPRITE_113_M3_009" and u.get("overallFrom") == "Pending RSA"
        and u.get("overallTo") == "Passed" for u in _plan["updated"]), _plan["updated"])
chk("derive: pass → (Completed RSA, Passed)", R._derive_status(True, "") == ("Completed RSA", "Passed"))
chk("derive: fail → (Failed RSA, Failed)", R._derive_status(False, "Completed") == ("Failed RSA", "Failed"))
chk("derive: scheduled → (Scheduled RSA, Pending RSA)", R._derive_status(None, "Scheduled") == ("Scheduled RSA", "Pending RSA"))

# ── clean net-new names (banner+#) · no number-fallback · forward-only mirror ────
M.OVERRIDE_FILE = "/nonexistent_default.json"
M.load_caper = lambda: [   # only Rosauers #29 in Caper, at a FAR address — must NOT lure Sprouts 029
    {"name": "Rosauers 29th Ave", "address": "1907 W Northwest Blvd, Spokane, WA 99205",
     "store_id": "prod-rosauers-29", "number": "29"}]
REG.clear()
_h = pad(["Cart ID", "Store", "State"])
REG[(RID, S.RSA_SHEET)] = [_h, _h, _h,
    # already PASSED — a stale Truno 'Scheduled/Untested' must NOT drag it backward
    pad(["P_WF_SHOPRITE_549_M3_008", "WF 60 - ShopRite 549", "PA", "", "", "Yes", "TRUNO", "", "",
         "Completed RSA", "06/01/2026", "Yes", "Passed W&M", "06/02/2026", "Passed", "", "HW Ops", "", "", "Tested up to 60 lbs", ""])]
def _tc(store, code, cart, status, result="", addr=""):
    r = [""] * 11
    r[S.T_STORE-1] = store; r[R.T_TRUNO_CODE-1] = code; r[S.T_CART-1] = cart
    r[S.T_STATUS-1] = status; r[R.T_TEST_RESULT-1] = result; r[R.T_ADDRESS-1] = addr; r[S.T_FORMS-1] = "Yes"
    return r
REG[(TID, S.TRUNO_SHEET)] = [["h"] + [""]*10,
    _tc("WF- Shoprite 116", "ISC0050-WKF-0116", "11", "Completed", "Tested up to 30 lbs", "20 Main St Fair Lawn NJ 07410"),
    _tc("Sprouts 029",      "ISC0050-SFM-0029", "10", "Completed", "Tested up to 30 lbs", "10 Vegas Blvd Las Vegas NV 89117"),
    _tc("WF- Shoprite 549", "ISC0050-WKF-0549", "8",  "Scheduled", "Untested", "5 Festival Rd Columbia SC 29200")]
_plan = R.reconcile(commit=True)
_sl = serials()
_nm = {a["cart"]: a["store"] for a in _plan["add"]}
chk("net-new WF store = 'ShopRite 116' (banner+#, not the dirty 'Fairlawn Store 116')", _nm.get("11") == "ShopRite 116", _nm.get("11"))
chk("net-new Sprouts store = 'Sprouts 29' (banner+#)", _nm.get("10") == "Sprouts 29", _nm.get("10"))
chk("no number-fallback: Sprouts 029 NOT mis-mapped to 'Rosauers 29th Ave'",
    not any("ROSAUERS" in s.upper() for s in _sl) and not any(a["store"] == "Rosauers 29th Ave" for a in _plan["add"]), _sl)
chk("forward-only: passed cart NOT regressed to Scheduled",
    S.query_cart("P_WF_SHOPRITE_549_M3_008")["rsaStatus"] == "Completed RSA", S.query_cart("P_WF_SHOPRITE_549_M3_008")["rsaStatus"])
chk("forward-only: overall 'Passed' preserved (not knocked back)",
    S.query_cart("P_WF_SHOPRITE_549_M3_008")["overallStatus"] == "Passed", S.query_cart("P_WF_SHOPRITE_549_M3_008")["overallStatus"])
chk("forward-only: 'Untested' not written over the real result", "Untested" not in colU("P_WF_SHOPRITE_549_M3_008"))
_st = {a["cart"]: a["state"] for a in _plan["add"]}
chk("state from address: Fair Lawn NJ 07410 → NJ", _st.get("11") == "NJ", _st.get("11"))
chk("state from address: Las Vegas NV 89117 → NV (not defaulted to NJ)", _st.get("10") == "NV", _st.get("10"))
chk("_state_from_addr: '…Cardiff, MD 21160' → MD", R._state_from_addr("1606 Dooley Rd, Cardiff, MD 21160") == "MD")
chk("_state_from_addr: no comma '…Philadelphia PA 19128' → PA", R._state_from_addr("6301 Ridge Ave Philadelphia PA 19128") == "PA")
chk("_state_from_addr: blank → None", R._state_from_addr("") is None)
chk("--exact-mirror CAN regress (opt-in)", True if (
    (REG.__setitem__((RID, S.RSA_SHEET), [_h, _h, _h,
        pad(["P_WF_SHOPRITE_549_M3_008", "WF 60 - ShopRite 549", "PA", "", "", "Yes", "TRUNO", "", "",
             "Completed RSA", "", "Yes", "", "", "Passed", "", "", "", "", "", ""])]) or True)
    and (REG.__setitem__((TID, S.TRUNO_SHEET), [["h"]+[""]*10,
        _tc("WF- Shoprite 549", "ISC0050-WKF-0549", "8", "Scheduled", "", "5 Festival Rd Columbia SC 29200")]) or True)
    and R.reconcile(commit=True, forward_only=False) is not None
    and S.query_cart("P_WF_SHOPRITE_549_M3_008")["rsaStatus"] == "Scheduled RSA") else False,
    S.query_cart("P_WF_SHOPRITE_549_M3_008")["rsaStatus"])

# ── Weis Selinsgrove: address bridges Truno/RSA #226 vs Caper #1 ──────────────────
M.OVERRIDE_FILE = "/nonexistent_default.json"
M.load_caper = lambda: [
    {"name": "Weis Markets Selinsgrove", "address": "719 Route 522 Selinsgrove PA 17870",
     "store_id": "prod-weis-1", "number": "1"}]
REG.clear()
_h = pad(["Cart ID", "Store", "State"])
REG[(RID, S.RSA_SHEET)] = [_h, _h, _h,
    pad(["P_WEIS_WEIS_226_M3_002A", "Weis 226", "PA", "", "", "Yes", "TRUNO", "", "", "Scheduled RSA",
         "", "Yes", "Pending W&M", "", "Pending RSA", "", "HW Ops", "", "", "", ""])]
REG[(TID, S.TRUNO_SHEET)] = [["h"] + [""]*10,
    _tc("Weis Markets Selinsgrove", "ISC0050-WEI-0226", "2", "Completed", "Tested up to 60 lbs", "719 Route 522 Selinsgrove PA 17870")]
_plan = R.reconcile(commit=True)
chk("Weis: matched existing 'Weis 226' (NOT net-new 'Weis 1') via #226 + address",
    any(e["serial"] == "P_WEIS_WEIS_226_M3_002A" for e in _plan["exists"]) and not _plan["add"], _plan["add"])

for p in PASS: print("  ✓", p)
if FAIL:
    print("\nFAILURES:")
    for f in FAIL: print("  ✗", f)
print("\n" + "=" * 60)
print(f"  RESULT: {len(PASS)} passed, {len(FAIL)} failed")
print("=" * 60)
sys.exit(1 if FAIL else 0)
