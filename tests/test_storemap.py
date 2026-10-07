#!/usr/bin/env python3
"""OFFLINE test for store_map.py — address normalization/keys, the address-first
Truno↔Caper↔RSA join (incl. the Weis 226 dedup), Truno-code parsing, alias
resolution, and store_hint matching in test_forms.apply_test_form. No network.

Run:  python3 tests/test_storemap.py
"""
import os, sys, types, json, tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = next((d for d in (_HERE, os.path.dirname(_HERE))
             if os.path.exists(os.path.join(d, "rsa_sheets.py"))), _HERE)
sys.path.insert(0, ROOT)

REG = {}
class _Cell:
    def __init__(self, row, col, value=""): self.row, self.col, self.value = row, col, value
def _grow(k, row, col):
    g = REG.setdefault(k, [])
    while len(g) < row: g.append([])
    r = g[row-1]
    while len(r) < col: r.append("")
class _WS:
    def __init__(self, sid, name): self.sid, self.name = sid, name
    def get_all_values(self): return [list(r) for r in REG.get((self.sid, self.name), [])]
    def update_cells(self, cells):
        for c in cells:
            _grow((self.sid, self.name), c.row, c.col)
            REG[(self.sid, self.name)][c.row-1][c.col-1] = c.value
    def col_values(self, col): return [(r[col-1] if col-1 < len(r) else "") for r in REG.get((self.sid, self.name), [])]
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
sys.modules["gspread"] = gspread
g = types.ModuleType("google"); go = types.ModuleType("google.oauth2")
gsa = types.ModuleType("google.oauth2.service_account")
gsa.Credentials = type("C", (), {"from_service_account_file": staticmethod(lambda *a, **k: object())})
sys.modules.update({"google": g, "google.oauth2": go, "google.oauth2.service_account": gsa})
openai = types.ModuleType("openai"); openai.OpenAI = type("O", (), {"__init__": lambda s, *a, **k: None})
openai.APIConnectionError = type("APIConnectionError", (Exception,), {}); openai.APITimeoutError = type("APITimeoutError", (openai.APIConnectionError,), {})
sys.modules["openai"] = openai

import rsa_sheets as S        # noqa: E402
import store_map as M         # noqa: E402
import test_forms as TF       # noqa: E402

PASS, FAIL = [], []
def chk(n, c, extra=""): (PASS if c else FAIL).append(n + (f"  [{extra}]" if extra and not c else ""))
def pad(r, n=21): return list(r) + [""] * (n - len(r))

# ── Address normalization → stable keys despite formatting differences ───────────
chk("addr_key: St vs Street + zip → same key",
    M.addr_key("123 Main St, Wallington, NJ 07057") == M.addr_key("123 Main Street Wallington NJ 07057"),
    (M.addr_key("123 Main St, Wallington, NJ 07057"), M.addr_key("123 Main Street Wallington NJ 07057")))
chk("addr_key: Blvd vs Boulevard → same key",
    M.addr_key("100 Commerce Blvd, Selinsgrove PA 17870") == M.addr_key("100 Commerce Boulevard Selinsgrove, PA 17870"))
chk("addr_key: different houses → different keys",
    M.addr_key("100 Commerce Blvd 17870") != M.addr_key("200 Commerce Blvd 17870"))

# ── parse_truno_code ─────────────────────────────────────────────────────────────
chk("parse ISC0050-WKF-0113 → (WKF,113)", M.parse_truno_code("ISC0050-WKF-0113") == ("WKF", "113"))
chk("parse ISC0050-WEI-0226 → (WEI,226)", M.parse_truno_code("ISC0050-WEI-0226") == ("WEI", "226"))

# ── build(): address-first Truno ↔ Caper ↔ RSA ───────────────────────────────────
def trow(name, code, addr):
    r = [""] * 11
    r[S.T_STORE-1] = name; r[M.T_TRUNO_CODE-1] = code; r[M.T_ADDRESS-1] = addr
    return r

