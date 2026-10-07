#!/usr/bin/env python3
"""
truno_failures.py — normalize the RSA Tracker to the latest Truno call (failures + passes).

Runs on every poll (and on demand): for each cart it takes the LATEST Truno call (by visit
date, across both tabs) and brings the matching RSA row into line — open failures → Failed
RSA, carts that later passed → Completed/Passed — updating existing rows only (no duplicates).

WHY THIS EXISTS
---------------
The Truno tracker has no structured "Failed" field. A cart's failure shows up only
as free text, spread across TWO columns that must be read together:
  • Test Results (col H): row-level signal — "out of tolerence", "Passed",
    "Untested", "Pending", "Tested up to 30 lbs" (never the literal "Failed").
  • Notes (col I): the per-cart verdict — "Cart 9 failed, Cart 7 passed",
    "Cart 7 failed shift test", "2 passed, 6 did not".

None of the other monitors read these for pass/fail, so failed carts never reach
the RSA Tracker. (truno_monitor only mirrors Scheduled/Cancelled; the only automatic
pass/fail path, drive_monitor, needs an uploaded Scale-Test-Form PDF; and
rsa_reconcile — which is manual-only — would even mark a failed-but-"Complete"
visit as *Passed*.) This module closes that gap, safely, and reads BOTH the main
"Sheet1" tab AND the "30 Lb Revisits" retest tab (which nothing else reads and which
is where most failures live).

SAFE BY DESIGN
--------------
  • Failure detection COMBINES Test Results (H) + Notes (I).
  • A cart is written to "Failed RSA" (+ Overall "Failed") ONLY when its failure is
    UNAMBIGUOUS and it maps to exactly ONE RSA cart.
  • One cart can appear on several rows/tabs; the LATEST timestamped row (by Scheduled
    Date, col F — the only per-row date Truno carries; the revisit tab breaks ties)
    is the definitive status. So a cart that FAILED on the main tab but PASSED on a
    later revisit is NOT written as failed.
  • UPDATE-ONLY: this matches each failed cart to an EXISTING RSA row and updates it in
    place. It never appends a row, so it can never create a duplicate for a cart already
    in the tracker. A failed cart with no existing RSA row is routed to review (not added).
  • Latest Truno call wins: a later pass normalizes a cart up to Completed/Passed; a later
    fail sets Failed RSA even over a prior Passed/Completed (the flip is shown in the report
    and the stale completion date is cleared).
  • Everything uncertain — multi-cart "out of tolerance" with no per-cart note,
    "cannot be located", count-vs-cart wording, a store/cart that doesn't resolve to a
    unique RSA cart, or a would-be regression — is NOT written. Its source cells are
    HIGHLIGHTED in the tracker for review and listed in the report.

USAGE
-----
    python3 truno_failures.py                 # DRY RUN — report only (no writes/highlights)
    python3 truno_failures.py --commit         # write confident fails + highlight ambiguous
    python3 truno_failures.py --commit --no-highlight   # write, but don't recolor cells
    python3 truno_failures.py --clear-highlights        # remove highlights we previously set

Started in the background by rsa_agent on launch (see start_poller).
"""
import os
import re
import json
import time
import logging
import threading

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import gspread
from gspread.utils import rowcol_to_a1

import rsa_sheets as sheets
import store_map
import truno_monitor as TM
import rsa_reconcile as R

log = logging.getLogger("rsa-agent.failures")

MAIN_SHEET     = sheets.TRUNO_SHEET                                           # "Sheet1"
REVISIT_SHEET  = os.environ.get("RSA_TRUNO_REVISIT_SHEET", "30 Lb Revisits")  # the retest tab
STATE_FILE     = os.environ.get("TRUNO_FAIL_STATE_FILE", "truno_failures_state.json")
POLL_SECONDS   = int(os.environ.get("RSA_FAIL_POLL_SECONDS", "7200"))         # 2h, like truno_monitor
T_TRUNO_CODE   = store_map.T_TRUNO_CODE                                       # col B — ISC store code

