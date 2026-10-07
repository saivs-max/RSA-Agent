#!/usr/bin/env python3
"""OFFLINE test for drive_monitor's form-application decisions: self-healing
(apply when a forms-sent cart has no result yet), idempotent convergence, --backfill
force, the missing-form alert, and the numberless name-token match. No network: the
Drive list/download and the vision parse are stubbed; gspread is an in-memory fake.

Run:  python3 tests/test_monitor.py
"""
import os, sys, types, json, tempfile, re

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
gspread.exceptions = types.SimpleNamespace(APIError=type("APIError", (Exception,), {}))
sys.modules["gspread"] = gspread
g = types.ModuleType("google"); go = types.ModuleType("google.oauth2")
gsa = types.ModuleType("google.oauth2.service_account")
gsa.Credentials = type("C", (), {"from_service_account_file": staticmethod(lambda *a, **k: object())})
sys.modules.update({"google": g, "google.oauth2": go, "google.oauth2.service_account": gsa})
gac = types.ModuleType("googleapiclient"); gad = types.ModuleType("googleapiclient.discovery")
gad.build = lambda *a, **k: None
sys.modules.update({"googleapiclient": gac, "googleapiclient.discovery": gad})
openai = types.ModuleType("openai"); openai.OpenAI = type("O", (), {"__init__": lambda s, *a, **k: None})
openai.APIConnectionError = type("APIConnectionError", (Exception,), {}); openai.APITimeoutError = type("APITimeoutError", (openai.APIConnectionError,), {})
sys.modules["openai"] = openai
sys.modules["fitz"] = types.ModuleType("fitz")
PIL = types.ModuleType("PIL"); PImg = types.ModuleType("PIL.Image"); PIL.Image = PImg
sys.modules.update({"PIL": PIL, "PIL.Image": PImg})

import rsa_sheets as S          # noqa: E402
import test_forms as TF         # noqa: E402
import drive_monitor as DM      # noqa: E402

RID, TID = S.RSA_SPREADSHEET_ID, S.TRUNO_SPREADSHEET_ID
PASS, FAIL = [], []
def chk(n, c, extra=""): (PASS if c else FAIL).append(n + (f"  [{extra}]" if extra and not c else ""))
def pad(r, n=21): return list(r) + [""] * (n - len(r))


def seed():
    REG.clear()
    hdr = pad(["Cart ID", "Store", "State"])
    def rsa(cid, store, result=""):
        r = pad([cid, store, "NJ", "", "", "Yes", "TRUNO", "", "", "Scheduled RSA", "", "Yes",
                 "Pending W&M", "", "Pending RSA", "", "HW Ops", "", "", "", ""])
        r[S.C_RSA_RESULTS - 1] = result
        return r
    REG[(RID, S.RSA_SHEET)] = [hdr, hdr, hdr,
        rsa("P_WF_77_ROXBOROUGH_M3_013", "WF 77 - Roxborough", ""),        # present, NO result yet
        rsa("P_WF_SHOPRITE_113_M3_001", "ShopRite #113 Wallington", "Passed — clean"),  # already has result
    ]
    def trow(store, cart, forms="Yes"):
        r = [""] * 11
        r[S.T_STORE-1] = store; r[S.T_CART-1] = cart; r[S.T_FORMS-1] = forms; r[S.T_STATUS-1] = "Completed"
        return r
    REG[(TID, S.TRUNO_SHEET)] = [["h"] + [""]*10,
        trow("WF 77 - Roxborough", "13"),
        trow("ShopRite #113 Wallington", "1"),
    ]


FILES = [
    {"id": "wf77",  "name": "WF 77 form.pdf",        "webViewLink": "http://wf77"},
    {"id": "sr113", "name": "Shoprite 113 form.pdf", "webViewLink": "http://sr113"},
]

