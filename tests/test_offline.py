#!/usr/bin/env python3
"""
RSA Agent — OFFLINE test suite.

Validates the whole app's logic with NO secrets, NO network, NO writes to any real
sheet. Every external dependency (Google Sheets, Drive, Slack, the AI gateway, PDF
vision) is replaced with an in-memory fake, so this runs anywhere and is safe to run
repeatedly.

Run from the project folder:
    python3 tests/test_offline.py

Exit code 0 = all green, 1 = at least one failure (also prints which).
"""
import os
import sys
import json
import types
import tempfile

# Find the app modules whether this file sits in <project>/tests/ or directly
# alongside rsa_agent.py (e.g. a flat run folder). Pick the dir that has them.
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = next((d for d in (_HERE, os.path.dirname(_HERE))
             if os.path.exists(os.path.join(d, "rsa_sheets.py"))), _HERE)
sys.path.insert(0, ROOT)

# ════════════════════════════════════════════════════════════════════════════════
# FAKES — stand in for every external library BEFORE the app modules import them.
# The fake Sheets store (REG) is mutable, so read-after-write behaves like Sheets.
# ════════════════════════════════════════════════════════════════════════════════
REG = {}            # (spreadsheet_id, sheet_name) -> list[list[str]]   (the "cells")
WRITES = []         # audit log of every update_cells call
REQ_POSTS = []      # audit log of every requests.post (Winter Scales email webhook)


class _Cell:
    def __init__(self, row, col, value=""):
        self.row, self.col, self.value = row, col, value


def _grow(key, row, col):
    grid = REG.setdefault(key, [])
    while len(grid) < row:
        grid.append([])
    r = grid[row - 1]
    while len(r) < col:
        r.append("")


class _WS:
    def __init__(self, sid, name):
        self.sid, self.name = sid, name

    def get_all_values(self):
        return [list(r) for r in REG.get((self.sid, self.name), [])]

    def update_cells(self, cells):
        WRITES.append((self.sid, self.name, cells))
        for c in cells:
            _grow((self.sid, self.name), c.row, c.col)
            REG[(self.sid, self.name)][c.row - 1][c.col - 1] = c.value

    def col_values(self, col):
        return [(r[col - 1] if col - 1 < len(r) else "")
                for r in REG.get((self.sid, self.name), [])]

    def cell(self, r, c):
        grid = REG.get((self.sid, self.name), [])
        v = grid[r - 1][c - 1] if r - 1 < len(grid) and c - 1 < len(grid[r - 1]) else None
        return types.SimpleNamespace(value=v)

    def delete_rows(self, idx):
        grid = REG.get((self.sid, self.name), [])
        if 0 < idx <= len(grid):
            grid.pop(idx - 1)


class _Book:
    def __init__(self, sid):
        self.sid = sid
        self.title = f"book:{sid[:6]}"

    def worksheet(self, name):
        return _WS(self.sid, name)


class _GClient:
    def open_by_key(self, sid):
        return _Book(sid)


def _install_fakes():
    gspread = types.ModuleType("gspread")
    gspread.Cell = _Cell
    gspread.authorize = lambda creds: _GClient()
    sys.modules["gspread"] = gspread

    g = types.ModuleType("google"); go = types.ModuleType("google.oauth2")
    gsa = types.ModuleType("google.oauth2.service_account")
    gsa.Credentials = type("Credentials", (), {
        "from_service_account_file": staticmethod(lambda *a, **k: object())})
    sys.modules.update({"google": g, "google.oauth2": go,
                        "google.oauth2.service_account": gsa})

    gac = types.ModuleType("googleapiclient")
    gad = types.ModuleType("googleapiclient.discovery")
    gad.build = lambda *a, **k: None
    sys.modules.update({"googleapiclient": gac, "googleapiclient.discovery": gad})

    openai = types.ModuleType("openai")
    openai.OpenAI = type("OpenAI", (), {"__init__": lambda self, *a, **k: None})
    # The app imports these to handle gateway connection/timeout blips distinctly.
    openai.APIConnectionError = type("APIConnectionError", (Exception,), {})
    openai.APITimeoutError = type("APITimeoutError", (openai.APIConnectionError,), {})
    sys.modules["openai"] = openai

    sys.modules["fitz"] = types.ModuleType("fitz")
    PIL = types.ModuleType("PIL"); PILImage = types.ModuleType("PIL.Image")
    PIL.Image = PILImage
    sys.modules.update({"PIL": PIL, "PIL.Image": PILImage})

    sb = types.ModuleType("slack_bolt")
    sb.App = type("App", (), {"__init__": lambda self, *a, **k: None,
                              "event": lambda self, *a, **k: (lambda f: f),
                              "action": lambda self, *a, **k: (lambda f: f),
                              "message": lambda self, *a, **k: (lambda f: f),
                              "client": types.SimpleNamespace()})
    sba = types.ModuleType("slack_bolt.adapter")
    sbas = types.ModuleType("slack_bolt.adapter.socket_mode")
    sbas.SocketModeHandler = type("H", (), {"__init__": lambda self, *a, **k: None})
    sys.modules.update({"slack_bolt": sb, "slack_bolt.adapter": sba,
                        "slack_bolt.adapter.socket_mode": sbas})

    dotenv = types.ModuleType("dotenv"); dotenv.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = dotenv

    # Fake requests: capture POSTs (Winter Scales email webhook) and return a
    # success response shaped like the GAS scheduleWinterScales endpoint.
    req = types.ModuleType("requests")
    req.get = lambda *a, **k: None

    class _Resp:
        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    def _post(url, json=None, timeout=None, **k):
        REQ_POSTS.append({"url": url, "json": json})
        body = json or {}
        return _Resp({"ok": True, "emailSent": True, "emailTo": body.get("emailTo") or []})

    req.post = _post
    sys.modules["requests"] = req


_install_fakes()
os.environ.setdefault("SLACK_BOT_TOKEN", "x")
os.environ.setdefault("SLACK_APP_TOKEN", "x")

import rsa_sheets as S          # noqa: E402
import test_forms as TF         # noqa: E402
import drive_monitor as DM      # noqa: E402
import rsa_agent as A           # noqa: E402
import store_map as SM          # noqa: E402

# Capture the REAL parse_intent now — test_dispatch later monkeypatches A.parse_intent,
# so the robust-parsing test references this saved original instead.
A_PARSE_INTENT = A.parse_intent

RID, TID, CID = S.RSA_SPREADSHEET_ID, S.TRUNO_SPREADSHEET_ID, S.COMPLIANCE_SPREADSHEET_ID

# Keep the RSA↔Truno map writes (from onboarding) out of the project folder during tests.
S.TRUNO_MAP_FILE = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name

# The Slack bot now only onboards stores that exist in the Deployed Stores directory.
# The bot-flow tests therefore run against a small, controlled directory fixture (the
# real export isn't checked in / would drift). store_map / store_state unit tests below
# deliberately do NOT use it — they exercise the Caper-address fallback for stores that
# are absent from the directory.
DEPLOYED_FIXTURE = os.path.join(_HERE, "deployed_stores_test.csv")


def _use_deployed_fixture():
    """Point the store directory at the test fixture (+ clear the load cache). Returns
    the previous DEPLOYED_CSV so the caller can restore it in a finally."""
    prev = SM.DEPLOYED_CSV
    SM.DEPLOYED_CSV = DEPLOYED_FIXTURE
    SM._DEPLOYED_CACHE = None
    return prev


def _restore_deployed(prev):
    SM.DEPLOYED_CSV = prev
    SM._DEPLOYED_CACHE = None


def with_deployed_fixture(fn):
    """Run a test against the Deployed Stores fixture, always restoring afterwards so
    the directory doesn't leak into the Caper-fallback unit tests."""
    def wrapper():
        prev = _use_deployed_fixture()
        try:
            return fn()
        finally:
            _restore_deployed(prev)
    wrapper.__name__ = fn.__name__
    return wrapper

# ── Mini assertion framework ────────────────────────────────────────────────────
PASS, FAIL = [], []


def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name + (f"   [{extra}]" if extra and not cond else ""))


def group(title):
    print(f"\n── {title} " + "─" * (60 - len(title)))


# ── Seed a realistic tracker every test run ─────────────────────────────────────
def pad(row, n=21):
    return list(row) + [""] * (n - len(row))


def seed():
    REG.clear(); WRITES.clear()
    hdr = pad(["Cart ID", "Store", "State"])
    r1 = pad(["P_WF_SHOPRITE_113_M3_001", "ShopRite #113 Wallington", "NJ", "Launch",
              "06/01/2026", "Yes", "TRUNO", "", "", "Pending RSA", "", "Yes",
              "Pending W&M", "", "Pending RSA", "3", "Sai VS", "06/01/2026", "first note", "", ""])
    r2 = pad(["P_WF_SHOPRITE_113_M3_002", "ShopRite #113 Wallington", "NJ", "Launch",
              "06/01/2026", "Yes", "TRUNO", "", "", "Scheduled RSA", "", "Yes",
              "Pending W&M", "", "Pending RSA", "12", "HW Ops", "06/01/2026", "", "", ""])
    r3 = pad(["P_KR_KROGER_250_M3_001", "Kroger #250 Atlanta", "GA", "Cart replacement",
              "06/05/2026", "Yes", "DUMAC", "", "", "Completed RSA", "06/12/2026", "Yes",
              "Passed W&M", "06/15/2026", "Passed", "1", "Andrew Cha", "06/15/2026", "", "", ""])
    REG[(RID, S.RSA_SHEET)] = [hdr, hdr, ["Cart ID", "Store", "State"], r1, r2, r3]

    crow = [""] * 29
    crow[S.CG["retailer"] - 1] = "ShopRite"; crow[S.CG["nickname"] - 1] = "ShopRite 113"
    crow[S.CG["city"] - 1] = "Wallington"; crow[S.CG["state"] - 1] = "NJ"
    crow[S.CG["county"] - 1] = "Bergen"
    crow[S.CG["preReq"] - 1] = "Confirm Locally"
    crow[S.CG["preDir"] - 1] = "Email the county W&M office before deploying"
    crow[S.CG["preNotify"] - 1] = "bergen-wm@nj.gov"
    crow[S.CG["postReq"] - 1] = "Self-certify"
    crow[S.CG["postDir"] - 1] = "File the self-cert within 30 days of launch"
    crow[S.CG["agency"] - 1] = "Bergen County W&M"
    REG[(CID, S.COMPLIANCE_SHEET)] = [["Title"] + [""] * 28, ["hdr"] + [""] * 28, crow]

    trow = [""] * 11
    trow[S.T_STORE - 1] = "ShopRite #113 Wallington"; trow[1] = "ISC0050-WKF-0113"
    trow[S.T_CART - 1] = "1, 2"; trow[S.T_VISIT - 1] = "06/10/2026"
    trow[S.T_NOTES - 1] = "scheduled"; trow[S.T_FORMS - 1] = "Yes"
    trow[S.T_STATUS - 1] = "Completed"
    trow2 = [""] * 11
    trow2[S.T_STORE - 1] = "Kroger #250 Atlanta"; trow2[S.T_CART - 1] = "1"
    trow2[S.T_FORMS - 1] = "No"; trow2[S.T_STATUS - 1] = "Scheduled"
    REG[(TID, S.TRUNO_SHEET)] = [["hdr"] + [""] * 10, trow, trow2]