# Cell background colors (Google Sheets, 0..1 RGB).
AMBER = {"red": 1.0, "green": 0.90, "blue": 0.55}   # ambiguous Truno source cells
RED   = {"red": 1.0, "green": 0.80, "blue": 0.80}   # RSA cart that conflicts (already passed)
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}     # used by --clear-highlights


# ── Verdict parsing: Test Results (H) + Notes (I) ───────────────────────────────────
# Verdict phrases, matched in this order so the most specific wins at any position.
# EXCEPTION = can't attribute a pass/fail (not located / not tested / not charged).
_TOK_RE = re.compile(
    r"(?P<num>\d+[A-Za-z]?)"
    r"|(?P<exc>cannot be located|could not be located|could not locate|cannot locate|"
    r"can'?t be located|not located|unable to (?:locate|test)|could not be (?:tested|verified)|"
    r"not charged|untested|not tested)"
    r"|(?P<fail>did ?n'?o?t pass|did ?n'?o?t|failed|fail|out of tol\w*|not pass|unable to pass)"
    r"|(?P<pas>passed|pass)",
    re.I)

# A verdict word that LEADS an explicit number list: "Carts passed: 2, 3, 5" /
# "failed: 1, 9, 12, 18". Only matches when digits immediately follow (optional ':').
_LEAD_RE = re.compile(
    r"(?P<v>failed|fail|did ?n'?o?t|passed|pass)\s*:?\s*"
    r"(?P<lst>\d+[A-Za-z]?(?:\s*(?:,|and)\s*\d+[A-Za-z]?)*)",
    re.I)

_HEDGE = ("please advise", "i believe", "believe these", "proactive", "soon be out of tol",
          "on the tolerance line", "in error")


def _norm_cart(c):
    """'01A' → '1A', '7' → '7', '  3a' → '3A'. Keeps a trailing letter, drops leading zeros."""
    m = re.match(r"\s*0*(\d+)\s*([A-Za-z]?)", str(c).strip())
    return (m.group(1) + m.group(2).upper()) if m else str(c).strip().upper()


def _cart_list(cart_cell):
    out, seen = [], set()
    for c in re.split(r"[,/]+", str(cart_cell or "")):
        c = c.strip()
        if c:
            n = _norm_cart(c)
            if n not in seen:
                seen.add(n); out.append(n)
    return out


def _is_date_serial_cart(cart_list):
    """A Truno Cart cell that got mangled into a date/serial number (e.g. '46029') can't
    identify a cart — like truno_monitor's date-cart guard, those rows aren't acted on."""
    return any(re.fullmatch(r"\d{4,}", c) for c in cart_list)


def _vgroup(word):
    w = word.lower()
    return "fail" if ("fail" in w or "did n" in w) else "pass"


def _resolve_cart(token, cart_list, cart_set):
    """Map a cart number mentioned in a note to a cart ON THIS ROW. Exact first ('3A'→'3A',
    '7'→'7'); then a BARE number to a lettered cart only when exactly one row cart shares
    that number ('Carts 5, 9 failed' → 5B, 9A). Ambiguous (two '5x' carts) → no match."""
    c = _norm_cart(token)
    if c in cart_set:
        return c
    if c.isdigit():
        hits = [x for x in cart_list if re.sub(r"[A-Za-z]$", "", x) == c]
        if len(hits) == 1:
            return hits[0]
    return None


