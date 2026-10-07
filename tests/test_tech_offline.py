#!/usr/bin/env python3
"""
RSA Agent — OFFLINE test suite for the TECH-RESULTS pipeline.

Mirrors tests/test_offline.py (in-memory fakes, no secrets / network / real writes) but
covers the tech calibration path: the Tech Results columns + writer, the calibration
summary/parse-apply, the tech Drive monitor (gating + idempotency + self-heal), the
results-completeness gap tracking, and the Slack bot's keyword routing.

Run from the project folder:
    python3 tests/test_tech_offline.py
Exit 0 = all green, 1 = at least one failure.
"""
import os
import sys
import json
import types
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = next((d for d in (_HERE, os.path.dirname(_HERE))
             if os.path.exists(os.path.join(d, "rsa_sheets.py"))), _HERE)
sys.path.insert(0, ROOT)

REG = {}            # (spreadsheet_id, sheet_name) -> list[list[str]]
WRITES = []


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


class _Book:
    def __init__(self, sid):
        self.sid = sid

    def worksheet(self, name):
        return _WS(self.sid, name)


class _GClient:
    def open_by_key(self, sid):
        return _Book(sid)


def _install_fakes():
    gspread = types.ModuleType("gspread")
    gspread.Cell = _Cell
    gspread.authorize = lambda creds: _GClient()
    gspread.exceptions = types.SimpleNamespace(APIError=type("APIError", (Exception,), {}))
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
    req = types.ModuleType("requests"); req.get = lambda *a, **k: None
    sys.modules["requests"] = req


_install_fakes()
os.environ.setdefault("SLACK_BOT_TOKEN", "x")
os.environ.setdefault("SLACK_APP_TOKEN", "x")

import rsa_sheets as S          # noqa: E402
import tech_forms as TECHF      # noqa: E402
import tech_monitor as TM       # noqa: E402
import rsa_agent as A           # noqa: E402

RID = S.RSA_SPREADSHEET_ID

PASS, FAIL = [], []


def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name + (f"   [{extra}]" if extra and not cond else ""))


def group(title):
    print(f"\n── {title} " + "─" * (60 - len(title)))


def pad(row, n=21):
    return list(row) + [""] * (n - len(row))


def seed():
    REG.clear(); WRITES.clear()
    S._TECH_COLS = None                                   # reset resolved-column cache
    hdr = pad(["Cart ID", "Store", "State"])
    r1 = pad(["P_WF_SHOPRITE_113_M3_001", "ShopRite #113 Wallington", "NJ", "Launch",
              "06/01/2026", "Yes", "TRUNO", "", "", "Scheduled RSA", "", "Yes",
              "Pending W&M", "", "Pending RSA", "3", "Sai VS", "06/01/2026", "first note", "", ""])
    r2 = pad(["P_WF_SHOPRITE_113_M3_002", "ShopRite #113 Wallington", "NJ", "Launch",
              "06/01/2026", "Yes", "TRUNO", "", "", "Scheduled RSA", "", "Yes",
              "Pending W&M", "", "Pending RSA", "5", "HW Ops", "06/01/2026", "", "", ""])
    # Kroger cart already has BOTH a W&M test result (col U) — used for completeness math.
    r3 = pad(["P_KR_KROGER_250_M3_001", "Kroger #250 Atlanta", "GA", "Cart replacement",
              "06/05/2026", "Yes", "DUMAC", "", "", "Completed RSA", "06/12/2026", "Yes",
              "Passed W&M", "06/15/2026", "Passed", "1", "Andrew Cha", "06/15/2026", "", "",
              "Passed — within tolerance"])
    REG[(RID, S.RSA_SHEET)] = [hdr, hdr, ["Cart ID", "Store", "State"], r1, r2, r3]


def colV(serial):
    res_col, _ = S._tech_cols()
    for r in S._ws(RID, S.RSA_SHEET).get_all_values():
        if (r[0] if r else "") == serial:
            return r[res_col - 1] if len(r) >= res_col else ""
    return ""