# ════════════════════════════════════════════════════════════════════════════════
# 1. READS
# ════════════════════════════════════════════════════════════════════════════════
def test_reads():
    group("Sheets reads")
    seed()
    q = S.query_cart("P_WF_SHOPRITE_113_M3_001")
    chk("query_cart finds an existing cart", q.get("found") and q["store"].startswith("ShopRite"))
    chk("query_cart returns its DRI", q.get("dri") == "Sai VS", q.get("dri"))
    chk("query_cart not-found is graceful", S.query_cart("NOPE")["found"] is False)

    sm = S.get_summary()["counts"]
    chk("summary total = 3", sm["total"] == 3, sm["total"])
    chk("summary completedRSA = 1", sm["completedRSA"] == 1, sm["completedRSA"])
    chk("summary overallPassed = 1", sm["overallPassed"] == 1, sm["overallPassed"])
    chk("summary stale10+ = 1 (cart 002 @ 12d)", sm["stale10plus"] == 1, sm["stale10plus"])

    lc = S.list_carts(state="NJ")
    chk("list_carts state filter (2 NJ carts)", lc["total"] == 2, lc["total"])
    chk("list_carts stale filter (1 stale)", S.list_carts(staleOnly=True)["total"] == 1)
    chk("list_carts DRI filter", S.list_carts(dri="Andrew")["total"] == 1)
    chk("list_carts store fragment", S.list_carts(storeFragment="kroger")["total"] == 1)


# ════════════════════════════════════════════════════════════════════════════════
# 2. WRITES (update / add)
# ════════════════════════════════════════════════════════════════════════════════
def test_writes():
    group("Sheets writes")
    seed()
    upd = S.update_cart("P_WF_SHOPRITE_113_M3_001", rsaStatus="Scheduled RSA",
                        notes="vendor confirmed")
    chk("update_cart reports fields", "RSA Status" in upd.get("updated", []), upd)
    after = S.query_cart("P_WF_SHOPRITE_113_M3_001")
    chk("update_cart persisted status", after["rsaStatus"] == "Scheduled RSA", after["rsaStatus"])
    chk("update_cart appended (not replaced) notes", "first note" in after["notes"] and "vendor confirmed" in after["notes"])
    chk("update_cart stamped Last Updated", after["lastUpdated"] == S._today(), after["lastUpdated"])
    chk("update_cart on missing cart errors", "error" in S.update_cart("NOPE", rsaStatus="x"))

    n_before = len(S._rsa_data())
    add = S.add_cart("P_WF_SHOPRITE_113_M3_009", "ShopRite #113 Wallington")
    chk("add_cart returns added", add.get("action") == "added", add)
    chk("add_cart grew the tracker by 1", len(S._rsa_data()) == n_before + 1)
    chk("new cart starts as 'Pending Truno Scheduled'",
        S.query_cart("P_WF_SHOPRITE_113_M3_009")["rsaStatus"] == "Pending Truno Scheduled",
        S.query_cart("P_WF_SHOPRITE_113_M3_009")["rsaStatus"])
    chk("update_cart can set the service trigger",
        "Service Trigger" in S.update_cart("P_WF_SHOPRITE_113_M3_009", trigger="Cart replacement").get("updated", []))
    chk("add_cart duplicate is rejected", "error" in S.add_cart("P_WF_SHOPRITE_113_M3_009", "ShopRite #113 Wallington"))


# ════════════════════════════════════════════════════════════════════════════════
# 3. TEST RESULTS  (the new RSA-results path)
# ════════════════════════════════════════════════════════════════════════════════
def test_results():
    group("Test results → RSA Tracker (col U)")
    seed()
    erc = S.ensure_results_column()
    chk("ensure_results_column runs (adds or finds header)", "added" in erc, erc)
    hdr = S._ws(RID, S.RSA_SHEET).cell(S.RSA_HEADER_ROW, S.C_RSA_RESULTS).value
    chk("RSA Test Results header now present in col U", hdr == "RSA Test Results", hdr)

    seed()
    p = S.set_test_result("P_WF_SHOPRITE_113_M3_001", store="ShopRite #113 Wallington",
                          result_text="Passed — within tolerance", passed=True,
                          link="https://drive/x")
    chk("PASS write succeeded", not p.get("error"), p)
    row = S.query_cart("P_WF_SHOPRITE_113_M3_001")
    chk("PASS → RSA Status = Completed RSA", row["rsaStatus"] == "Completed RSA", row["rsaStatus"])
    chk("PASS → Overall = Passed (W&M out of scope)", row["overallStatus"] == "Passed", row["overallStatus"])
    ucell = S._ws(RID, S.RSA_SHEET).cell(4, S.C_RSA_RESULTS).value
    chk("PASS → result text written to col U", ucell == "Passed — within tolerance", ucell)
    tcell = S._ws(RID, S.RSA_SHEET).cell(4, S.C_TECH_LINK).value
    chk("PASS → report link written", tcell == "https://drive/x", tcell)

    f = S.set_test_result("P_WF_SHOPRITE_113_M3_002", store="ShopRite #113 Wallington",
                          result_text="Failed — drift", passed=False)
    chk("FAIL write succeeded", not f.get("error"))
    rf = S.query_cart("P_WF_SHOPRITE_113_M3_002")
    chk("FAIL → RSA Status = Failed RSA", rf["rsaStatus"] == "Failed RSA", rf["rsaStatus"])
    chk("FAIL does NOT move to Pending W&M", rf["overallStatus"] != "Pending W&M", rf["overallStatus"])

    u = S.set_test_result("P_KR_KROGER_250_M3_001", store="Kroger #250 Atlanta",
                          result_text="Result unclear", passed=None)
    chk("UNCLEAR write succeeded (text only, no status flip)", not u.get("error"))
    chk("set_test_result on unknown cart errors", "error" in S.set_test_result("NOPE"))


# ════════════════════════════════════════════════════════════════════════════════
# 4. SERIAL LOGIC  (match existing vs derive new)
# ════════════════════════════════════════════════════════════════════════════════
def test_serials():
    group("Serial matching / derivation")
    seed()
    rows = S._rsa_data()
    chk("match_cart finds the real serial for cart 2",
        S.match_cart("ShopRite #113 Wallington", "2", rows) == "P_WF_SHOPRITE_113_M3_002")
    chk("match_cart leading-zero cart '02' still matches",
        S.match_cart("ShopRite #113 Wallington", "02", rows) == "P_WF_SHOPRITE_113_M3_002")
    chk("match_cart returns None for untracked cart 9",
        S.match_cart("ShopRite #113 Wallington", "9", rows) is None)
    chk("match_cart returns None for unknown store",
        S.match_cart("Wegmans 999", "1", rows) is None)
    chk("derive_cart_id reuses the store prefix for a new cart",
        S.derive_cart_id("ShopRite #113 Wallington", "7", rows) == "P_WF_SHOPRITE_113_M3_007")
    chk("derive_cart_id keeps a letter suffix (3A)",
        S.derive_cart_id("ShopRite #113 Wallington", "3A", rows) == "P_WF_SHOPRITE_113_M3_003A")
    chk("derive_cart_id falls back for a brand-new store",
        S.derive_cart_id("Brand New Mart", "1", rows).startswith("P_") and "_M3_" in S.derive_cart_id("Brand New Mart", "1", rows))


# ════════════════════════════════════════════════════════════════════════════════
# 5. COMPLIANCE  (pre/post only, address match, flags)
# ════════════════════════════════════════════════════════════════════════════════
def test_compliance():
    group("Compliance lookup")
    seed()
    c = S.lookup_compliance("ShopRite #113 Wallington", "NJ", "Launch", address="12 Main St, Wallington")
    chk("matched the store", c.get("matched") and c.get("storeMatched"), c.get("storeMatched"))
    txt = c["slackText"]
    chk("shows Pre-launch", "Pre-launch" in txt)
    chk("shows Post-launch", "Post-launch" in txt)
    chk("does NOT dump At-launch", "At-launch" not in txt and "atDir" not in txt)
    chk("flags 'Confirm Locally'", any("Confirm Locally" in f for f in c.get("flags", [])))

    c2 = S.lookup_compliance("Mystery Mart", "ZZ", "Launch")
    chk("unknown state → not matched, with warning text", not c2.get("matched") and "warning" in c2["slackText"].lower())