CALLS = []
def fake_process_upload(data, name, link=None, store_hint=None):
    """Simulate apply: write a result to col U of carts whose store number is in the filename."""
    CALLS.append(name)
    nums = set(re.findall(r"\d{2,}", name)); nums |= {n.lstrip("0") or n for n in nums}
    ws = S._ws(S.RSA_SPREADSHEET_ID, S.RSA_SHEET)
    cells = []
    for i, r in enumerate(ws.get_all_values()):
        cid = r[0] if r else ""
        store = r[1] if len(r) > 1 else ""
        if cid and (nums & set(S._store_nums(store))):
            cells.append(gspread.Cell(i + 1, S.C_RSA_RESULTS, "Passed (form)"))
    if cells:
        ws.update_cells(cells)
    return {"store": name, "results": [{"cart": "x", "cartId": cid, "ok": True, "passed": True}]}


def _setup(processed_ids):
    seed()
    CALLS.clear()
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close()
    json.dump(list(processed_ids), open(tmp.name, "w"))
    DM.PROCESSED_FILE = tmp.name
    DM._list_folder_files = lambda: list(FILES)
    DM._download = lambda fid: b"%PDF"
    DM.time = types.SimpleNamespace(sleep=lambda *_: None)   # no real sleeping
    TF.process_upload = fake_process_upload
    DM.test_forms = TF


# 1) Self-heal: both files already "processed", but WF-77's cart has no result → reapply WF-77 only
_setup({"wf77", "sr113"})
msgs = []
DM.process_confirmed_forms(notify=msgs.append)
chk("self-heal applies WF-77 (cart had no result)", "WF 77 form.pdf" in CALLS, CALLS)
chk("does NOT re-apply ShopRite (cart already has result + file seen)", "Shoprite 113 form.pdf" not in CALLS, CALLS)

# 2) Converged: WF-77 cart now has a result → next pass applies nothing (idempotent)
CALLS.clear()
DM.process_confirmed_forms(notify=[].append)
chk("converged — second pass applies nothing", CALLS == [], CALLS)

# 3) Force/backfill: re-applies every forms-sent store regardless of cache/results
_setup({"wf77", "sr113"})
DM.process_confirmed_forms(notify=[].append, force=True)
chk("backfill re-applies BOTH forms", set(CALLS) == {"WF 77 form.pdf", "Shoprite 113 form.pdf"}, CALLS)

# 4) New file is always applied (even if the cart already has a result)
_setup(set())                       # nothing processed yet → both are "new"
DM.process_confirmed_forms(notify=[].append)
chk("new files applied", set(CALLS) == {"WF 77 form.pdf", "Shoprite 113 form.pdf"}, CALLS)

# 5) Missing form → one-time alert
_setup(set())
REG[(TID, S.TRUNO_SHEET)].append([""]*10 + [""])      # ensure list ok
wegmans = [""]*11
wegmans[S.T_STORE-1] = "Wegmans 900"; wegmans[S.T_CART-1] = "1"; wegmans[S.T_FORMS-1] = "Yes"
REG[(TID, S.TRUNO_SHEET)].append(wegmans)
msgs = []
DM.process_confirmed_forms(notify=msgs.append)
chk("missing-form alert raised for store with no file", any("no matching form" in m for m in msgs), msgs)

# 6) Name-token match for a numberless store
mf = DM._match_file([{"id": "x", "name": "Roxborough Whole Foods 26.05.pdf"}], "Whole Foods Roxborough")
chk("_match_file matches numberless store by name token", mf and mf["id"] == "x", mf)
chk("_match_file still ignores pure-year tokens", DM._match_file([{"id": "y", "name": "x_2026.pdf"}], "Store 2026") is None)

for p in PASS: print("  ✓", p)
if FAIL:
    print("\nFAILURES:")
    for f in FAIL: print("  ✗", f)
print("\n" + "=" * 60)
print(f"  RESULT: {len(PASS)} passed, {len(FAIL)} failed")
print("=" * 60)
sys.exit(1 if FAIL else 0)