def parse_verdicts(cart_list, result_text, notes):
    """Combine Test Results (H) + Notes (I) into a per-cart verdict.

    Returns (verdicts, row_reason). verdicts maps cart → 'fail' | 'pass' | 'conflict' |
    'exception'; row_reason is set when the row carries a fail signal that can't be
    pinned to a specific cart (→ caller treats as ambiguous and highlights).

    Attribution handles both shapes seen in the tracker:
      • LEAD list  — "Carts passed: 2, 3, 5 … Carts failed: 1, 9, 12, 18"
      • TRAIL      — "Cart 9 failed, Cart 7 passed" / "2 passed, 6 did not"
    A number is only ever treated as a cart if it is ON THIS ROW (so weights like
    '80 lbs', call numbers and dates can't be mistaken for carts)."""
    cart_set = set(cart_list)

    # Mangled Cart cell (date/serial) → don't guess; flag if there's any fail text.
    if _is_date_serial_cart(cart_list):
        blob = (str(result_text or "") + " " + str(notes or "")).lower()
        if any(k in blob for k in ("fail", "did not", "out of tol", "not pass")):
            return {}, "Cart cell looks like a date/serial (%s) — fix the Truno row" % ", ".join(cart_list)
        return {}, None

    acc = {c: set() for c in cart_list}
    text = str(notes or "")

    # 1) LEAD lists: verdict word immediately followed by a number list.
    spans = []
    for m in _LEAD_RE.finditer(text):
        v = _vgroup(m.group("v"))
        for tok in re.findall(r"\d+[A-Za-z]?", m.group("lst")):
            c = _resolve_cart(tok, cart_list, cart_set)
            if c:
                acc[c].add(v)
        spans.append((m.start("lst"), m.end("lst")))
    # Blank the consumed lists so the trailing scan doesn't re-read those numbers.
    scan = list(text)
    for a, b in spans:
        for i in range(a, b):
            scan[i] = " "
    scan = "".join(scan)

    # 2) TRAIL: buffer cart numbers, flush them to the next verdict word that appears.
    buffer = []
    for m in _TOK_RE.finditer(scan):
        g = m.lastgroup
        if g == "num":
            c = _resolve_cart(m.group("num"), cart_list, cart_set)
            if c:
                buffer.append(c)
        else:
            v = "exception" if g == "exc" else "fail" if g == "fail" else "pass"
            for c in buffer:
                acc[c].add(v)
            buffer = []

    verdicts = {}
    for c, s in acc.items():
        if not s:
            continue
        if "fail" in s and "pass" in s:
            verdicts[c] = "conflict"
        elif "fail" in s:
            verdicts[c] = "fail"
        elif "pass" in s:
            verdicts[c] = "pass"
        else:
            verdicts[c] = "exception"

    # 3) Row-level signal for carts no clause named. Always from Test Results (H); from the
    #    Notes too, but ONLY when the note named no specific cart (a blanket "Passed" /
    #    "out of tolerence" applies to the whole row; "Cart 7 passed" does not spill to cart 9).
    r = str(result_text or "").lower()
    notes_named = any(_resolve_cart(t, cart_list, cart_set) for t in re.findall(r"\d+[A-Za-z]?", str(notes or "")))
    rl = r if notes_named else (r + " . " + str(notes or "").lower())
    res_fail = any(k in rl for k in ("fail", "did not", "out of tol", "not pass"))
    res_pass = ("pass" in rl) and not res_fail
    row_reason = None
    unattributed = [c for c in cart_list if c not in verdicts]
    if unattributed and res_fail and not res_pass:
        if len(cart_list) == 1 and not verdicts:
            verdicts[cart_list[0]] = "fail"
        elif not any(v == "fail" for v in verdicts.values()):
            row_reason = ("A failure is indicated ('%s') but it doesn't say which cart(s): %s"
                          % ((str(result_text).strip() or "see note"), ", ".join(unattributed)))
    elif unattributed and res_pass and not res_fail:
        for c in unattributed:
            verdicts.setdefault(c, "pass")

    # 4) Hedged language ("please advise", "I believe", "soon be out of tolerance",
    #    "in error") → don't auto-write a fail; route the row to review instead.
    low = (text + " " + r).lower()
    if any(h in low for h in _HEDGE) and any(v == "fail" for v in verdicts.values()):
        row_reason = row_reason or ("technician hedged (%s) — confirm before marking failed"
                                    % next(h for h in _HEDGE if h in low))
        verdicts = {c: v for c, v in verdicts.items() if v != "fail"}

    return verdicts, row_reason