truno_rows = [
    trow("Weis 226 Selinsgrove", "ISC0050-WEI-0226", "100 Commerce Blvd, Selinsgrove, PA 17870"),
    trow("Sprouts 505",          "ISC0050-SFM-0505", "200 Market St, Los Angeles, CA 90001"),
    trow("Kroger 250",           "ISC0050-KRG-0250", "300 Oak Ave, Atlanta, GA 30301"),   # net-new
    trow("Mystery Store",        "",                 ""),                                 # no address
]
caper = [
    {"name": "Weis 226",   "address": "100 Commerce Boulevard Selinsgrove PA 17870"},   # diff format, same addr
    {"name": "Sprouts 505", "address": "200 Market Street Los Angeles CA 90001"},
    {"name": "Kroger 250", "address": "300 Oak Avenue Atlanta GA 30301"},
]
rsa_rows = [
    pad(["P_WEIS_226_M3_001", "Weis 226", "PA"]),       # the real Weis 226 already in RSA
    pad(["P_SPROUTS_505_M3_001", "Sprouts 505", "CA"]),
    # note: NO Kroger 250 in RSA → should surface as net-new
]
m = {e["trunoIC"]: e for e in M.build(truno_rows=truno_rows, caper=caper, rsa_rows=rsa_rows)}

chk("Weis matched on ADDRESS to canonical 'Weis 226'",
    m["Weis 226 Selinsgrove"]["status"] == "matched"
    and m["Weis 226 Selinsgrove"]["basis"] == "address"
    and m["Weis 226 Selinsgrove"]["caper"] == "Weis 226"
    and m["Weis 226 Selinsgrove"]["rsa"] == "Weis 226",
    m["Weis 226 Selinsgrove"])
chk("Sprouts 505 matched on address", m["Sprouts 505"]["status"] == "matched")
chk("Kroger 250 flagged NET-NEW (in Caper, not in RSA)", m["Kroger 250"]["status"] == "matched_no_rsa",
    m["Kroger 250"]["status"])
chk("Mystery (no address) flagged", m["Mystery Store"]["status"] == "no_address", m["Mystery Store"]["status"])

# ── resolve(): the Weis alias prevents the duplicate ─────────────────────────────
tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close()
json.dump({"aliases": {"Weis 226 Selinsgrove": "Weis 226"}}, open(tmp.name, "w"))
M.OVERRIDE_FILE = tmp.name
chk("resolve maps 'Weis 226 Selinsgrove' → 'Weis 226'", M.resolve("Weis 226 Selinsgrove") == "Weis 226")
chk("resolve leaves un-aliased store unchanged", M.resolve("Sprouts 505") == "Sprouts 505")

# ── apply_test_form honours store_hint (form's printed name ignored) ─────────────
REG.clear()
hdr = pad(["Cart ID", "Store", "State"])
REG[(S.RSA_SPREADSHEET_ID, S.RSA_SHEET)] = [hdr, hdr, hdr,
    pad(["P_SPROUTS_505_M3_001", "Sprouts 505", "CA", "", "", "Yes", "TRUNO", "", "", "Scheduled RSA",
         "", "Yes", "Pending W&M", "", "Pending RSA", "", "HW Ops", "", "", "", ""])]
M.OVERRIDE_FILE = "/nonexistent.json"
parsed = {"store": "Sprouts Farmers Market #180", "carts": [{"cart": "1", "passed": True}]}
chk("WITHOUT hint: form name #180 fails to match", TF.apply_test_form(dict(parsed))["results"][0]["ok"] is False)
REG[(S.RSA_SPREADSHEET_ID, S.RSA_SHEET)][3][S.C_RSA_RESULTS-1] = ""
hit = TF.apply_test_form(dict(parsed), store_hint="Sprouts 505")
chk("WITH hint: matches Sprouts 505 cart", hit["results"][0]["ok"] is True
    and hit["results"][0]["cartId"] == "P_SPROUTS_505_M3_001", hit["results"][0])

for p in PASS: print("  ✓", p)
if FAIL:
    print("\nFAILURES:")
    for f in FAIL: print("  ✗", f)
print("\n" + "=" * 60)
print(f"  RESULT: {len(PASS)} passed, {len(FAIL)} failed")
print("=" * 60)
sys.exit(1 if FAIL else 0)