# ════════════════════════════════════════════════════════════════════════════════
# 6. MULTI-CART ONBOARDING
# ════════════════════════════════════════════════════════════════════════════════
def test_onboarding():
    group("Onboarding (multi + single)")
    seed()
    ob = S.onboard_carts(["1", "7", "8"], "ShopRite #113 Wallington", state="NJ",
                         trigger="Launch", address="Wallington")
    by = {c.get("cartNum"): c for c in ob["carts"]}
    chk("cart 1 existing → updated", by["1"].get("action") == "updated", by["1"])
    chk("cart 7 new → added", by["7"].get("action") == "added", by["7"])
    chk("cart 8 new → added", by["8"].get("action") == "added", by["8"])
    chk("cart 7 and 8 got distinct serials",
        by["7"]["cartId"] != by["8"]["cartId"] and by["8"]["cartId"].endswith("008"))
    chk("two new rows actually appended", len(S._rsa_data()) == 5, len(S._rsa_data()))
    chk("TRUNO request created as Pending, NO fabricated date",
        ob["truno"].get("trunoRow") and ob["truno"].get("status") == "Pending" and not ob["truno"].get("visitDate"))
    chk("onboarded cart status = Pending Truno Scheduled",
        S.query_cart(by["7"]["cartId"])["rsaStatus"] == "Pending Truno Scheduled")
    chk("onboard result carries the trigger", ob.get("trigger") == "Launch", ob.get("trigger"))
    chk("compliance directive attached", ob.get("compliance", {}).get("matched"))
    chk("missing store → friendly error", "error" in S.onboard_carts(["1"], ""))
    chk("missing cart numbers → friendly error", "error" in S.onboard_carts([], "ShopRite 113"))


# ════════════════════════════════════════════════════════════════════════════════
# 7. FORM PARSE → APPLY  (test_forms.apply_test_form)
# ════════════════════════════════════════════════════════════════════════════════
def test_form_apply():
    group("Form results → apply to tracker")
    seed()
    parsed = {"store": "ShopRite #113 Wallington", "serviceDate": "06/10/2026",
              "trunoCall": "TC-1", "carts": [
                  {"cart": "1", "passed": True, "comments": "clean"},
                  {"cart": "2", "passed": False, "comments": "needs recalibration"},
                  {"cart": "99", "passed": True, "comments": ""}]}
    res = TF.apply_test_form(parsed, link="https://drive/form")
    by = {r["cart"]: r for r in res["results"]}
    chk("cart 1 written", by["1"]["ok"] is True, by["1"])
    chk("cart 2 (fail) still written", by["2"]["ok"] is True)
    chk("cart 99 not in tracker → flagged, not written", by["99"]["ok"] is False and "no matching" in by["99"]["detail"])
    chk("serviceDate passed through", res.get("serviceDate") == "06/10/2026")
    chk("cart 1 landed as Completed RSA", S.query_cart("P_WF_SHOPRITE_113_M3_001")["rsaStatus"] == "Completed RSA")
    chk("cart 2 landed as Failed RSA", S.query_cart("P_WF_SHOPRITE_113_M3_002")["rsaStatus"] == "Failed RSA")
    chk("parse error short-circuits apply", "error" in TF.apply_test_form({"error": "bad scan"}))
    chk("no store/carts → error", "error" in TF.apply_test_form({"store": "", "carts": []}))


# ════════════════════════════════════════════════════════════════════════════════
# 8. DRIVE MONITOR  (gating, matching, idempotency, missing-form alert)
# ════════════════════════════════════════════════════════════════════════════════
def test_drive_monitor():
    group("Drive monitor (Truno-gated, idempotent)")
    seed()

    # File matching (incl. the year-token fix)
    files = [{"id": "f1", "name": "ShopRite_113_ScaleTest_2026-06-10.pdf", "webViewLink": "http://v"},
             {"id": "f2", "name": "Kroger_250_2026-06-12.pdf", "webViewLink": "http://w"}]
    chk("_match_file matches store 113 by number", (DM._match_file(files, "ShopRite #113 Wallington") or {}).get("id") == "f1")
    chk("_match_file ignores a year token (no false match on 2026)",
        DM._match_file([{"id": "y", "name": "x_2026_2026-06-10.pdf"}], "Store 2026") is None)
    chk("_match_file None when nothing matches", DM._match_file(files, "Wegmans 7") is None)

    # Full pass with everything stubbed: only "Forms sent = Yes" stores get ingested.
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    DM.PROCESSED_FILE = tmp.name
    DM._list_folder_files = lambda: files
    DM._download = lambda fid: b"%PDF-fake"
    calls = {"n": 0}

    def fake_process_upload(data, name, link=None, store_hint=None):
        calls["n"] += 1
        ws = S._ws(RID, S.RSA_SHEET)                       # simulate writing results to col U
        cells = []
        for i, r in enumerate(ws.get_all_values()):
            if (r[0] if r else "") in ("P_WF_SHOPRITE_113_M3_001", "P_WF_SHOPRITE_113_M3_002"):
                cells.append(S.gspread.Cell(i + 1, S.C_RSA_RESULTS, "Passed (form)"))
        if cells:
            ws.update_cells(cells)
        return {"store": "ShopRite #113 Wallington",
                "results": [{"cart": "1", "cartId": "P_WF_SHOPRITE_113_M3_001", "ok": True, "passed": True},
                            {"cart": "2", "cartId": "P_WF_SHOPRITE_113_M3_002", "ok": True, "passed": False}]}
    TF.process_upload = fake_process_upload
    DM.test_forms = TF

    msgs = []
    r1 = DM.process_confirmed_forms(notify=msgs.append)
    chk("only the Forms-sent=Yes store is processed (Kroger is 'No')", r1["processed"] == 1, r1)
    chk("parser was invoked exactly once", calls["n"] == 1, calls["n"])
    chk("notify summarized the write", any("wrote" in m for m in msgs), msgs)
    chk("pass line reports RSA Passed (no W&M step)", any("Passed" in m for m in msgs))

    # Second pass: same file already processed → nothing re-ingested (idempotent).
    msgs2 = []
    r2 = DM.process_confirmed_forms(notify=msgs2.append)
    chk("idempotent — re-run processes 0 files", r2["processed"] == 0, r2)
    chk("idempotent — parser not called again", calls["n"] == 1, calls["n"])

    # Missing-form alert fires once for a confirmed store with no file.
    DM._list_folder_files = lambda: []          # folder now has no matching files
    os.unlink(tmp.name)                          # reset processed memory
    msgs3 = []
    DM.process_confirmed_forms(notify=msgs3.append)
    chk("missing-form alert raised", any("no matching form" in m for m in msgs3), msgs3)


# ════════════════════════════════════════════════════════════════════════════════
# 9. AGENT FORMATTERS  (Block Kit shape)
# ════════════════════════════════════════════════════════════════════════════════
def test_formatters():
    group("Agent formatters")
    seed()
    ob = S.onboard_carts(["1", "7"], "ShopRite #113 Wallington", state="NJ", trigger="Launches", address="Wallington")
    fo = json.dumps(A.format_onboard_multi(ob))
    chk("onboard_multi lists both carts + serial", "M3_007" in fo and "ShopRite" in fo)
    chk("onboard_multi shows Pending Truno Scheduled (no fabricated date)", "Pending Truno Scheduled" in fo)
    chk("onboard_multi shows the trigger + Nicole email", "Launches" in fo and "Nicole" in fo)
    chk("compliance formatter renders pre+post", "Pre-launch" in json.dumps(A.format_compliance(ob["compliance"])))
    chk("trigger prompt presents the dashboard options",
        "Cart replacement" in json.dumps(A.format_trigger_prompt("DEMO MART", "NJ", ["1", "2"])))
    q = json.dumps(A.format_query(S.query_cart("P_KR_KROGER_250_M3_001")))
    chk("query formatter shows the cart", "Kroger" in q)
    chk("summary formatter renders", "blocks" in A.format_summary(S.get_summary()))


# ════════════════════════════════════════════════════════════════════════════════
# 10. AGENT DISPATCH  (handle_message end-to-end with a stubbed LLM)
# ════════════════════════════════════════════════════════════════════════════════
class _FakeClient:
    def __init__(self):
        self.updates = []          # chat_update / chat_postMessage payloads (for assertions)

    def users_info(self, user=None):
        return {"user": {"real_name": "Tester"}}

    def reactions_add(self, **k):
        pass

    def reactions_remove(self, **k):
        pass

    def chat_delete(self, **k):
        pass

    def chat_postMessage(self, **k):
        self.updates.append(k)

    def chat_update(self, **k):
        self.updates.append(k)


def _make_say(captured):
    def say(text=None, blocks=None, thread_ts=None):
        captured.append({"text": text, "blocks": blocks})
        return {"ts": "t1"}
    return say