# ════════════════════════════════════════════════════════════════════════════════
# 1. COLUMNS + WRITER
# ════════════════════════════════════════════════════════════════════════════════
def test_columns_and_writer():
    group("Tech Results columns + set_tech_result (col V/W)")
    seed()
    ec = S.ensure_tech_results_columns()
    chk("ensure_tech_results_columns resolves a col", ec.get("techResultsCol"), ec)
    hdrV = S._ws(RID, S.RSA_SHEET).cell(S.RSA_HEADER_ROW, ec["techResultsCol"]).value
    chk("'Tech Results' header written", hdrV == "Tech Results", hdrV)
    hdrW = S._ws(RID, S.RSA_SHEET).cell(S.RSA_HEADER_ROW, ec["techResultsLinkCol"]).value
    chk("'Tech Results Link' header written", hdrW == "Tech Results Link", hdrW)

    # Idempotent: re-running resolves the SAME columns by name, writes no new header.
    ec2 = S.ensure_tech_results_columns()
    chk("re-resolve is idempotent (same col)", ec2["techResultsCol"] == ec["techResultsCol"], ec2)
    chk("re-resolve creates nothing new", ec2["created"] == [], ec2["created"])

    seed(); S.ensure_tech_results_columns()
    before = S.query_cart("P_WF_SHOPRITE_113_M3_001")
    w = S.set_tech_result("P_WF_SHOPRITE_113_M3_001", store="ShopRite #113 Wallington",
                          readings_text="Load: 25 lbs->25 · Shift TL 25 lbs->25", link="https://drive/tech")
    chk("set_tech_result succeeded", not w.get("error"), w)
    chk("readings written to col V", "Load:" in colV("P_WF_SHOPRITE_113_M3_001"), colV("P_WF_SHOPRITE_113_M3_001"))
    _, link_col = S._tech_cols()
    wcell = S._ws(RID, S.RSA_SHEET).cell(4, link_col).value
    chk("file link written to col W", wcell == "https://drive/tech", wcell)
    after = S.query_cart("P_WF_SHOPRITE_113_M3_001")
    chk("set_tech_result does NOT change RSA status", after["rsaStatus"] == before["rsaStatus"], after["rsaStatus"])
    chk("set_tech_result stamps Last Updated", after["lastUpdated"] == S._today(), after["lastUpdated"])
    chk("set_tech_result on unknown cart errors", "error" in S.set_tech_result("NOPE", readings_text="x"))
    chk("set_tech_result with nothing to write errors", "error" in S.set_tech_result("P_WF_SHOPRITE_113_M3_002"))