# ── Reading both Truno tabs ─────────────────────────────────────────────────────────
def _read_tab(sheet_name):
    """Rows of one Truno tab as dicts, carrying the sheet row number for highlighting."""
    try:
        vals = sheets._ws(sheets.TRUNO_SPREADSHEET_ID, sheet_name).get_all_values()
    except Exception as e:
        log.warning("could not read Truno tab %r: %s", sheet_name, e)
        return []
    rows = []
    for i, r in enumerate(vals[1:], start=2):            # row 1 = header; data from row 2
        rows.append({
            "tab": sheet_name, "rownum": i,
            "store":   sheets._cell(r, sheets.T_STORE),
            "code":    sheets._cell(r, T_TRUNO_CODE),
            "cart":    sheets._cell(r, sheets.T_CART),
            "address": sheets._cell(r, sheets.T_ADDRESS),
            "visit":   sheets._cell(r, sheets.T_VISIT),
            "result":  sheets._cell(r, sheets.T_TEST_RESULT),
            "notes":   sheets._cell(r, sheets.T_NOTES),
        })
    return rows


def _group_key(row, cart):
    """Merge the same physical cart across tabs/rows. Prefer the ISC store code (col B),
    which both tabs carry and which is the most reliable join; fall back to store name."""
    code = (row.get("code") or "").strip().upper()
    base = code or sheets._norm(row.get("store"))
    return (base, cart)


# ── Main analysis ───────────────────────────────────────────────────────────────────
def analyze():
    """Read both tabs, detect failures, reconcile cross-tab (latest visit wins), map to
    RSA, and classify each as a confident write, an already-synced no-op, a resolved
    pass, or ambiguous. Pure reads — returns a plan; writing/highlighting is separate."""
    main_rows    = _read_tab(MAIN_SHEET)
    revisit_rows = _read_tab(REVISIT_SHEET)
    all_rows = main_rows + revisit_rows

    # rsa + matching context (shared with truno_monitor for identical matching behavior).
    try:
        rsa_rows = sheets._rsa_data(force=True)
    except Exception as e:
        return {"error": "could not read the RSA Tracker: %s" % e}
    try:
        caper_by_addr, caper_by_num = store_map.caper_index()
    except Exception:
        caper_by_addr, caper_by_num = {}, {}
    try:
        truno_map = sheets._load_truno_map()
    except Exception:
        truno_map = {}

    # 1) Detect a verdict for every (cart) on every row; keep the LATEST by visit date
    #    (revisit tab breaks ties over the main tab, since it is the retest).
    latest = {}   # group_key -> {verdict, reason, row, cart}
    for row in all_rows:
        if not row["store"] and not row["code"]:
            continue
        carts = _cart_list(row["cart"])
        if not carts:
            continue
        verdicts, row_reason = parse_verdicts(carts, row["result"], row["notes"])
        tab_rank = 1 if row["tab"] == REVISIT_SHEET else 0
        vk = R._visit_key(row["visit"])
        for cart in carts:
            v = verdicts.get(cart)
            reason = row_reason if (row_reason and cart not in verdicts) else None
            if not v and not reason:
                continue
            key = _group_key(row, cart)
            cur = latest.get(key)
            cand = {"verdict": v, "reason": reason, "row": row, "cart": cart,
                    "_order": (vk, tab_rank, row["rownum"])}
            if cur is None or cand["_order"] >= cur["_order"]:
                latest[key] = cand

    # plan: fail = set Failed RSA; passis = normalize a stale cart up to Completed/Passed
    # (latest Truno call passed); already = RSA already matches; ambiguous = highlight only.
    plan = {"fail": [], "pass": [], "already": [], "ambiguous": []}

    for key, item in latest.items():
        row, cart, verdict, reason = item["row"], item["cart"], item["verdict"], item["reason"]
        src = {"tab": row["tab"], "rownum": row["rownum"], "store": row["store"] or row["code"],
               "cart": cart, "result": row["result"], "notes": row["notes"], "visit": row["visit"]}

        # Row-level / verdict ambiguity → highlight, never auto-write.
        if reason:
            plan["ambiguous"].append({**src, "reason": reason}); continue
        if verdict == "conflict":
            plan["ambiguous"].append({**src, "reason": "note has both pass and fail for this cart"}); continue
        if verdict == "exception":
            plan["ambiguous"].append({**src, "reason": "cart flagged 'cannot be located / tested' — not a clear result"}); continue
        if verdict not in ("fail", "pass"):
            continue

        # Map to exactly one EXISTING RSA cart (update-only → never creates a duplicate).
        res = TM.resolve_truno_row(row["store"], row["code"], row["address"], cart,
                                   rsa_rows, truno_map, caper_by_addr, caper_by_num)
        matches = res["matches"]
        if len(matches) != 1 or res["ambiguous"] or res["unmatched"] or not res["store_found"]:
            if verdict == "fail":                 # a fail we can't place is worth a human look
                if not res["store_found"]:
                    why = "store not found in the RSA Tracker"
                elif res["ambiguous"]:
                    why = "cart maps to more than one RSA serial: " + ", ".join(
                        sorted({s for _c, cands in res["ambiguous"] for s in cands})[:6])
                elif res["unmatched"]:
                    why = "no RSA cart matches cart %s at this store" % cart
                else:
                    why = "cart does not resolve to exactly one RSA cart"
                plan["ambiguous"].append({**src, "reason": why, "candidates": res.get("store_serials", [])})
            continue                               # a pass with no unique RSA row → nothing to normalize

        sheet_row, serial = matches[0]
        data = rsa_rows[sheet_row - sheets.RSA_DATA_START]
        cur_status  = sheets._cell(data, sheets.C_RSASTATUS)
        cur_overall = sheets._cell(data, sheets.C_OVERALL)
        cs, ov = cur_status.lower(), cur_overall.lower()
        entry = {**src, "serial": serial, "rsa_row": sheet_row, "from": cur_status or "—"}

        if verdict == "fail":
            if "fail" in cs:
                plan["already"].append({**entry, "status": cur_status})
            else:
                # Latest Truno call is a fail → set Failed RSA, even over a prior
                # Passed/Completed (latest-call-wins). The report flags the flip.
                entry["was_pass"] = ("passed rsa" in cs or "completed rsa" in cs or "pass" in ov)
                plan["fail"].append(entry)
        else:   # verdict == "pass"  → normalize a stale cart UP to Completed/Passed (always an advance)
            if "pass" in cs or "completed rsa" in cs or "pass" in ov:
                plan["already"].append({**entry, "status": cur_status})
            else:
                plan["pass"].append(entry)
    return plan