@with_deployed_fixture
def test_dispatch():
    group("Agent dispatch (handle_message)")
    seed()
    client = _FakeClient()

    # queryCart
    A.parse_intent = lambda msg, name: {"action": "queryCart", "cartId": "P_KR_KROGER_250_M3_001"}
    cap = []
    A.handle_message("status of kroger 250 cart 1", _make_say(cap), client, "C1", "t0", "U1")
    chk("queryCart routed → reply mentions the cart", any("Kroger" in json.dumps(c) for c in cap), cap[-1] if cap else None)

    # complianceCheck
    A.parse_intent = lambda msg, name: {"action": "complianceCheck", "store": "ShopRite #113 Wallington",
                                        "state": "NJ", "address": "Wallington", "trigger": "Launch"}
    cap = []
    A.handle_message("what's required for shoprite 113", _make_say(cap), client, "C1", "t0", "U1")
    chk("complianceCheck routed → Pre-launch in reply", any("Pre-launch" in json.dumps(c) for c in cap))

    # onboardCart WITH a trigger AND an explicit vendor (NJ) → adds rows directly
    seed()
    A.parse_intent = lambda msg, name: {"action": "onboardCart", "cartNumbers": ["7", "8"],
                                        "store": "ShopRite #113 Wallington", "state": "NJ",
                                        "trigger": "Launches", "rsaVendor": "TRUNO", "address": "Wallington"}
    cap = []
    n0 = len(S._rsa_data())
    A.handle_message("onboard carts 7 and 8 at shoprite 113 nj, launches, truno", _make_say(cap), client, "C1", "t0", "U1")
    chk("onboardCart (trigger + explicit vendor) → 2 new rows added", len(S._rsa_data()) == n0 + 2, len(S._rsa_data()))
    chk("onboardCart reply shows Pending Truno Scheduled", any("Pending Truno Scheduled" in json.dumps(c) for c in cap))

    # onboardCart for an NJ store with NO vendor stated → must ASK TRUNO vs Winter
    # Scales (the NJ rule) and add nothing yet.
    seed()
    A.parse_intent = lambda msg, name: {"action": "onboardCart", "cartNumbers": ["7", "8"],
                                        "store": "ShopRite #113 Wallington", "state": "NJ",
                                        "trigger": "Launches", "address": "Wallington"}
    cap = []
    n0 = len(S._rsa_data())
    A.handle_message("onboard carts 7 and 8 at shoprite 113 nj, launches", _make_say(cap), client, "C1", "t0", "U1")
    blob = json.dumps(cap)
    chk("NJ onboard w/o vendor → nothing added yet", len(S._rsa_data()) == n0, len(S._rsa_data()))
    chk("NJ onboard w/o vendor → asks TRUNO vs Winter Scales", "Winter Scales" in blob and "TRUNO" in blob)

    # Same store, but the user names Winter Scales up front → no prompt, WS path runs.
    seed()
    S.GAS_WEB_APP_URL = "https://fake.example/exec"      # enable the email webhook
    REQ_POSTS.clear()
    A.parse_intent = lambda msg, name: {"action": "onboardCart", "cartNumbers": ["7", "8"],
                                        "store": "ShopRite #113 Wallington", "state": "NJ",
                                        "trigger": "Launches", "rsaVendor": "winter scales",
                                        "address": "Wallington"}
    cap = []
    n0 = len(S._rsa_data())
    A.handle_message("onboard carts 7 and 8 at shoprite 113 nj, launches, winterscales", _make_say(cap), client, "C1", "t0", "U1")
    chk("NJ onboard w/ Winter Scales stated → 2 rows added", len(S._rsa_data()) == n0 + 2, len(S._rsa_data()))
    chk("Winter Scales onboard → emailed the WS contacts", len(REQ_POSTS) == 1 and "winterscale.com" in json.dumps(REQ_POSTS))
    chk("Winter Scales cart status = Pending RSA (not Truno)",
        S.query_cart("P_WF_SHOPRITE_113_M3_007").get("rsaStatus") == "Pending RSA")
    S.GAS_WEB_APP_URL = ""                                # restore

    # onboardCart WITHOUT a trigger → must ASK (present options), add nothing
    seed()
    A.parse_intent = lambda msg, name: {"action": "onboardCart", "cartNumbers": ["7", "8"],
                                        "store": "ShopRite #113 Wallington", "state": "NJ", "address": "Wallington"}
    cap = []
    n0 = len(S._rsa_data())
    A.handle_message("onboard carts 7 and 8 at shoprite 113", _make_say(cap), client, "C1", "t0", "U1")
    blob = json.dumps(cap)
    chk("onboard w/o trigger → nothing added yet", len(S._rsa_data()) == n0, len(S._rsa_data()))
    chk("onboard w/o trigger → asks for the service trigger", "service trigger" in blob.lower())
    chk("trigger prompt presents dashboard options", "Cart replacement" in blob and "Scale Drift" in blob)

    # Clicking a trigger button for an NJ store now ASKS the vendor (adds nothing yet)
    seed()
    n0 = len(S._rsa_data())
    cbtn = _FakeClient()
    body = {"actions": [{"action_id": "rsa_set_trigger_1",
                         "value": json.dumps({"store": "ShopRite #113 Wallington", "state": "NJ",
                                              "carts": ["7", "8"], "trigger": "Cart replacement"})}],
            "user": {"id": "U1", "name": "Tester"},
            "container": {"channel_id": "C1", "message_ts": "t1"}}
    A._complete_onboard_from_button(body, cbtn)
    chk("NJ trigger button → asks vendor, nothing added yet", len(S._rsa_data()) == n0, len(S._rsa_data()))
    chk("NJ trigger button → vendor prompt offers TRUNO + Winter Scales",
        "Winter Scales" in json.dumps(cbtn.updates) and "TRUNO" in json.dumps(cbtn.updates))

    # Clicking the TRUNO vendor button completes the onboarding (stateless via value)
    seed()
    n0 = len(S._rsa_data())
    cv = _FakeClient()
    body = {"actions": [{"action_id": "rsa_set_vendor_0",
                         "value": json.dumps({"store": "ShopRite #113 Wallington", "state": "NJ",
                                              "carts": ["7", "8"], "trigger": "Cart replacement",
                                              "vendor": "TRUNO"})}],
            "user": {"id": "U1", "name": "Tester"},
            "container": {"channel_id": "C1", "message_ts": "t1"}}
    A._complete_onboard_with_vendor(body, cv)
    chk("TRUNO vendor button → 2 rows onboarded", len(S._rsa_data()) == n0 + 2, len(S._rsa_data()))
    new = S.query_cart("P_WF_SHOPRITE_113_M3_007")
    chk("vendor-button cart carries the chosen trigger", new.get("trigger") == "Cart replacement", new.get("trigger"))
    chk("TRUNO vendor-button cart status = Pending Truno Scheduled", new.get("rsaStatus") == "Pending Truno Scheduled")
    chk("TRUNO vendor-button cart vendor = TRUNO", new.get("rsaVendor") == "TRUNO", new.get("rsaVendor"))

    # Clicking the Winter Scales vendor button → adds as Pending RSA + emails WS
    seed()
    S.GAS_WEB_APP_URL = "https://fake.example/exec"
    REQ_POSTS.clear()
    n0 = len(S._rsa_data())
    cw = _FakeClient()
    body = {"actions": [{"action_id": "rsa_set_vendor_1",
                         "value": json.dumps({"store": "ShopRite #113 Wallington", "state": "NJ",
                                              "carts": ["7", "8"], "trigger": "Cart replacement",
                                              "address": "500 Test Ave, Wallington, NJ 07057",
                                              "vendor": "Winter Scales"})}],
            "user": {"id": "U1", "name": "Tester"},
            "container": {"channel_id": "C1", "message_ts": "t1"}}
    A._complete_onboard_with_vendor(body, cw)
    chk("Winter Scales vendor button → 2 rows onboarded", len(S._rsa_data()) == n0 + 2, len(S._rsa_data()))
    wnew = S.query_cart("P_WF_SHOPRITE_113_M3_007")
    chk("WS vendor-button cart vendor = Winter Scales", wnew.get("rsaVendor") == "Winter Scales", wnew.get("rsaVendor"))
    chk("WS vendor-button cart status = Pending RSA", wnew.get("rsaStatus") == "Pending RSA", wnew.get("rsaStatus"))
    chk("WS vendor button → emailed the WS contacts once", len(REQ_POSTS) == 1 and "winterscale.com" in json.dumps(REQ_POSTS))
    chk("WS reply tells ops to follow up manually", "follow up" in json.dumps(cw.updates).lower())
    S.GAS_WEB_APP_URL = ""

    # unknown → help / clarification
    A.parse_intent = lambda msg, name: {"action": "unknown", "clarification": "Which store?"}
    cap = []
    A.handle_message("do the thing", _make_say(cap), client, "C1", "t0", "U1")
    chk("unknown → clarification returned", any("Which store?" in json.dumps(c) for c in cap))


# ════════════════════════════════════════════════════════════════════════════════
# 11. NJ VENDOR RULE  (TRUNO vs Winter Scales)
# ════════════════════════════════════════════════════════════════════════════════
def test_nj_vendor_rule():
    group("NJ vendor rule (TRUNO vs Winter Scales)")
    # Units — state detection + vendor normalisation
    chk("is_nj true for NJ / New Jersey", S.is_nj("NJ") and S.is_nj("new jersey"))
    chk("is_nj false for NY / blank", not S.is_nj("NY") and not S.is_nj(""))
    chk("_norm_vendor maps winter variants",
        S._norm_vendor("winterscales") == "Winter Scales" and S._norm_vendor("Winter Scale") == "Winter Scales")
    chk("_norm_vendor maps truno + passthrough on None",
        S._norm_vendor("truno") == "TRUNO" and S._norm_vendor(None) is None)

    # Winter Scales onboarding: vendor + Pending RSA, NO truno request, email sent.
    seed()
    S.GAS_WEB_APP_URL = "https://fake.example/exec"
    REQ_POSTS.clear()
    WRITES.clear()
    ws = S.onboard_carts(["7", "8"], "ShopRite #113 Wallington", state="NJ",
                         trigger="Launches", rsaVendor="Winter Scales", address="Wallington")
    chk("WS result vendor = Winter Scales", ws.get("vendor") == "Winter Scales", ws.get("vendor"))
    chk("WS path makes NO truno request, email sent",
        "truno" not in ws and ws.get("winterScales", {}).get("emailSent"))
    chk("WS emailed all three contacts once", len(REQ_POSTS) == 1 and all(
        e in json.dumps(REQ_POSTS) for e in
        ("service@winterscale.com", "john.winter@winterscale.com", "rich.ianniello@winterscale.com")))
    chk("WS path never writes the Truno sheet", not any(w[0] == TID for w in WRITES))
    q7 = S.query_cart("P_WF_SHOPRITE_113_M3_007")
    chk("WS cart vendor written = Winter Scales", q7.get("rsaVendor") == "Winter Scales", q7.get("rsaVendor"))
    chk("WS cart status = Pending RSA", q7.get("rsaStatus") == "Pending RSA", q7.get("rsaStatus"))
    chk("WS cart note records manual follow-up", "Winter Scales" in (q7.get("notes") or ""))
    S.GAS_WEB_APP_URL = ""

    # Graceful fallback: WS chosen but webhook unset → still added, email skipped.
    seed()
    S.GAS_WEB_APP_URL = ""
    ws2 = S.onboard_carts(["7"], "ShopRite #113 Wallington", state="NJ", trigger="Launches",
                          rsaVendor="winterscales", address="500 Test Ave, Wallington, NJ 07057")
    chk("WS w/o webhook → onboarded, email skipped (manual)",
        ws2.get("winterScales", {}).get("emailSkipped") and not ws2.get("winterScales", {}).get("emailSent"))

    # Default (no vendor) keeps TRUNO behaviour — one shared Truno request.
    seed()
    tr = S.onboard_carts(["7", "8"], "ShopRite #113 Wallington", state="NJ", trigger="Launches",
                         address="500 Test Ave, Wallington, NJ 07057")
    chk("default vendor = TRUNO + Truno request created",
        tr.get("vendor") == "TRUNO" and tr.get("truno", {}).get("status") == "Pending")
    chk("default TRUNO cart status = Pending Truno Scheduled",
        S.query_cart("P_WF_SHOPRITE_113_M3_007").get("rsaStatus") == "Pending Truno Scheduled")