# ════════════════════════════════════════════════════════════════════════════════
# 2. SUMMARY + PARSE-APPLY
# ════════════════════════════════════════════════════════════════════════════════
def test_summarize_and_apply():
    group("Calibration summary + apply_tech_form")
    cart = {"cart": "1",
            "loadTests": [{"expected": "25 lbs", "actual": "25"}, {"expected": "50 lbs", "actual": ""}],
            "shiftTests": [{"position": "Top Left", "expected": "25 lbs", "actual": "25"},
                           {"position": "Bottom Right", "expected": "5 lbs", "actual": "5"}],
            "passed": True, "comments": "clean"}
    s = TECHF.summarize_readings(cart)
    chk("summary includes load readings (expected->actual)", "Load: 25 lbs->25" in s, s)
    chk("summary blanks become n/a", "50 lbs->n/a" in s, s)
    chk("summary abbreviates shift positions (TL/BR)", "TL 25" in s and "BR 5" in s, s)
    chk("summary carries pass + comment", "Passed: Yes" in s and "clean" in s, s)
    chk("empty cart → graceful summary", "Calibration recorded" in TECHF.summarize_readings({}))

    seed(); S.ensure_tech_results_columns()
    parsed = {"store": "ShopRite #113 Wallington", "serviceDate": "06/10/2026", "technician": "A. Ottaino",
              "carts": [{"cart": "1", "loadTests": [{"expected": "25 lbs", "actual": "25"}], "passed": True},
                        {"cart": "2", "loadTests": [{"expected": "25 lbs", "actual": "24"}], "comments": "minor drift"},
                        {"cart": "99", "loadTests": [], "comments": "ghost"}]}
    res = TECHF.apply_tech_form(parsed, link="https://drive/sheet")
    by = {r["cart"]: r for r in res["results"]}
    chk("cart 1 matched + written", by["1"]["ok"] and by["1"]["cartId"] == "P_WF_SHOPRITE_113_M3_001", by["1"])
    chk("cart 2 matched + written", by["2"]["ok"] and by["2"]["cartId"] == "P_WF_SHOPRITE_113_M3_002")
    chk("cart 99 unmatched → flagged not written", by["99"]["ok"] is False and "no confident" in by["99"]["detail"])
    chk("technician passed through", res.get("technician") == "A. Ottaino", res.get("technician"))
    chk("cart 1 readings landed in col V", "Load: 25 lbs->25" in colV("P_WF_SHOPRITE_113_M3_001"), colV("P_WF_SHOPRITE_113_M3_001"))
    chk("cart 2 comment landed in col V", "minor drift" in colV("P_WF_SHOPRITE_113_M3_002"), colV("P_WF_SHOPRITE_113_M3_002"))
    chk("parse error short-circuits", "error" in TECHF.apply_tech_form({"error": "bad scan"}))
    chk("no store/carts → error", "error" in TECHF.apply_tech_form({"store": "", "carts": []}))


# ════════════════════════════════════════════════════════════════════════════════
# 2b. WEIGHT-CHECK CHECKLIST  (the real Excel/CSV format → parse + apply)
# ════════════════════════════════════════════════════════════════════════════════
def _checklist_grid(store="ShopRite #113 Wallington"):
    """Mirror the real WF checklist layout: header on a row, lbs/position sub-headers
    beneath, then one row per cart. (Tolerance preamble rows above, like the real file.)"""
    pre = ["Test Weight (lbs)", "Maintenance Tolerance*", "Acceptance Tolerance**"]
    header = ["Test Date", "Retailer Name", "Cart No", "Initialization value before calibration",
              "If Initialization value > 1000, Re-initialize and record the new value",
              "Latest Calibration Date", "Increasing Load Test", "", "", "", "Shift test", "", "", "",
              "Calibration required (Y/N)", "Initialization value after calibration",
              "Retorquing (Y/N)", "Calibration completed (Y/N)", "Status"]
    sub = ["", "", "", "", "", "", "5 lbs", "25 lbs", "80 lbs", "25 lbs",
           "Bottom Left 25 lbs", "Bottom Right 25 lbs", "Top Right 25 lbs", "Top Left 25 lbs",
           "", "", "", "", ""]
    c1 = ["2026-04-01", store, "1", "201.09", "331.9", "2026-03-02",
          "5", "25", "80.01", "25.01", "25.0", "25.0", "25.0", "25.0", "N", "NA", "N", "Y", ""]
    c2 = ["2026-04-01", store, "2", "1004.25", "1061.76", "2025-09-23",
          "5", "25", "80.0", "24.99", "25", "25", "24.99", "24.98", "N", "NA", "N", "N", ""]
    return [pre, ["", "tolerance", "table"], header, sub, c1, c2]