# ── Writing + highlighting ──────────────────────────────────────────────────────────
def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"highlighted": []}


def _save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning("could not save %s: %s", STATE_FILE, e)


def _apply_format(ws, ranges, color):
    """Set a background color on A1 ranges of one worksheet (batched, with fallback)."""
    if not ranges:
        return
    fmt = {"backgroundColor": color}
    try:
        ws.batch_format([{"range": a1, "format": fmt} for a1 in ranges])
    except Exception:
        for a1 in ranges:
            try:
                ws.format(a1, fmt)
            except Exception as e:
                log.warning("highlight %s failed: %s", a1, e)


def _highlight(plan, state):
    """Amber the ambiguous Truno source cells (Cart/Test Results/Notes/Status) on their
    own tab; red the RSA status cell for a pass/fail conflict. Remembers every cell it
    colored (in STATE_FILE) so --clear-highlights can undo exactly those."""
    cols = [sheets.T_CART, sheets.T_TEST_RESULT, sheets.T_NOTES, sheets.T_STATUS]
    by_tab, rsa_ranges, touched = {}, [], []
    for a in plan["ambiguous"]:
        a1s = [rowcol_to_a1(a["rownum"], c) for c in cols]
        by_tab.setdefault(a["tab"], []).extend(a1s)
        touched += ["%s!%s" % (a["tab"], x) for x in a1s]
        if a.get("rsa_row"):
            j = rowcol_to_a1(a["rsa_row"], sheets.C_RSASTATUS)
            rsa_ranges.append(j); touched.append("%s!%s" % (sheets.RSA_SHEET, j))

    for tab, ranges in by_tab.items():
        _apply_format(sheets._ws(sheets.TRUNO_SPREADSHEET_ID, tab), ranges, AMBER)
    if rsa_ranges:
        _apply_format(sheets._ws(sheets.RSA_SPREADSHEET_ID, sheets.RSA_SHEET), rsa_ranges, RED)

    state["highlighted"] = sorted(set(state.get("highlighted", [])) | set(touched))
    return len(touched)