# ════════════════════════════════════════════════════════════════════════════════
# 11b. TRUNO / WINTER SCALES ADDRESS  (populate col E + block when unresolved)
# ════════════════════════════════════════════════════════════════════════════════
def test_truno_address():
    group("Truno address (populate col E + block) + WS address")
    ADDR = "500 Test Ave, Wallington, NJ 07057"

    # 1. Address resolvable → Truno row created WITH col E populated.
    seed()
    ob = S.onboard_carts(["7"], "ShopRite #113 Wallington", state="NJ", trigger="Launches",
                         rsaVendor="TRUNO", address=ADDR)
    chk("TRUNO onboard w/ address → row created (Pending)",
        ob.get("truno", {}).get("status") == "Pending", ob.get("truno"))
    trow = ob["truno"]["trunoRow"]
    cellE = S._ws(TID, S.TRUNO_SHEET).cell(trow, S.T_ADDRESS).value
    chk("address written to Truno col E (T_ADDRESS)", cellE == ADDR, cellE)
    chk("truno result carries the address", ob["truno"].get("address") == ADDR)

    # 2. No address + store not in Caper → BLOCK: no Truno row written.
    seed()
    WRITES.clear()
    ob2 = S.onboard_carts(["7"], "ShopRite #113 Wallington", state="NJ", trigger="Launches",
                          rsaVendor="TRUNO")                 # no address, not in Caper
    chk("no address → TRUNO blocked (needAddress, no row)",
        ob2.get("truno", {}).get("needAddress") is True and ob2["truno"].get("trunoRow") is None,
        ob2.get("truno"))
    chk("no address → nothing written to the Truno sheet", not any(w[0] == TID for w in WRITES))
    chk("block is on the Truno row only — RSA row still added", len(S._rsa_data()) == 4, len(S._rsa_data()))

    # 3. Winter Scales: the address rides along in the email payload; blocks without one.
    seed()
    S.GAS_WEB_APP_URL = "https://fake.example/exec"
    REQ_POSTS.clear()
    ws = S.onboard_carts(["7", "8"], "ShopRite #113 Wallington", state="NJ", trigger="Launches",
                         rsaVendor="Winter Scales", address=ADDR)
    chk("WS email sent when an address is present", ws.get("winterScales", {}).get("emailSent"))
    chk("WS email payload includes the address",
        bool(REQ_POSTS) and REQ_POSTS[-1]["json"].get("address") == ADDR,
        REQ_POSTS[-1]["json"] if REQ_POSTS else None)
    REQ_POSTS.clear()
    ws2 = S.onboard_carts(["7"], "ShopRite #113 Wallington", state="NJ", trigger="Launches",
                          rsaVendor="Winter Scales")          # no address
    chk("WS without address → blocked, no email POST",
        ws2.get("winterScales", {}).get("needAddress") is True and not REQ_POSTS, ws2.get("winterScales"))
    S.GAS_WEB_APP_URL = ""

    # 4. store_address resolution order: full Caper street > caller hint > '' (→ block).
    real_csv = SM.CAPER_CSV
    SM.CAPER_CSV = os.path.join(ROOT, "caper_stores.csv")
    try:
        chk("store_address prefers a full Caper street address",
            "New York Ave" in SM.store_address("Lyndhurst 113"), SM.store_address("Lyndhurst 113"))
        chk("store_address falls back to the caller hint when Caper has no street",
            SM.store_address("Mystery Mart 77", "9 Hint Rd") == "9 Hint Rd")
        chk("store_address is '' when nothing resolves (caller then blocks)",
            SM.store_address("Mystery Mart 77") == "")
    finally:
        SM.CAPER_CSV = real_csv