def test_checklist():
    group("Weight-check checklist parse + apply (Excel/CSV format)")
    parsed = TECHF.parse_checklist(_checklist_grid(), filename="ShopRite113_ Weight check checklist_20260401.xlsx")
    chk("checklist store parsed from Retailer column", parsed.get("store") == "ShopRite #113 Wallington", parsed.get("store"))
    chk("checklist serviceDate parsed", parsed.get("serviceDate") == "2026-04-01", parsed.get("serviceDate"))
    chk("checklist format tagged", parsed.get("format") == "checklist")
    chk("both cart rows parsed", len(parsed.get("carts", [])) == 2, len(parsed.get("carts", [])))
    c1 = parsed["carts"][0]
    chk("cart 1 load readings (4 points: 5/25/80/25)", len(c1["loadTests"]) == 4, c1["loadTests"])
    chk("cart 1 shift readings (4 corners)", len(c1["shiftTests"]) == 4, c1["shiftTests"])
    s1 = TECHF.summarize_readings(c1)
    chk("cart 1 summary has load 80->80.01", "80->80.01" in s1, s1)
    chk("cart 1 summary abbreviates corners", "BL 25" in s1 and "TL 25" in s1, s1)
    chk("cart 1 summary shows Cal done: Y", "Cal done: Y" in s1, s1)
    chk("cart 1 summary shows Init before", "Init before: 201.09" in s1, s1)

    # CSV bytes route through extract_tech_form → checklist parser (no openpyxl needed).
    import io, csv as _csv
    buf = io.StringIO(); _csv.writer(buf).writerows(_checklist_grid())
    routed = TECHF.extract_tech_form(buf.getvalue().encode(), "ShopRite113_checklist.csv")
    chk("CSV routed to checklist parser", routed.get("format") == "checklist" and len(routed.get("carts", [])) == 2, routed.get("error"))

    # Apply to the seeded tracker — carts 1 & 2 match ShopRite #113.
    seed(); S.ensure_tech_results_columns()
    res = TECHF.apply_tech_form(parsed, link="https://drive/wf62.xlsx")
    by = {r["cart"]: r for r in res["results"]}
    chk("checklist cart 1 → matched + written", by["1"]["ok"] and by["1"]["cartId"] == "P_WF_SHOPRITE_113_M3_001", by["1"])
    chk("checklist cart 2 → matched + written", by["2"]["ok"] and by["2"]["cartId"] == "P_WF_SHOPRITE_113_M3_002")
    chk("checklist readings landed in col V", "Load: 5->5" in colV("P_WF_SHOPRITE_113_M3_001"), colV("P_WF_SHOPRITE_113_M3_001"))
    chk("non-checklist file (no Cart No) → error", "error" in TECHF.parse_checklist([["foo", "bar"], ["1", "2"]]))


# ════════════════════════════════════════════════════════════════════════════════
# 2c. STRICT MATCHING  (only attach a file on a unique store# + cart# match)
# ════════════════════════════════════════════════════════════════════════════════
def _row(serial, store):
    r = [""] * 23
    r[S.C_CARTID - 1] = serial
    r[S.C_STORE - 1] = store
    return r


def test_strict_match():
    group("Strict tech matching (no random/wrong-cart files)")
    # Unique store numbers: WF #62 and Acme #70, both with a cart 1.
    rows = [_row("P_WF_WHOLEFOODS_62_M3_001", "Whole Foods #62 Brooklyn"),
            _row("P_XX_ACME_70_M3_001",       "Acme #70 Newark")]
    chk("unique store#+cart# → matches the right cart",
        S.match_cart_strict("WF-62", "1", rows) == "P_WF_WHOLEFOODS_62_M3_001",
        S.match_cart_strict("WF-62", "1", rows))
    chk("cart number not present → no match (no write)",
        S.match_cart_strict("WF-62", "2", rows) is None)
    chk("store number not present → no match (no random file)",
        S.match_cart_strict("WF-99", "1", rows) is None)
    chk("store has NO number → no match",
        S.match_cart_strict("Whole Foods", "1", rows) is None)

    # Ambiguous: two different stores both numbered 62, both with a cart 1.
    ambm = rows + [_row("P_XX_OTHER_62_M3_001", "Other Mart #62")]
    chk("ambiguous (two stores share #62 + cart 1) → skip, don't guess",
        S.match_cart_strict("WF-62", "1", ambm) is None)

    # apply_tech_form must NOT write when there's no confident match.
    seed(); S.ensure_tech_results_columns()
    parsed = {"store": "WF-62", "carts": [{"cart": "1", "loadTests": [{"expected": "25 lbs", "actual": "25"}]}]}
    res = TECHF.apply_tech_form(parsed, link="https://drive/wf62.xlsx")
    chk("WF-62 not in this tracker → flagged, nothing written",
        res["results"][0]["ok"] is False and "no confident" in res["results"][0]["detail"], res["results"][0])
    chk("no tech result written to any seeded cart",
        not colV("P_WF_SHOPRITE_113_M3_001") and not colV("P_KR_KROGER_250_M3_001"))