def clear_highlights():
    """Reset to white every cell this tool previously highlighted."""
    state = _load_state()
    by_sheet = {}
    for ref in state.get("highlighted", []):
        tab, a1 = ref.split("!", 1)
        by_sheet.setdefault(tab, []).append(a1)
    for tab, ranges in by_sheet.items():
        sid = sheets.RSA_SPREADSHEET_ID if tab == sheets.RSA_SHEET else sheets.TRUNO_SPREADSHEET_ID
        _apply_format(sheets._ws(sid, tab), ranges, WHITE)
    n = len(state.get("highlighted", []))
    _save_state({"highlighted": []})
    log.info("Cleared %d highlighted cell(s).", n)
    return n


def _write(plan):
    """Apply the latest Truno verdict to each matched RSA cart. Update-only (never adds a
    row) and idempotent (rows already in the target state are in plan['already'], not here):
      • fail → RSA Status 'Failed RSA', Overall 'Failed'
      • pass → RSA Status 'Completed RSA', Overall 'Passed', completion date (normalization)"""
    today = sheets._today()
    cells = []
    for w in plan["fail"]:
        rr = w["rsa_row"]
        detail = ("Failed (Truno): %s" % (w["notes"] or w["result"] or "see Truno tracker")).strip()
        cells += [gspread.Cell(rr, sheets.C_RSASTATUS, "Failed RSA"),
                  gspread.Cell(rr, sheets.C_OVERALL, "Failed"),
                  gspread.Cell(rr, sheets.C_RSA_RESULTS, detail[:300]),
                  gspread.Cell(rr, sheets.C_LASTUPD, today)]
        if w.get("was_pass"):                       # flipping Passed → Failed: drop the stale completion date
            cells.append(gspread.Cell(rr, sheets.C_RSACOMPL, ""))
    for w in plan["pass"]:
        rr = w["rsa_row"]
        cells += [gspread.Cell(rr, sheets.C_RSASTATUS, "Completed RSA"),
                  gspread.Cell(rr, sheets.C_OVERALL, "Passed"),
                  gspread.Cell(rr, sheets.C_RSACOMPL, w.get("visit") or today),
                  gspread.Cell(rr, sheets.C_LASTUPD, today)]
    if cells:
        sheets._ws(sheets.RSA_SPREADSHEET_ID, sheets.RSA_SHEET).update_cells(cells)
    return len(plan["fail"]) + len(plan["pass"])


# ── Orchestration ───────────────────────────────────────────────────────────────────
def run(commit=False, highlight=True, notify=None):
    def say(msg):
        log.info(msg)
        if notify:
            try:
                notify(msg)
            except Exception:
                pass

    plan = analyze()
    if isinstance(plan, dict) and plan.get("error"):
        say(":warning: Truno-failure sync: %s" % plan["error"])
        return plan
    if commit:
        if plan["fail"] or plan["pass"]:
            try:
                _write(plan)
            except Exception as e:
                say(":warning: Truno-failure sync write failed: %s" % e)
                return {"error": str(e)}
        if highlight and plan["ambiguous"]:
            state = _load_state()
            try:
                _highlight(plan, state)
                _save_state(state)
            except Exception as e:
                log.warning("highlighting failed: %s", e)
    return plan


FAIL_RESYNC_SECONDS = int(os.environ.get("TRUNO_FAIL_RESYNC_SECONDS", str(24 * 3600)))