# ════════════════════════════════════════════════════════════════════════════════
# 12. ROBUST PARSING  (_extract_json + parse_intent never crashes on bad LLM output)
# ════════════════════════════════════════════════════════════════════════════════
class _FakeLLM:
    """Stand-in for the OpenAI gateway client: returns a fixed assistant message, or
    raises, so parse_intent's failure handling can be exercised with no network."""
    def __init__(self, content=None, raises=False):
        self._content, self._raises = content, raises
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, **k):
        if self._raises:
            if isinstance(self._raises, BaseException):   # a specific error to raise
                raise self._raises
            raise RuntimeError("gateway down")
        msg = types.SimpleNamespace(content=self._content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def test_robust_parsing():
    group("Robust intent parsing (never crashes)")
    ej = A._extract_json
    chk("clean object parses", ej('{"action":"getSummary"}') == {"action": "getSummary"})
    chk("code-fenced object parses",
        (ej('```json\n{"action":"queryCart","cartId":"X"}\n```') or {}).get("cartId") == "X")
    chk("prose-prefixed object parses (the reported crash repro)",
        (ej('Sure! Here you go:\n{"action":"listCarts","state":"NJ"}') or {}).get("state") == "NJ")
    chk("object with trailing prose parses",
        (ej('{"action":"getSummary"} hope that helps!') or {}).get("action") == "getSummary")
    chk("bare array parses to a list", isinstance(ej('[{"store":"A","cartNumbers":["1"]}]'), list))
    chk("empty string → None", ej("") is None)
    chk("pure prose → None", ej("I cannot do that right now.") is None)

    # parse_intent must NEVER raise — drive it with a fake gateway client.
    real = A.llm
    try:
        A.llm = _FakeLLM(content="I think you mean two stores — which one first?")  # prose, no JSON
        r = A_PARSE_INTENT("Sprouts 15 carts 3,4 / Sprouts 20 carts 5", "Sai")
        chk("prose reply → unknown (no crash)", r.get("action") == "unknown", r)
        chk("prose reply → carries a clarification", bool(r.get("clarification")))

        A.llm = _FakeLLM(content='[{"store":"Sprouts 15","state":"NJ","cartNumbers":["3","4"]},'
                                 '{"store":"Sprouts 20","state":"NJ","cartNumbers":["5"]}]')
        r = A_PARSE_INTENT("...", "Sai")
        chk("bare-array reply → onboardCart with 2 stores",
            r.get("action") == "onboardCart" and len(r.get("stores") or []) == 2, r)

        A.llm = _FakeLLM(raises=True)
        r = A_PARSE_INTENT("anything", "Sai")
        chk("gateway error → unknown (no crash)", r.get("action") == "unknown", r)

        # A network/gateway connection blip is handled distinctly: unknown intent + a
        # plain "couldn't reach the backend" nudge (and a concise log, not a stack dump).
        import openai as _oa
        A.llm = _FakeLLM(raises=_oa.APIConnectionError("gateway unreachable"))
        r = A_PARSE_INTENT("anything", "Sai")
        chk("connection error → unknown (no crash)", r.get("action") == "unknown", r)
        chk("connection error → friendly 'reach the assistant backend' nudge",
            "reach the assistant backend" in (r.get("clarification") or ""), r)
    finally:
        A.llm = real


# ════════════════════════════════════════════════════════════════════════════════
# 13. MULTI-STORE ONBOARDING  (several stores in one message; ask trigger/vendor once)
# ════════════════════════════════════════════════════════════════════════════════
@with_deployed_fixture
def test_multi_store():
    group("Multi-store onboarding (ask once)")
    client = _FakeClient()
    two = [{"store": "ShopRite #113 Wallington", "state": "NJ", "cartNumbers": ["7", "8"]},
           {"store": "Kroger #250 Atlanta", "state": "GA", "cartNumbers": ["2", "3"]}]

    # Two stores, trigger + explicit vendor given → both onboard directly in one shot.
    seed()
    A.parse_intent = lambda msg, name: {"action": "onboardCart", "trigger": "Launches",
                                        "rsaVendor": "TRUNO", "stores": two}
    cap = []
    n0 = len(S._rsa_data())
    A.handle_message("two stores", _make_say(cap), client, "C1", "t0", "U1")
    blob = json.dumps(cap)
    chk("multi-store direct → 4 rows added across 2 stores", len(S._rsa_data()) == n0 + 4, len(S._rsa_data()))
    chk("multi-store reply names both stores", "ShopRite" in blob and "Kroger" in blob)
    chk("multi-store reply says 'across 2 store(s)'", "across 2 store(s)" in blob)
    chk("multi-store added the new serials for both stores",
        "P_WF_SHOPRITE_113_M3_007" in blob and "P_KR_KROGER_250_M3_002" in blob)

    # Two stores, trigger given, NO vendor, one NJ → ask vendor ONCE, add nothing yet.
    seed()
    A.parse_intent = lambda msg, name: {"action": "onboardCart", "trigger": "Launches",
                                        "stores": [{"store": "ShopRite #113 Wallington", "state": "NJ", "cartNumbers": ["7"]},
                                                   {"store": "Kroger #250 Atlanta", "state": "GA", "cartNumbers": ["2"]}]}
    cap = []
    n0 = len(S._rsa_data())
    A.handle_message("two stores no vendor", _make_say(cap), client, "C1", "t0", "U1")
    blob = json.dumps(cap)
    chk("multi-store NJ w/o vendor → nothing added yet", len(S._rsa_data()) == n0, len(S._rsa_data()))
    chk("multi-store NJ w/o vendor → asks vendor once (TRUNO + Winter Scales)",
        "Winter Scales" in blob and "TRUNO" in blob)

    # Two stores, NO trigger → ask the service trigger ONCE, add nothing yet.
    seed()
    A.parse_intent = lambda msg, name: {"action": "onboardCart",
                                        "stores": [{"store": "ShopRite #113 Wallington", "state": "NJ", "cartNumbers": ["7"]},
                                                   {"store": "Kroger #250 Atlanta", "state": "GA", "cartNumbers": ["2"]}]}
    cap = []
    n0 = len(S._rsa_data())
    A.handle_message("two stores no trigger", _make_say(cap), client, "C1", "t0", "U1")
    blob = json.dumps(cap)
    chk("multi-store w/o trigger → nothing added yet", len(S._rsa_data()) == n0, len(S._rsa_data()))
    chk("multi-store w/o trigger → asks the service trigger", "service trigger" in blob.lower())

    # Trigger button carrying a groups payload (NJ present, no vendor) → asks vendor.
    seed()
    n0 = len(S._rsa_data())
    cbtn = _FakeClient()
    groups = [{"store": "ShopRite #113 Wallington", "state": "NJ", "carts": ["7", "8"]},
              {"store": "Kroger #250 Atlanta", "state": "GA", "carts": ["2"]}]
    body = {"actions": [{"action_id": "rsa_set_trigger_0",
                         "value": json.dumps({"groups": groups, "trigger": "Launches"})}],
            "user": {"id": "U1", "name": "Tester"},
            "container": {"channel_id": "C1", "message_ts": "t1"}}
    A._complete_onboard_from_button(body, cbtn)
    chk("groups trigger button (NJ) → asks vendor, nothing added", len(S._rsa_data()) == n0, len(S._rsa_data()))
    chk("groups trigger button → vendor prompt offers both options",
        "Winter Scales" in json.dumps(cbtn.updates) and "TRUNO" in json.dumps(cbtn.updates))

    # Vendor button carrying a groups payload (TRUNO) → onboards every store group.
    seed()
    n0 = len(S._rsa_data())
    cv = _FakeClient()
    body = {"actions": [{"action_id": "rsa_set_vendor_0",
                         "value": json.dumps({"groups": groups, "trigger": "Launches", "vendor": "TRUNO"})}],
            "user": {"id": "U1", "name": "Tester"},
            "container": {"channel_id": "C1", "message_ts": "t1"}}
    A._complete_onboard_with_vendor(body, cv)
    chk("groups vendor button (TRUNO) → 3 rows onboarded across 2 stores",
        len(S._rsa_data()) == n0 + 3, len(S._rsa_data()))
    chk("groups vendor button reply spans both stores",
        "ShopRite" in json.dumps(cv.updates) and "Kroger" in json.dumps(cv.updates))


# ════════════════════════════════════════════════════════════════════════════════
# 14. STATE RESOLUTION  (determine state from the Caper address; ask if unknown)
# ════════════════════════════════════════════════════════════════════════════════
def test_state_resolution():
    group("State resolution (Caper address → state)")
    # ZIP → state, incl. the tricky DC/VA split and the El Paso carve-out.
    chk("ZIP 20170 → VA (Herndon, not DC)", SM.state_from_zip("20170") == "VA")
    chk("ZIP 21224 → MD (Baltimore)", SM.state_from_zip("21224") == "MD")
    chk("ZIP 89121 → NV", SM.state_from_zip("89121") == "NV")
    chk("ZIP 07057 → NJ", SM.state_from_zip("07057") == "NJ")
    chk("ZIP 88510 → TX (El Paso, not NM)", SM.state_from_zip("88510") == "TX")
    # Address parsing — a LEADING street number must NOT be mistaken for a ZIP.
    chk("bare ZIP address → state", SM.state_from_address("20170") == ("VA", "zip"))
    chk("trailing ZIP wins over leading street #",
        SM.state_from_address("22833 Bothell Everett Hwy Bothell, WASHING TON 98021")[0] == "WA")
    chk("street-number-only address → unknown (no false ZIP)",
        SM.state_from_address("10000 W Sahara Ave") == (None, None))
    chk("'…, OH, 45040' → OH", SM.state_from_address("5100 Terra Firma Dr, Mason, OH, 45040")[0] == "OH")

    # store_state against the real Caper CSV (absolute path so cwd doesn't matter).
    real_csv = SM.CAPER_CSV
    SM.CAPER_CSV = os.path.join(ROOT, "caper_stores.csv")
    try:
        chk("Sprouts 15 → VA (Caper ZIP 20170)", SM.store_state("Sprouts 15")["state"] == "VA")
        chk("Sprouts 20 → MD (Caper ZIP 21224)", SM.store_state("Sprouts 20")["state"] == "MD")
        chk("Sprouts 15 is NOT defaulted to NJ", SM.store_state("Sprouts 15")["state"] != "NJ")
        chk("unknown store → not confident (will ask)",
            SM.store_state("Mystery Mart 77")["confident"] is False)
    finally:
        SM.CAPER_CSV = real_csv


@with_deployed_fixture
def test_state_dispatch():
    group("Onboarding state gate (resolve / ask / remember)")
    client = _FakeClient()
    # Real Caper CSV regardless of cwd; redirect the override file to a temp path so
    # the test NEVER touches the real store_map.json.
    real_csv, real_ovr = SM.CAPER_CSV, SM.OVERRIDE_FILE
    SM.CAPER_CSV = os.path.join(ROOT, "caper_stores.csv")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    tmp.write("{}"); tmp.close()
    SM.OVERRIDE_FILE = tmp.name
    try:
        # Two stores, NO state given → both resolved from the Caper address (VA, MD).
        seed()
        A.parse_intent = lambda msg, name: {"action": "onboardCart", "trigger": "Launches",
            "stores": [{"store": "Sprouts 15", "cartNumbers": ["3", "4", "5"]},
                       {"store": "Sprouts 20", "cartNumbers": ["5", "6"]}]}
        cap = []; n0 = len(S._rsa_data())
        A.handle_message("sprouts 15 and 20", _make_say(cap), client, "C1", "t0", "U1")
        blob = json.dumps(cap)
        chk("no state given → 5 rows added (states resolved)", len(S._rsa_data()) == n0 + 5, len(S._rsa_data()))
        chk("Sprouts 15 resolved to VA in the reply", "(VA)" in blob)
        chk("Sprouts 20 resolved to MD in the reply", "(MD)" in blob)
        chk("neither store was defaulted to NJ", "(NJ)" not in blob)

        # The reported bug: intent mislabels Sprouts 15 as NJ → Caper corrects to VA.
        seed()
        A.parse_intent = lambda msg, name: {"action": "onboardCart", "trigger": "Launches",
            "stores": [{"store": "Sprouts 15", "state": "NJ", "cartNumbers": ["7"]}]}
        cap = []; n0 = len(S._rsa_data())
        A.handle_message("sprouts 15 nj", _make_say(cap), client, "C1", "t0", "U1")
        blob = json.dumps(cap)
        chk("typed NJ is overridden by the Caper address → VA", "(VA)" in blob and "(NJ)" not in blob)
        chk("Sprouts 15 onboarded (1 row)", len(S._rsa_data()) == n0 + 1)

        # Unknown store, no state → bot ASKS for the state (adds nothing).
        seed()
        A.parse_intent = lambda msg, name: {"action": "onboardCart", "trigger": "Launches",
            "stores": [{"store": "Mystery Mart 77", "cartNumbers": ["1"]}]}
        cap = []; n0 = len(S._rsa_data())
        A.handle_message("mystery mart", _make_say(cap), client, "C1", "t0", "U1")
        blob = json.dumps(cap)
        chk("unknown store → nothing added yet", len(S._rsa_data()) == n0)
        chk("unknown store → asks for the state", "what state is" in blob.lower())

        # Clicking a state button records the override and completes the onboarding.
        seed(); n0 = len(S._rsa_data())
        cstate = _FakeClient()
        body = {"actions": [{"action_id": "rsa_set_state_0",
                             "value": json.dumps({"groups": [{"store": "Mystery Mart 77", "state": None, "carts": ["1"]}],
                                                  "resolveIdx": 0, "trigger": "Launches", "vendor": None, "state": "TX"})}],
                "user": {"id": "U1", "name": "Tester"},
                "container": {"channel_id": "C1", "message_ts": "t1"}}
        A._complete_state_from_button(body, cstate)
        upd = json.dumps(cstate.updates)
        chk("state button → 1 row onboarded", len(S._rsa_data()) == n0 + 1, len(S._rsa_data()))
        chk("state button → onboarded as TX", "(TX)" in upd)
        chk("state choice remembered as an override",
            SM.store_state("Mystery Mart 77")["state"] == "TX")
    finally:
        SM.CAPER_CSV, SM.OVERRIDE_FILE = real_csv, real_ovr
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════════
# 15. STORE DIRECTORY  (Deployed Stores: name matching + the bot's selection gate)
# ════════════════════════════════════════════════════════════════════════════════
@with_deployed_fixture
def test_store_directory():
    group("Store directory (match by banner / id / nickname / internal id / retailer)")
    # Exact match on each selectable name field → one store.
    chk("match by Store id",        SM.match_store("prod-wakefern-113")["matchedBy"] == "store")
    chk("match by nickname",        SM.match_store("ShopRite #113 Wallington")["matchedBy"] == "nickname")
    chk("match by external id",     SM.match_store("113")["store"]["display"] == "ShopRite 113")
    chk("match is case/space/punct-insensitive",
        SM.match_store("shoprite #113   wallington")["status"] == "matched")
    # NEW: banner + external store id ("ShopRite 113") is the preferred name.
    bm = SM.match_store("ShopRite 113")
    chk("match by banner + external id", bm["matchedBy"] == "banner_alias" and bm["status"] == "matched", bm)
    chk("display is '<Banner> <External Store ID>'", (bm["store"] or {}).get("display") == "ShopRite 113")
    chk("lowercase 'shoprite 113' also matches the banner alias",
        SM.match_store("shoprite 113")["matchedBy"] == "banner_alias")
    # NEW: internal id = Store field with the deployment (prod-/qvs-) prefix removed.
    im = SM.match_store("wakefern-113")
    chk("match by internal id (Store minus 'prod-')", im["matchedBy"] == "internal_id" and im["status"] == "matched", im)
    # A retailer names a GROUP of stores → ambiguous (must pick).
    rm = SM.match_store("sprouts")
    chk("retailer → ambiguous with all its stores", rm["status"] == "ambiguous" and rm["count"] == 2, rm)
    # No exact match but a close one → suggestions; nothing close → none.
    chk("near miss → suggestions offered", SM.match_store("Sprouts 1")["status"] == "suggest")
    chk("nonsense → no match", SM.match_store("Zzz Quux Mart")["status"] == "none")
    # The address written to Truno is the single-line 'Street, City, State Zip'.
    chk("matched store → directory address",
        SM.store_address("Sprouts 15") == "100 Elden St, Herndon, VA 20170", SM.store_address("Sprouts 15"))
    chk("matched store → state straight from the directory",
        SM.store_state("Sprouts 20")["state"] == "MD" and SM.store_state("Sprouts 20")["basis"] == "deployed")
    chk("directory address wins over a caller hint for a known store",
        SM.store_address("Sprouts 15", "999 Wrong Rd") == "100 Elden St, Herndon, VA 20170")


@with_deployed_fixture
def test_store_gate():
    group("Bot store-selection gate (validate → pick → map address)")
    client = _FakeClient()

    # 1. Unknown store → the bot refuses to onboard and asks for a real deployed store.
    seed()
    A.parse_intent = lambda msg, name: {"action": "onboardCart", "cartNumbers": ["1"],
        "store": "Wegmans 999", "state": "NY", "trigger": "Launches", "rsaVendor": "TRUNO"}
    cap = []; n0 = len(S._rsa_data())
    A.handle_message("onboard 1 at wegmans 999", _make_say(cap), client, "C1", "t0", "U1")
    blob = json.dumps(cap)
    chk("unknown store → nothing onboarded", len(S._rsa_data()) == n0)
    chk("unknown store → asks for a deployed store", "deployed stores list" in blob.lower())

    # 2. A retailer (many stores) → ambiguous → asks which one, with pick buttons.
    seed()
    A.parse_intent = lambda msg, name: {"action": "onboardCart", "cartNumbers": ["1"],
        "store": "sprouts", "trigger": "Launches", "rsaVendor": "TRUNO"}
    cap = []; n0 = len(S._rsa_data())
    A.handle_message("onboard 1 at sprouts", _make_say(cap), client, "C1", "t0", "U1")
    blob = json.dumps(cap)
    chk("retailer → nothing onboarded yet", len(S._rsa_data()) == n0)
    chk("retailer → asks which store", "matches 2 stores" in blob)
    chk("retailer → offers store-pick buttons", "rsa_set_store" in blob)

    # 3. Picking a store button locks it in → onboards under the canonical name.
    seed(); n0 = len(S._rsa_data())
    cstore = _FakeClient()
    body = {"actions": [{"action_id": "rsa_set_store_0",
        "value": json.dumps({"groups": [{"store": "sprouts", "carts": ["7"]}],
                             "resolveIdx": 0, "trigger": "Launches", "vendor": "TRUNO",
                             "store": "prod-sprouts-15"})}],
        "user": {"id": "U1", "name": "Tester"},
        "container": {"channel_id": "C1", "message_ts": "t1"}}
    A._complete_store_from_button(body, cstore)
    chk("store-pick button → 1 row onboarded", len(S._rsa_data()) == n0 + 1, len(S._rsa_data()))
    chk("store-pick resolves to the canonical name", "Sprouts 15" in json.dumps(cstore.updates))

    # 4. THE CORE REQUIREMENT: a validated store maps to its address combination + its
    #    banner name, both copied into the Truno tracker (store col A + address col E),
    #    with NO caller-supplied address.
    seed()
    A.parse_intent = lambda msg, name: {"action": "onboardCart", "cartNumbers": ["7"],
        "store": "ShopRite #113 Wallington", "trigger": "Launches", "rsaVendor": "TRUNO"}
    cap = []
    A.handle_message("onboard 7 at shoprite 113", _make_say(cap), client, "C1", "t0", "U1")
    truno_grid = S._ws(TID, S.TRUNO_SHEET).get_all_values()
    addr_cells  = [row[S.T_ADDRESS - 1] for row in truno_grid if len(row) >= S.T_ADDRESS]
    store_cells = [row[S.T_STORE - 1]   for row in truno_grid if len(row) >= S.T_STORE]
    chk("directory address written to Truno col E",
        "12 Main St, Wallington, NJ 07057" in addr_cells, addr_cells)
    chk("banner + external id name written to Truno col A (store)",
        "ShopRite 113" in store_cells, store_cells)


# ════════════════════════════════════════════════════════════════════════════════
# 16. BULK COMPLETE (13-day gate) + W&M HIDDEN FROM VIEWS + RSA REMINDERS
# ════════════════════════════════════════════════════════════════════════════════
def _rr(cid, store, rsa="Pending RSA", overall="Pending RSA", days="", last="",
        vendor="Winter Scales", state="NJ", wm="", dri=""):
    r = [""] * 23
    r[S.C_CARTID - 1] = cid;   r[S.C_STORE - 1] = store;   r[S.C_STATE - 1] = state
    r[S.C_RSAVENDOR - 1] = vendor; r[S.C_RSASTATUS - 1] = rsa; r[S.C_OVERALL - 1] = overall
    r[S.C_WMSTATUS - 1] = wm; r[S.C_DRI - 1] = dri
    r[S.C_DAYS - 1] = str(days); r[S.C_LASTUPD - 1] = last
    return r


def test_normalize_tracker():
    group("Tracker normalize: Overall=Passed on Completed RSA; DRI=CSM ONLY for the handoff")
    hdr = pad(["Cart ID", "Store", "State"])
    REG[(RID, S.RSA_SHEET)] = [hdr, hdr, hdr,
        # handoff: Completed RSA + Pending W&M → Overall Passed + DRI CSM
        _rr("P_H_M3_001", "StoreH", rsa="Completed RSA", overall="Pending W&M", wm="Pending W&M", dri="HW Ops"),
        # Pending W&M but RSA NOT done → stays HW Ops (the over-correction fix)
        _rr("P_ACT_M3_001", "StoreAct", rsa="Scheduled RSA", overall="Pending RSA", wm="Pending W&M", dri="HW Ops"),
        # wrongly set to CSM but not a handoff → reverted to HW Ops
        _rr("P_STRAY_M3_001", "StoreStray", rsa="Scheduled RSA", overall="Pending RSA", wm="Passed W&M", dri="CSM"),
        # a named owner, not a handoff → left untouched
        _rr("P_NAMED_M3_001", "StoreNamed", rsa="Scheduled RSA", overall="Pending RSA", dri="Sai VS")]
    dry = S.normalize_tracker(dry_run=True)
    chk("dry-run: counts 2 changed, writes nothing",
        dry["changedRows"] == 2 and S.query_cart("P_H_M3_001")["overallStatus"] == "Pending W&M", dry)
    res = S.normalize_tracker()
    chk("handoff (Completed RSA + Pending W&M) → Overall Passed", S.query_cart("P_H_M3_001")["overallStatus"] == "Passed")
    chk("handoff → DRI = CSM", S.query_cart("P_H_M3_001")["dri"] == "CSM", S.query_cart("P_H_M3_001")["dri"])
    chk("Pending W&M but RSA not done → DRI stays HW Ops (not CSM)",
        S.query_cart("P_ACT_M3_001")["dri"] == "HW Ops", S.query_cart("P_ACT_M3_001")["dri"])
    chk("stray CSM (non-handoff) → reverted to HW Ops",
        S.query_cart("P_STRAY_M3_001")["dri"] == "HW Ops", S.query_cart("P_STRAY_M3_001")["dri"])
    chk("named owner untouched", S.query_cart("P_NAMED_M3_001")["dri"] == "Sai VS")
    chk("normalize reports counts", res["overallPassed"] == 1 and res["driCsm"] == 1 and res["driReverted"] == 1, res)
    chk("re-running normalize is a no-op (converges)", S.normalize_tracker()["changedRows"] == 0)


def test_normalize_poller():
    group("Normalize poller: run_once applies rules + notifies only on change")
    import rsa_normalize as RN
    hdr = pad(["Cart ID", "Store", "State"])
    REG[(RID, S.RSA_SHEET)] = [hdr, hdr, hdr,
        _rr("P_N_M3_001", "StoreN", rsa="Completed RSA", overall="Pending W&M", wm="Pending W&M", dri="HW Ops"),
        _rr("P_N_M3_002", "StoreN", rsa="Scheduled RSA", overall="Pending RSA", wm="Pending W&M", dri="HW Ops")]
    posted = []
    res = RN.run_once(notify=posted.append)
    chk("run_once applies the handoff rules", res["overallPassed"] == 1 and res["driCsm"] == 1, res)
    chk("active Pending-W&M cart is left with HW Ops", S.query_cart("P_N_M3_002")["dri"] == "HW Ops",
        S.query_cart("P_N_M3_002")["dri"])
    chk("run_once posts one summary when rows change", len(posted) == 1 and "normalized" in posted[0], posted)
    RN.run_once(notify=posted.append)
    chk("run_once is silent after convergence (no new post)", len(posted) == 1, posted)


def test_bulk_and_wm_removal():
    group("Bulk Completed RSA (13-day gate) + W&M hidden from views")
    hdr = pad(["Cart ID", "Store", "State"])
    REG[(RID, S.RSA_SHEET)] = [hdr, hdr, hdr,
        _rr("P_WF_SHOPRITE_496_M3_001", "ShopRite 496", days=95),    # stale → update
        _rr("P_WF_SHOPRITE_35",         "ShopRite 35",  days=10),    # 10d ≤ 13 → skip
        _rr("P_WF_SHOPRITE_457_M3_002", "ShopRite 457", days=157)]   # stale → update

    # dry run writes nothing
    dry = S.bulk_set_rsa_status(["P_WF_SHOPRITE_496_M3_001"], min_stale_days=13, dry_run=True)
    chk("bulk dry-run: reports the cart, writes nothing",
        dry["count"] == 1 and S.query_cart("P_WF_SHOPRITE_496_M3_001")["rsaStatus"] == "Pending RSA", dry)

    res = S.bulk_set_rsa_status(
        ["P_WF_SHOPRITE_496_M3_001", "P_WF_SHOPRITE_35", "P_WF_SHOPRITE_457_M3_002", "P_WF_NOPE_1"],
        min_stale_days=13)
    chk("bulk: 2 carts stale > 13 days updated", res["count"] == 2, res)
    chk("bulk: the 10-day cart is skipped (not > 13)",
        any(s["cartId"] == "P_WF_SHOPRITE_35" for s in res["skippedRecent"]), res["skippedRecent"])
    chk("bulk: unknown id reported missing", res["missing"] == ["P_WF_NOPE_1"], res["missing"])
    q = S.query_cart("P_WF_SHOPRITE_496_M3_001")
    chk("bulk: updated cart → Completed RSA", q["rsaStatus"] == "Completed RSA", q["rsaStatus"])
    chk("bulk: updated cart → Overall Passed", q["overallStatus"] == "Passed", q["overallStatus"])
    chk("bulk: the 10-day cart is left untouched",
        S.query_cart("P_WF_SHOPRITE_35")["rsaStatus"] == "Pending RSA")
    # W&M Status can be set too (to silence the legacy W&M-keyed scheduling reminders)
    S.bulk_set_rsa_status(["P_WF_SHOPRITE_457_M3_002"], wmStatus="Passed W&M", min_stale_days=13)
    chk("bulk: sets the W&M Status column when asked",
        S.query_cart("P_WF_SHOPRITE_457_M3_002")["wmStatus"] == "Passed W&M",
        S.query_cart("P_WF_SHOPRITE_457_M3_002")["wmStatus"])

    # W&M hidden from the bot's views + roll-ups
    counts = S.get_summary()["counts"]
    chk("summary counts drop the W&M keys", "pendingWM" not in counts and "passedWM" not in counts, list(counts))
    chk("summary view has no W&M line", "W&M" not in json.dumps(A.format_summary(S.get_summary())))
    chk("cart query view has no W&M line", "W&M" not in json.dumps(A.format_query(S.query_cart("P_WF_SHOPRITE_496_M3_001"))))
    chk("list view has no W&M column", "W&M" not in json.dumps(A.format_list(S.list_carts())))


def test_rsa_reminders():
    group("RSA scheduling reminders (RSA-only, replaces RSAAlert)")
    import rsa_reminders as RR
    hdr = pad(["Cart ID", "Store", "State"])
    REG[(RID, S.RSA_SHEET)] = [hdr, hdr, hdr,
        _rr("P_WF_SHOPRITE_457_M3_002", "ShopRite 457", days=157, vendor="Winter Scales"),
        _rr("P_WF_SHOPRITE_457_M3_004", "ShopRite 457", days=120, vendor="Winter Scales"),
        _rr("P_KR_KROGER_1", "Kroger 250", rsa="Completed RSA", overall="Passed", days=200),  # done → excluded
        _rr("P_WF_SHOPRITE_35", "ShopRite 35", days=10)]                                       # fresh ≤13 → excluded

    groups = RR.find_stale_rsa(min_days=13)
    stores = {g["store"] for g in groups}
    chk("reminder: stale open store included", "ShopRite 457" in stores, stores)
    chk("reminder: a done cart's store is excluded", "Kroger 250" not in stores, stores)
    chk("reminder: a fresh (<13d) store is excluded", "ShopRite 35" not in stores, stores)
    g457 = next(g for g in groups if g["store"] == "ShopRite 457")
    chk("reminder: groups both stale carts for the store", len(g457["carts"]) == 2, g457)
    chk("reminder: surfaces the single shared vendor", g457["vendor"] == "Winter Scales")
    msg = RR.format_reminder(g457)
    chk("reminder: message is RSA-only (no W&M)", "W&M" not in msg, msg)
    chk("reminder: names the store + longest days", "ShopRite 457" in msg and "157 days" in msg, msg)
    chk("reminder: tells ops to schedule with the vendor", "Winter Scales" in msg)
    posted = []
    RR.post_rsa_reminders(notify=posted.append, min_days=13)
    chk("reminder: posts one message per stale store", len(posted) == 1, posted)


def test_transient_read_handling():
    group("Transient network errors → retry/log, not a Slack flood")
    chk("broken pipe is transient", S.is_transient_error(BrokenPipeError(32, "Broken pipe")))
    chk("connection reset is transient", S.is_transient_error(ConnectionResetError(54, "Connection reset by peer")))
    chk("errno-string is transient", S.is_transient_error(Exception("[Errno 54] Connection reset by peer")))
    chk("a config/value error is NOT transient", not S.is_transient_error(ValueError("bad folder id")))
    posted = []
    r1 = DM._read_fail("the Drive folder", ConnectionResetError(54, "Connection reset by peer"), posted.append)
    chk("transient read failure posts NOTHING to Slack", posted == [] and r1.get("transient") is True, (posted, r1))
    r2 = DM._read_fail("the Drive folder", ValueError("permission denied"), posted.append)
    chk("a real/persistent error IS posted to Slack",
        any("Could not read" in m for m in posted) and r2.get("transient") is False, posted)


def test_gfh_bristol_match():
    group("Form results bridge GFH-2 / Bristol → RSA rows (the 0/2 bug)")
    hdr = pad(["Cart ID", "Store", "State"])
    REG[(RID, S.RSA_SHEET)] = [hdr, hdr, hdr,
        _rr("P_GFH_2_M3_452", "GFH2- Santa Monica", rsa="Scheduled RSA", overall="Pending RSA"),
        _rr("P_GFH_2_M3_453", "GFH2- Santa Monica", rsa="Scheduled RSA", overall="Pending RSA")]
    rows = S._rsa_data()
    chk("cart 452 bridges 'GFH-2' → the RSA row", S.match_cart("GFH-2", "452", rows) == "P_GFH_2_M3_452",
        S.match_cart("GFH-2", "452", rows))
    chk("cart 453 bridges 'GFH-2' → the RSA row", S.match_cart("GFH-2", "453", rows) == "P_GFH_2_M3_453")
    # full apply path (store_hint is the alias target 'GFH-2'): both carts write, not 0/2
    parsed = {"store": "GFH2- Santa Monica",
              "carts": [{"cart": "452", "passed": True}, {"cart": "453", "passed": True}]}
    res = TF.apply_test_form(parsed, store_hint="GFH-2")
    chk("apply writes 2/2 carts (not 0/2)", sum(1 for r in res["results"] if r["ok"]) == 2, res["results"])


def test_cart_letter_order():
    group("Cart label order-agnostic: form 'A1' matches tracker '1A'")
    hdr = pad(["Cart ID", "Store", "State"])
    REG[(RID, S.RSA_SHEET)] = [hdr, hdr, hdr,
        _rr("P_GFH_2_M3_001A", "GFH2- Santa Monica", rsa="Scheduled RSA", overall="Pending RSA"),
        _rr("P_GFH_2_M3_003A", "GFH2- Santa Monica", rsa="Scheduled RSA", overall="Pending RSA")]
    rows = S._rsa_data()
    chk("_cart_num_letter('A1') == ('1','a')", S._cart_num_letter("A1") == ("1", "a"), S._cart_num_letter("A1"))
    chk("form 'A1' matches tracker 1A", S.match_cart("GFH-2", "A1", rows) == "P_GFH_2_M3_001A",
        S.match_cart("GFH-2", "A1", rows))
    chk("form 'A3' matches tracker 3A", S.match_cart("GFH-2", "A3", rows) == "P_GFH_2_M3_003A")
    chk("plain '1A' still matches", S.match_cart("GFH-2", "1A", rows) == "P_GFH_2_M3_001A")
    parsed = {"store": "GFH2- Santa Monica",
              "carts": [{"cart": "A1", "passed": True}, {"cart": "A3", "passed": True}]}
    res = TF.apply_test_form(parsed, store_hint="GFH-2")
    chk("apply writes 2/2 with letter-first labels (was 0/2)",
        sum(1 for r in res["results"] if r["ok"]) == 2, res["results"])


def test_alias_bridge_restored():
    group("Alias file bridges the Truno/form store name → RSA store")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"aliases": {"GFH- Bristol 0021": "GFH-2", "ISC0050-GFH-0021": "GFH-2"}, "states": {}}, tmp)
    tmp.close()
    prev = SM.OVERRIDE_FILE
    SM.OVERRIDE_FILE = tmp.name
    try:
        chk("resolve('GFH- Bristol 0021') → GFH-2", SM.resolve("GFH- Bristol 0021") == "GFH-2",
            SM.resolve("GFH- Bristol 0021"))
        chk("resolve('ISC0050-GFH-0021') → GFH-2", SM.resolve("ISC0050-GFH-0021") == "GFH-2")
    finally:
        SM.OVERRIDE_FILE = prev
        try: os.unlink(tmp.name)
        except Exception: pass


# ── Run everything ───────────────────────────────────────────────────────────────
def main():
    for t in (test_reads, test_writes, test_results, test_serials, test_compliance,
              test_onboarding, test_form_apply, test_drive_monitor, test_formatters,
              test_dispatch, test_nj_vendor_rule, test_truno_address, test_robust_parsing,
              test_multi_store, test_state_resolution, test_state_dispatch,
              test_store_directory, test_store_gate, test_bulk_and_wm_removal, test_rsa_reminders,
              test_transient_read_handling, test_gfh_bristol_match, test_cart_letter_order,
              test_alias_bridge_restored, test_normalize_tracker, test_normalize_poller):
        try:
            t()
        except Exception as e:
            import traceback
            FAIL.append(f"{t.__name__} CRASHED: {type(e).__name__}: {e}")
            traceback.print_exc()

    for p in PASS:
        print("  ✓", p)
    if FAIL:
        print("\nFAILURES:")
        for f in FAIL:
            print("  ✗", f)
    print("\n" + "=" * 64)
    print(f"  RESULT:  {len(PASS)} passed, {len(FAIL)} failed")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