# ════════════════════════════════════════════════════════════════════════════════
# 3. GAP TRACKING
# ════════════════════════════════════════════════════════════════════════════════
def test_gap_tracking():
    group("Results completeness / carts_missing_results")
    seed(); S.ensure_tech_results_columns()
    comp = S.results_completeness()
    chk("3 carts total", comp["total"] == 3, comp["total"])
    chk("1 cart has a test result (Kroger)", comp["test"] == 1, comp["test"])
    chk("0 carts have a tech result yet", comp["tech"] == 0, comp["tech"])
    chk("0 carts have BOTH yet", comp["both"] == 0, comp["both"])
    chk("2 carts have NEITHER", comp["neither"] == 2, comp["neither"])

    chk("missing tech = all 3", len(S.carts_missing_results("tech")) == 3, len(S.carts_missing_results("tech")))
    chk("missing test = 2", len(S.carts_missing_results("test")) == 2, len(S.carts_missing_results("test")))
    chk("missing BOTH = 2", len(S.carts_missing_results("both")) == 2)
    chk("missing ANY = 3", len(S.carts_missing_results("any")) == 3)

    # Give ShopRite cart 1 a tech result → completeness shifts.
    S.set_tech_result("P_WF_SHOPRITE_113_M3_001", readings_text="Load: 25->25")
    comp2 = S.results_completeness()
    chk("after one tech write, tech count = 1", comp2["tech"] == 1, comp2["tech"])
    chk("missing tech now 2", comp2["missingTech"] == 2, comp2["missingTech"])
    flags = {c["cartId"]: c for c in S.carts_missing_results("any")}
    chk("cart 1 now flagged only for missing test",
        flags["P_WF_SHOPRITE_113_M3_001"]["hasTech"] and not flags["P_WF_SHOPRITE_113_M3_001"]["hasTest"])