def start_poller(notify=None, interval=POLL_SECONDS):
    """Run the failure sync / normalizer in a daemon thread (started by rsa_agent on
    launch). First pass runs on startup; thereafter every `interval` seconds (default 2h),
    so the RSA Tracker is normalized to the latest Truno call on an ongoing basis. Each
    pass sets Failed RSA on open failures, normalizes carts that later passed up to
    Completed/Passed, and highlights ambiguous rows; it only updates existing rows (no
    duplicates). Posts a short Slack summary only when something changed or was newly flagged."""
    def loop():
        log.info("Truno failure sync running every %ss (tabs: %r + %r)", interval, MAIN_SHEET, REVISIT_SHEET)
        last_flagged = set()
        while True:
            try:
                plan = run(commit=True, highlight=True, notify=None)
                nf = len(plan.get("fail", [])) if isinstance(plan, dict) else 0
                npass = len(plan.get("pass", [])) if isinstance(plan, dict) else 0
                flagged = {(a["tab"], a["rownum"], a["cart"]) for a in plan.get("ambiguous", [])} \
                    if isinstance(plan, dict) else set()
                new_flags = flagged - last_flagged
                last_flagged = flagged
                if notify and (nf or npass or new_flags):
                    bits = []
                    if nf:
                        bits.append("set *Failed RSA* on %d cart(s)" % nf)
                    if npass:
                        bits.append("normalized %d cart(s) to *Passed* (later Truno call)" % npass)
                    if new_flags:
                        bits.append("highlighted %d ambiguous cart(s) for review" % len(new_flags))
                    try:
                        notify(":triangular_flag_on_post: Truno failure sync: " + "; ".join(bits) + ".")
                    except Exception:
                        pass
            except Exception as e:
                log.error("Truno failure sync loop error: %s", e)
            time.sleep(interval)

    threading.Thread(target=loop, name="truno-failures", daemon=True).start()


def _print_plan(plan, commit):
    print("=" * 84)
    print("  TRUNO → RSA FAILURE SYNC / NORMALIZE   (%s)" % ("COMMIT" if commit else "DRY RUN — no writes/highlights"))
    print("=" * 84)
    print("\n  %s Failed RSA: %d cart(s)" % ("WROTE" if commit else "WOULD WRITE", len(plan["fail"])))
    for w in plan["fail"]:
        flip = "  ⟵ FLIP from Passed" if w.get("was_pass") else ""
        print("    ✗ %-34s  %-22s cart %-4s  %s → Failed RSA%s   [%s %s]"
              % (w["serial"], str(w["store"])[:22], w["cart"], w["from"], flip, w["tab"], w["rownum"]))
    print("\n  %s Completed/Passed (later Truno call): %d cart(s)"
          % ("NORMALIZED" if commit else "WOULD NORMALIZE", len(plan["pass"])))
    for w in plan["pass"]:
        print("    ✓ %-34s  %-22s cart %-4s  %s → Completed RSA   [%s %s]"
              % (w["serial"], str(w["store"])[:22], w["cart"], w["from"], w["tab"], w["rownum"]))
    print("\n  ALREADY in sync (no-op): %d" % len(plan["already"]))
    for a in plan["already"]:
        print("    = %-34s  %s" % (a.get("serial", "?"), str(a["store"])[:30]))
    print("\n  ⚠ AMBIGUOUS — highlighted for review%s: %d" % ("" if commit else " (would highlight)", len(plan["ambiguous"])))
    for a in plan["ambiguous"]:
        print("    ? %-22s cart %-4s [%s row %s]  — %s"
              % (str(a["store"])[:22], a["cart"], a["tab"], a["rownum"], a["reason"]))
        if a.get("notes"):
            print("        notes:  %s" % str(a["notes"])[:90])
    print("\n" + "=" * 84)
    if not commit:
        print("  Preview only. Re-run with  --commit  to write fails and highlight the ambiguous cells.")
    print("=" * 84)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Normalize the RSA Tracker to the latest Truno call "
                                 "(Test Results + Notes): set Failed RSA on open fails, pass later-passed carts, highlight ambiguous")
    ap.add_argument("--commit", action="store_true", help="apply writes + highlight ambiguous (default: dry-run preview)")
    ap.add_argument("--no-highlight", action="store_true", help="with --commit, write but do not recolor cells")
    ap.add_argument("--clear-highlights", action="store_true", help="remove every highlight this tool previously set, then exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.clear_highlights:
        print("Cleared %d highlighted cell(s)." % clear_highlights())
        return
    plan = run(commit=args.commit, highlight=not args.no_highlight)
    if isinstance(plan, dict) and "error" in plan:
        print("ERROR:", plan["error"]); return
    _print_plan(plan, args.commit)


if __name__ == "__main__":
    main()
