#!/usr/bin/env python3
"""OFFLINE test for rsa_undo.py — proves it deletes ONLY rows tagged
'Backfilled from Truno tracker' (incl. a Weis 226 duplicate), leaves pre-existing /
bot / manual rows untouched, and handles non-contiguous blocks. No network.

Run:  python3 tests/test_undo.py
"""
import os, sys, types

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
    def delete_rows(self, start, end=None):
        end = end or start
        g = REG.get((self.sid, self.name), [])
        del g[start-1:end]                  # 1-indexed inclusive
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
gspread.exceptions = types.SimpleNamespace(APIError=type("APIError", (Exception,), {}))
sys.modules["gspread"] = gspread
g = types.ModuleType("google"); go = types.ModuleType("google.oauth2")
gsa = types.ModuleType("google.oauth2.service_account")
gsa.Credentials = type("C", (), {"from_service_account_file": staticmethod(lambda *a, **k: object())})
sys.modules.update({"google": g, "google.oauth2": go, "google.oauth2.service_account": gsa})

import rsa_sheets as S        # noqa: E402
import rsa_undo as U          # noqa: E402

RID = S.RSA_SPREADSHEET_ID
PASS, FAIL = [], []
def chk(n, c, extra=""): (PASS if c else FAIL).append(n + (f"  [{extra}]" if extra and not c else ""))
def pad(r, n=21): return list(r) + [""] * (n - len(r))


def row(serial, store, note):
    r = pad([serial, store, "NJ"])
    r[S.C_NOTES - 1] = note
    return r

TAG = U.TAG
hdr = pad(["Cart ID", "Store", "State"])
REG[(RID, S.RSA_SHEET)] = [hdr, hdr, hdr,
    row("P_WEIS_226_M3_001",             "Weis 226",             "Added via RSA Agent bot"),   # 4  pre-existing
    row("P_WEIS_226_SELINSGROVE_M3_001", "Weis 226 Selinsgrove", TAG),                          # 5  DUPLICATE (backfilled)
    row("P_SHOPRITE_113_M3_001",         "ShopRite 113",         "manual entry"),               # 6  pre-existing
    row("P_SPROUTS_505_M3_001",          "Sprouts 505",          TAG),                          # 7  backfilled
    row("P_SPROUTS_505_M3_007",          "Sprouts 505",          TAG),                          # 8  backfilled (contig w/ 7)
    row("P_KROGER_250_M3_001",           "Kroger 250",           "Added via RSA Agent bot"),    # 9  pre-existing
    row("P_DAVIS_11934_M3_001",          "Davis 11934",          TAG + "  [RSA Agent · …]"),     # 10 backfilled (non-contig)
]

# _runs grouping
chk("_runs groups contiguous + non-contiguous correctly",
    U._runs([5, 7, 8, 10]) == [(5, 5), (7, 8), (10, 10)], U._runs([5, 7, 8, 10]))

# find: exactly the 4 tagged rows
_, found = U.find_backfilled()
fset = {e["serial"] for e in found}
chk("find returns exactly the 4 tagged rows", fset == {
    "P_WEIS_226_SELINSGROVE_M3_001", "P_SPROUTS_505_M3_001",
    "P_SPROUTS_505_M3_007", "P_DAVIS_11934_M3_001"}, fset)
chk("find does NOT include the real Weis 226", "P_WEIS_226_M3_001" not in fset)

# preview must not change anything
before = len(S._rsa_data())
U.undo(commit=False)
chk("preview deletes nothing", len(S._rsa_data()) == before, len(S._rsa_data()))

# commit: delete only tagged
U.undo(commit=True)
remaining = [S._cell(r, S.C_CARTID) for r in S._rsa_data() if S._cell(r, S.C_CARTID)]
chk("after undo: exactly the 3 untagged rows remain", set(remaining) == {
    "P_WEIS_226_M3_001", "P_SHOPRITE_113_M3_001", "P_KROGER_250_M3_001"}, remaining)
chk("real 'Weis 226' survived", "P_WEIS_226_M3_001" in remaining)
chk("duplicate 'Weis 226 Selinsgrove' removed", "P_WEIS_226_SELINSGROVE_M3_001" not in remaining)
chk("both Sprouts 505 backfill rows removed",
    not any("SPROUTS_505" in s for s in remaining))
chk("no untagged row was deleted (count == 3)", len(remaining) == 3, len(remaining))

# idempotent: second undo finds nothing
_, again = U.find_backfilled()
chk("re-run finds nothing left to undo", again == [], again)

for p in PASS: print("  ✓", p)
if FAIL:
    print("\nFAILURES:")
    for f in FAIL: print("  ✗", f)
print("\n" + "=" * 60)
print(f"  RESULT: {len(PASS)} passed, {len(FAIL)} failed")
print("=" * 60)
sys.exit(1 if FAIL else 0)