# ════════════════════════════════════════════════════════════════════════════════
# 4. TECH MONITOR  (gating, matching, idempotency, self-heal)
# ════════════════════════════════════════════════════════════════════════════════
def test_tech_monitor():
    group("Tech monitor (no Truno gate, self-healing, idempotent)")
    seed(); S.ensure_tech_results_columns()
    rsa_rows = S._rsa_data()
    has_tech = {S._cell(r, S.C_CARTID): bool(S._cell(r, S._tech_cols()[0])) for r in rsa_rows if S._cell(r, S.C_CARTID)}
    chk("file for store 113 (carts lack tech) → worth processing",
        TM._file_store_has_missing_tech("ShopRite 113 - 26.06.10.pdf", rsa_rows, has_tech) is True)
    chk("year token in name not read as store#",
        TM._file_store_has_missing_tech("ShopRite 113 - 2026.pdf", rsa_rows, has_tech) is True)  # 113 still matches
    chk("file for an untracked store → skip",
        TM._file_store_has_missing_tech("Wegmans 7777 - 26.06.10.pdf", rsa_rows, has_tech) is False)

    files = [{"id": "t1", "name": "ShopRite 113 - 26.06.10.pdf", "webViewLink": "http://v"},
             {"id": "t2", "name": "Kroger 250 - 26.06.11.pdf", "webViewLink": "http://w"}]
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close()
    TM.PROCESSED_FILE = tmp.name
    TM._list_folder_files = lambda: files
    TM._download = lambda f: (b"%PDF-fake", None)
    calls = {"n": 0}

    def fake_process_upload(data, name, link=None):
        calls["n"] += 1
        store = "ShopRite #113 Wallington" if "113" in name else "Kroger #250 Atlanta"
        carts = ([{"cart": "1", "loadTests": [{"expected": "25 lbs", "actual": "25"}], "passed": True},
                  {"cart": "2", "loadTests": [{"expected": "25 lbs", "actual": "24"}], "comments": "drift"}]
                 if "113" in name else
                 [{"cart": "1", "loadTests": [{"expected": "25 lbs", "actual": "25"}], "passed": True}])
        return TECHF.apply_tech_form({"store": store, "carts": carts}, link=link)

    TECHF.process_upload = fake_process_upload
    TM.tech_forms = TECHF

    msgs = []
    r1 = TM.process_tech_files(notify=msgs.append)
    chk("first pass processes BOTH files (no Truno gate)", r1["processed"] == 2, r1)
    chk("parser invoked twice", calls["n"] == 2, calls["n"])
    chk("ShopRite cart 1 got tech readings", "Load: 25 lbs->25" in colV("P_WF_SHOPRITE_113_M3_001"))
    chk("Kroger cart 1 got tech readings", "Load: 25 lbs->25" in colV("P_KR_KROGER_250_M3_001"))
    chk("notify summarized the write", any("wrote tech calibration" in m for m in msgs), msgs)

    # Idempotent: same files already processed + carts now have tech → nothing re-done.
    msgs2 = []
    r2 = TM.process_tech_files(notify=msgs2.append)
    chk("second pass processes 0 (idempotent)", r2["processed"] == 0, r2)
    chk("parser not called again", calls["n"] == 2, calls["n"])

    # Self-heal: a brand-new cart for store 113 appears AFTER the file → re-applied.
    S.add_cart("P_WF_SHOPRITE_113_M3_009", "ShopRite #113 Wallington")
    msgs3 = []
    r3 = TM.process_tech_files(notify=msgs3.append)
    chk("self-heal re-processes the 113 file for the new cart", r3["processed"] == 1, r3)
    chk("Kroger file NOT re-processed (its cart already has tech)", calls["n"] == 3, calls["n"])
    os.unlink(tmp.name)

    # Backfill ignores the processed cache.
    tmp2 = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp2.close()
    TM.PROCESSED_FILE = tmp2.name
    calls["n"] = 0
    rb = TM.process_tech_files(force=True)
    chk("--backfill re-applies every file", rb["processed"] == 2 and calls["n"] == 2, rb)
    os.unlink(tmp2.name)

    # Gap report formatter.
    comp, missing = TM.gap_report()
    txt = TM.fmt_gap_report(comp, missing)
    chk("gap report shows completeness line", "Results completeness" in txt, txt)
    chk("gap report lists a missing cart with tags", "missing:" in txt)


# ════════════════════════════════════════════════════════════════════════════════
# 5. AGENT ROUTING + FORMATTERS
# ════════════════════════════════════════════════════════════════════════════════
class _FakeClient:
    def users_info(self, user=None):
        return {"user": {"real_name": "Tech Tester"}}

    def reactions_add(self, **k):
        pass

    def reactions_remove(self, **k):
        pass

    def chat_delete(self, **k):
        pass

    def chat_postMessage(self, **k):
        pass

    def chat_update(self, **k):
        pass


def _make_say(captured):
    def say(text=None, blocks=None, thread_ts=None):
        captured.append({"text": text, "blocks": blocks})
        return {"ts": "t1"}
    return say


def test_agent_routing():
    group("Slack bot — keyword routing + formatters + missingResults")
    # Explicit caption directives win (the "command" to specify the destination).
    chk("explicit 'tech results' → tech", A._route_upload("tech results", "ShopRite 113.pdf") == "tech")
    chk("explicit 'rsa results' → test", A._route_upload("rsa results", "ShopRite 113.pdf") == "test")
    chk("explicit 'test form' → test", A._route_upload("test form", "x.pdf") == "test")
    chk("explicit 'w&m' → test", A._route_upload("w&m results", "x.pdf") == "test")
    chk("explicit 'rsa' overrides spreadsheet auto", A._route_upload("rsa results", "checklist.xlsx") == "test")
    chk("explicit 'calibration' → tech", A._route_upload("cart calibration done", "x.pdf") == "tech")
    # Auto-detection when no directive is given.
    chk("spreadsheet (.xlsx) → tech (auto)", A._route_upload("", "WF62_ Weight check checklist.xlsx") == "tech")
    chk("spreadsheet (.csv) → tech (auto)", A._route_upload("", "checklist.csv") == "tech")
    chk("filename keyword → tech (auto)", A._route_upload("", "ShopRite113_tech.pdf") == "tech")
    chk("plain PDF, no keyword → test (default)", A._route_upload("shoprite 113 form", "ShopRite 113.pdf") == "test")
    chk("_is_tech_upload wrapper still works", A._is_tech_upload("", "x.xlsx") is True and A._is_tech_upload("", "y.pdf") is False)

    # handle_files routes a tech upload through tech_forms (writes col V), not test_forms.
    seed(); S.ensure_tech_results_columns()
    A.requests = types.SimpleNamespace(get=lambda *a, **k: types.SimpleNamespace(content=b"%PDF"))
    routed = {"tech": 0, "test": 0}

    def fake_tech(data, name, link=None):
        routed["tech"] += 1
        return TECHF.apply_tech_form({"store": "ShopRite #113 Wallington",
                                      "carts": [{"cart": "1", "loadTests": [{"expected": "25 lbs", "actual": "25"}]}]}, link=link)

    def fake_test(data, name, link=None):
        routed["test"] += 1
        return {"store": "x", "results": []}

    A.tech_forms.process_upload = fake_tech
    A.test_forms.process_upload = fake_test
    cap = []
    A.handle_files([{"name": "ShopRite 113.pdf", "url_private": "u", "permalink": "p"}],
                   _make_say(cap), _FakeClient(), "C1", "t0", caption="tech calibration")
    chk("tech-captioned upload → tech_forms used", routed["tech"] == 1 and routed["test"] == 0, routed)
    chk("tech upload reply mentions readings", any("Load: 25 lbs->25" in json.dumps(c) for c in cap), cap)

    cap = []
    A.handle_files([{"name": "ShopRite 113.pdf", "url_private": "u", "permalink": "p"}],
                   _make_say(cap), _FakeClient(), "C1", "t0", caption="shoprite 113 results")
    chk("no-keyword upload → test_forms used (default)", routed["test"] == 1, routed)

    # format_tech_results shape
    ftxt = A.format_tech_results("scan.pdf", {"store": "ShopRite #113", "serviceDate": "06/10/2026",
            "results": [{"cart": "1", "cartId": "P_WF_SHOPRITE_113_M3_001", "ok": True, "readings": "Load: 25->25"}]})
    chk("format_tech_results renders cart + readings", "P_WF_SHOPRITE_113_M3_001" in ftxt and "Load: 25->25" in ftxt, ftxt)

    # missingResults dispatch end-to-end with a stubbed LLM
    seed(); S.ensure_tech_results_columns()
    A.parse_intent = lambda msg, name: {"action": "missingResults", "resultsKind": "tech"}
    cap = []
    A.handle_message("which carts are missing tech results?", _make_say(cap), _FakeClient(), "C1", "t0", "U1")
    blob = json.dumps(cap)
    chk("missingResults routed → completeness shown", "Results completeness" in blob or "have BOTH" in blob, blob[:200])
    chk("missingResults lists a cart missing tech", "missing: tech" in blob, blob[:300])


def main():
    for t in (test_columns_and_writer, test_summarize_and_apply, test_checklist,
              test_strict_match, test_gap_tracking, test_tech_monitor, test_agent_routing):
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
