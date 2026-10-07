#!/usr/bin/env python3
"""
RSA Agent — Google Sheets data layer (service-account auth).

Reads/writes the RSA Tracker, Truno tracker, and W&M Compliance Guide directly
via the Sheets API, so the bot never has to call the (domain-locked) Apps Script
Web App. Functions return the same shapes the GAS Web App did, so the agent's
formatters work unchanged.

Setup: share all three sheets with the service-account email, and point
GOOGLE_SA_KEY_FILE at the downloaded JSON key.
"""

import os
import re
import json
import logging

log = logging.getLogger("rsa-agent.sheets")
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
import gspread
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────
SA_KEY_FILE = os.environ.get("GOOGLE_SA_KEY_FILE", "sa-key.json")

RSA_SPREADSHEET_ID        = os.environ.get("RSA_SPREADSHEET_ID", "12YHqYh7Grd0Ixurwt23MfJU0dBy6cdndUooJfPK2TcE")
TRUNO_SPREADSHEET_ID      = os.environ.get("TRUNO_SPREADSHEET_ID", "1aXvOqJLJmXCyMitYuZHYcDOKgTojhdZPs7ikvrvalno")
COMPLIANCE_SPREADSHEET_ID = os.environ.get("COMPLIANCE_SPREADSHEET_ID", "126U07Y-b8D9w5Nw6GKkyo-P-9Yh9lARJtWotzPrBBRc")

RSA_SHEET        = os.environ.get("RSA_SHEET_NAME", "RSA Tracker")
TRUNO_SHEET      = os.environ.get("TRUNO_SHEET_NAME", "Sheet1")
COMPLIANCE_SHEET = os.environ.get("COMPLIANCE_SHEET_NAME", "Store Requirements")
NICOLE_EMAIL     = os.environ.get("NICOLE_EMAIL", "nicole@truno.com")

# ── Winter Scales — NJ-only alternative vendor ────────────────────────────────
# For stores in NJ the agent does NOT auto-assign TRUNO; it asks the ops manager
# to pick TRUNO or Winter Scales. Winter Scales has no tracker sheet of its own,
# so the hand-off is an email to these contacts (sent through the GAS web app,
# which owns Gmail); ops then follows up with Winter Scales directly.
WINTERSCALES_VENDOR = "Winter Scales"
WINTERSCALES_EMAILS = [e.strip() for e in os.environ.get(
    "WINTERSCALES_EMAILS",
    "service@winterscale.com, john.winter@winterscale.com, rich.ianniello@winterscale.com",
).split(",") if e.strip()]

# GAS web app webhook (the same one the bot already uses). Used here only to send
# the Winter Scales email via GmailApp — every sheet write still goes via gspread.
GAS_WEB_APP_URL = os.environ.get("GAS_WEB_APP_URL", "")
AGENT_SECRET    = os.environ.get("AGENT_SECRET", "")

RSA_URL   = f"https://docs.google.com/spreadsheets/d/{RSA_SPREADSHEET_ID}/edit"
TRUNO_URL = f"https://docs.google.com/spreadsheets/d/{TRUNO_SPREADSHEET_ID}/edit"


def webapp_url(cart_id=None):
    """Link to the RSA webapp (the searchable cart UI) rather than the raw sheet.

    Given a cart_id, deep-links to ``.../exec?cart=<id>`` so the webapp opens on the
    Tracker tab already filtered to that cart — that's where users go to find it.
    Falls back to the spreadsheet URL when GAS_WEB_APP_URL isn't configured, so a
    usable link is always produced."""
    base = GAS_WEB_APP_URL or RSA_URL
    if cart_id and GAS_WEB_APP_URL:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}cart={quote(str(cart_id))}"
    return base

RSA_DATA_START = 4   # RSA Tracker: rows 1-3 are titles/headers; data from row 4
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# RSA Tracker columns (1-indexed)
C_CARTID, C_STORE, C_STATE, C_TRIGGER, C_SVCDATE, C_RSAREQ, C_RSAVENDOR = 1, 2, 3, 4, 5, 6, 7
C_RSASCHED, C_ETS, C_RSASTATUS, C_RSACOMPL, C_WMREQ, C_WMSTATUS, C_WMDATE = 8, 9, 10, 11, 12, 13, 14
C_OVERALL, C_DAYS, C_DRI, C_LASTUPD, C_NOTES = 15, 16, 17, 18, 19
C_TECH_LINK, C_RSA_RESULTS = 20, 21    # T = Tech Upload Link, U = RSA Test Results (W&M pass/fail)
# Tech calibration results — written when a tech calibrates a scheduled cart, BEFORE
# the W&M test. Defaults to cols V/W; overridable, and auto-resolved by header name.
C_TECH_RESULTS      = int(os.environ.get("RSA_TECH_RESULTS_COL", "22"))       # V = Tech Results
C_TECH_RESULTS_LINK = int(os.environ.get("RSA_TECH_RESULTS_LINK_COL", "23"))  # W = Tech Results file link
RSA_HEADER_ROW = 3                      # RSA Tracker column-header row

# Compliance "Store Requirements" columns (1-indexed; data from row 3)
CG = {"retailer": 2, "nickname": 3, "city": 4, "state": 5, "county": 7,
      "preReq": 10, "preDir": 13, "preNotify": 14, "atReq": 15, "atDir": 18, "atNotify": 19,
      "postReq": 20, "postDir": 23, "postNotify": 24, "cadence": 25, "agency": 28}

# Truno Sheet1 columns (1-indexed)
T_STORE, T_CART, T_VISIT, T_NOTES, T_STATUS, T_FORMS = 1, 3, 6, 9, 11, 10
T_ADDRESS = 5                               # col E — store Address (Caper join key)

_gc = None


def _client():
    global _gc
    if _gc is None:
        creds = Credentials.from_service_account_file(SA_KEY_FILE, scopes=SCOPES)
        _gc = gspread.authorize(creds)
    return _gc


_ws_cache: dict = {}

def _ws(spreadsheet_id, sheet_name):
    key = (spreadsheet_id, sheet_name)
    if key not in _ws_cache:
        _ws_cache[key] = _retry(
            lambda: _client().open_by_key(spreadsheet_id).worksheet(sheet_name)
        )
    return _ws_cache[key]


def _cell(row, idx1):                       # 1-indexed column access, safe
    i = idx1 - 1
    return (row[i] if i < len(row) else "").strip() if isinstance(row, list) else ""


def _today():
    return datetime.now().strftime("%m/%d/%Y")


def is_transient_error(e):
    """True for transient network/connection hiccups (broken pipe, connection reset,
    timeouts, 5xx/429) that should be retried and logged rather than alerted on. Used by
    the pollers so a momentary blip doesn't flood Slack."""
    if isinstance(e, (ConnectionError, TimeoutError, OSError)):   # BrokenPipe/ConnReset are OSError
        return True
    status = getattr(getattr(e, "resp", None), "status", None)
    try:
        if status is not None and int(status) in (429, 500, 502, 503, 504):
            return True
    except (TypeError, ValueError):
        pass
    # gspread uses e.response (not e.resp) — check that too
    resp = getattr(e, "response", None)
    try:
        if int(getattr(resp, "status_code", 0) or 0) in (429, 500, 502, 503, 504):
            return True
    except (TypeError, ValueError):
        pass
    s = str(e).lower()
    return any(t in s for t in (
        "broken pipe", "connection reset", "connection aborted", "connection refused",
        "timed out", "timeout", "temporarily unavailable", "remote end closed",
        "eof occurred", "server not found", "handshake", "errno 32", "errno 54",
        "quota exceeded", "rate limit"))


import time as _time

def _retry(fn, *args, retries=5, base_delay=2.0, **kwargs):
    """Call fn(*args, **kwargs), retrying on 429/transient errors with exponential backoff.
    Waits 2s, 4s, 8s, 16s, 32s between attempts before re-raising."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt >= retries - 1 or not is_transient_error(e):
                raise
            delay = base_delay * (2 ** attempt)
            log.warning("Sheets API transient error (attempt %d/%d), retrying in %.0fs: %s",
                        attempt + 1, retries, delay, e)
            _time.sleep(delay)


def _get_all(ws):
    """Retry-wrapped ws.get_all_values()."""
    return _retry(ws.get_all_values)


def _update(ws, cells):
    """Retry-wrapped ws.update_cells(cells)."""
    return _retry(ws.update_cells, cells)


# ── Matching (ported from GAS _findCartRow) ─────────────────────────────────────
def _find_row(values, cart_id, store_hint):
    """values = data rows (each a list). Returns (sheet_row, row_list) or (None, None).

    Exact Cart-ID match always wins. A bare number (e.g. '1') is treated as a CART
    NUMBER — matched only against a serial's `_M3_<num>` suffix, never as a loose
    substring — and must be pinned by the store hint or be unique, so '1' can't
    silently select '..._M3_001' of an unrelated store (was a wrong-cart-write bug).
    A longer/partial serial may still match by containment."""
    q = str(cart_id or "").lower().strip()
    if not q:
        return None, None
    sh = str(store_hint or "").lower().strip()

    def store_ok(row):
        if not sh:
            return True
        rstore = _cell(row, C_STORE).lower()
        return sh in rstore or (rstore[:4] and rstore[:4] in q)

    # 1) exact Cart-ID match (prefer one that also satisfies the store hint)
    exact = [(RSA_DATA_START + i, r) for i, r in enumerate(values)
             if _cell(r, C_CARTID).lower() == q]
    if exact:
        return next((row for row in exact if store_ok(row[1])), exact[0])

    # 2) bare number → match the serial's cart-number suffix only (never a substring)
    bare = re.fullmatch(r"0*(\d+)([a-z]?)", q)
    if bare:
        want = (bare.group(1).lstrip("0") or "0") + bare.group(2)
        cand = []
        for i, r in enumerate(values):
            m = re.search(r"_m3_0*(\d+)([a-z]?)$", _cell(r, C_CARTID).lower())
            if m and ((m.group(1).lstrip("0") or "0") + m.group(2)) == want:
                cand.append((RSA_DATA_START + i, r))
        hinted = [c for c in cand if store_ok(c[1])]
        if len(hinted) == 1:
            return hinted[0]
        if not sh and len(cand) == 1:
            return cand[0]
        return None, None          # ambiguous bare number → caller asks; no wrong-cart write

    # 3) partial/serial-like containment (q has letters → not a bare number)
    best = None
    for i, row in enumerate(values):
        rid = _cell(row, C_CARTID).lower()
        if not rid or not (q in rid or rid in q):
            continue
        if store_ok(row):
            return RSA_DATA_START + i, row
        if best is None:
            best = (RSA_DATA_START + i, row)
    return best if best else (None, None)


_rsa_cache: dict   = {"ts": 0.0, "rows": None}
_truno_cache: dict = {"ts": 0.0, "rows": None}
# 120s TTL — long enough that pollers polling every 5 min share a single read even
# when they drift slightly out of phase; writes always bust the cache immediately.
_RSA_CACHE_TTL   = 120
_TRUNO_CACHE_TTL = 120


def _bust_rsa_cache():
    """Invalidate the cached RSA rows after any write so the next read is fresh."""
    _rsa_cache["ts"] = 0.0


def _bust_truno_cache():
    """Invalidate the cached TRUNO rows (call after any TRUNO write)."""
    _truno_cache["ts"] = 0.0


def _rsa_data(force=False):
    """Return RSA Tracker rows (from row RSA_DATA_START onward), with a TTL cache
    so multiple pollers running close together share a single Sheets API read instead of
    each issuing their own, which quickly exhausts the 60 req/min quota."""
    import time as _time
    now = _time.monotonic()
    if not force and _rsa_cache["rows"] is not None and (now - _rsa_cache["ts"]) < _RSA_CACHE_TTL:
        return _rsa_cache["rows"]
    vals = _get_all(_ws(RSA_SPREADSHEET_ID, RSA_SHEET))
    rows = vals[RSA_DATA_START - 1:]
    _rsa_cache["ts"] = now
    _rsa_cache["rows"] = rows
    return rows


def _truno_data(force=False):
    """Return TRUNO tracker rows (header stripped), with a TTL cache to avoid
    redundant reads when multiple pollers access it in the same window."""
    import time as _time
    now = _time.monotonic()
    if not force and _truno_cache["rows"] is not None and (now - _truno_cache["ts"]) < _TRUNO_CACHE_TTL:
        return _truno_cache["rows"]
    vals = _get_all(_ws(TRUNO_SPREADSHEET_ID, TRUNO_SHEET))
    rows = vals[1:]   # strip header
    _truno_cache["ts"] = now
    _truno_cache["rows"] = rows
    return rows


# ── READS ───────────────────────────────────────────────────────────────────────
def query_cart(cart_id, store_hint=None):
    try:
        row, data = _find_row(_rsa_data(), cart_id, store_hint)
        if not row:
            return {"found": False, "message": f'No cart found matching "{cart_id}".'}
        days = _cell(data, C_DAYS)
        return {
            "found": True, "cartId": _cell(data, C_CARTID), "store": _cell(data, C_STORE),
            "state": _cell(data, C_STATE), "trigger": _cell(data, C_TRIGGER),
            "rsaRequired": _cell(data, C_RSAREQ), "rsaVendor": _cell(data, C_RSAVENDOR),
            "rsaStatus": _cell(data, C_RSASTATUS), "rsaSchedDate": _cell(data, C_RSASCHED),
            "rsaComplDate": _cell(data, C_RSACOMPL),
            "wmStatus": _cell(data, C_WMSTATUS),
            "wmDate": _cell(data, C_WMDATE), "overallStatus": _cell(data, C_OVERALL),
            "days": days or None, "dri": _cell(data, C_DRI),
            "lastUpdated": _cell(data, C_LASTUPD), "notes": _cell(data, C_NOTES),
            "trackerUrl": webapp_url(_cell(data, C_CARTID)),
        }
    except Exception as e:
        return {"error": f"query_cart: {e}"}


def get_summary():
    try:
        rows = _rsa_data()
        def cnt(col, *vals):
            return sum(1 for r in rows if _cell(r, col) in vals)
        rsa = lambda r: _cell(r, C_RSASTATUS)
        wm  = lambda r: _cell(r, C_WMSTATUS)
        ov  = lambda r: _cell(r, C_OVERALL)
        nonempty = [r for r in rows if _cell(r, C_CARTID)]
        def stale():
            n = 0
            for r in nonempty:
                d = _cell(r, C_DAYS)
                try:
                    if float(d) >= 10 and ov(r) != "Passed":
                        n += 1
                except ValueError:
                    pass
            return n
        # W&M is out of scope: summaries/roll-ups track RSA only (the W&M columns remain
        # in the sheet but are no longer surfaced or counted).
        cutoff_30 = datetime.now() - timedelta(days=30)
        def _csm_pending_last30():
            n = 0
            for r in nonempty:
                if _cell(r, C_DRI) != CSM_DRI:
                    continue
                try:
                    if datetime.strptime(_cell(r, C_RSACOMPL).strip(), "%m/%d/%Y") >= cutoff_30:
                        n += 1
                except (ValueError, AttributeError):
                    pass
            return n
        counts = {
            "total": len(nonempty),
            "pendingTrunoScheduled": sum(1 for r in nonempty if rsa(r) == "Pending Truno Scheduled"),
            "pendingRSA": sum(1 for r in nonempty if rsa(r) == "Pending RSA"),
            "scheduledRSA": sum(1 for r in nonempty if rsa(r) == "Scheduled RSA"),
            "completedRSA": sum(1 for r in nonempty if rsa(r) == "Completed RSA"),
            "failedRSA": sum(1 for r in nonempty if rsa(r) == "Failed RSA"),
            "overallPassed": sum(1 for r in nonempty if ov(r) == "Passed"),
            "needsAction": sum(1 for r in nonempty if ov(r) in ("Redispatch Needed", "Needs Verification")
                               or rsa(r) == "Failed RSA"),
            "stale10plus": stale(),
            "csmPendingThisMonth": _csm_pending_last30(),
        }
        by_state = {}
        for r in nonempty:
            st = _cell(r, C_STATE)
            if st:
                by_state[st] = by_state.get(st, 0) + 1
        return {"counts": counts, "byState": by_state, "trackerUrl": webapp_url()}
    except Exception as e:
        return {"error": f"get_summary: {e}"}


def get_monthly_summary(month=None, year=None):
    """Return completed and scheduled RSA visit counts for a given month/year.

    Sources:
    • RSA Tracker  — C_RSACOMPL (col 11) for completed visit date; C_RSASCHED (col 8)
                     for the scheduled RSA date when rsaStatus = 'Scheduled RSA'.
    • TRUNO sheet  — T_VISIT (col 6) for the TRUNO-scheduled visit date.

    month: 1-12 integer (defaults to current month)
    year:  4-digit integer (defaults to current year)
    Dates in both sheets are MM/DD/YYYY."""
    try:
        now = datetime.now()
        m = int(month) if month else now.month
        y = int(year) if year else now.year

        def _in_month(date_str):
            """True when a MM/DD/YYYY string falls in the requested month/year."""
            s = str(date_str or "").strip()
            if not s:
                return False
            try:
                d = datetime.strptime(s, "%m/%d/%Y")
                return d.month == m and d.year == y
            except ValueError:
                return False

        rsa_rows = [r for r in _rsa_data() if _cell(r, C_CARTID)]

        # ── Completed ──────────────────────────────────────────────────────────
        completed = []
        for r in rsa_rows:
            if _cell(r, C_RSASTATUS) == "Completed RSA" and _in_month(_cell(r, C_RSACOMPL)):
                completed.append({
                    "cartId":  _cell(r, C_CARTID),
                    "store":   _cell(r, C_STORE),
                    "state":   _cell(r, C_STATE),
                    "vendor":  _cell(r, C_RSAVENDOR),
                    "dri":     _cell(r, C_DRI),
                    "date":    _cell(r, C_RSACOMPL),
                    "overall": _cell(r, C_OVERALL),
                })

        # ── Scheduled from RSA Tracker (rsaSchedDate column) ─────────────────
        rsa_scheduled = []
        for r in rsa_rows:
            status = _cell(r, C_RSASTATUS)
            if status in ("Scheduled RSA", "Pending Truno Scheduled") and _in_month(_cell(r, C_RSASCHED)):
                rsa_scheduled.append({
                    "cartId": _cell(r, C_CARTID),
                    "store":  _cell(r, C_STORE),
                    "state":  _cell(r, C_STATE),
                    "vendor": _cell(r, C_RSAVENDOR),
                    "dri":    _cell(r, C_DRI),
                    "date":   _cell(r, C_RSASCHED),
                    "status": status,
                })

        # ── Scheduled from TRUNO sheet (visit date column) ────────────────────
        truno_scheduled = []
        try:
            truno_rows = _get_all(_ws(TRUNO_SPREADSHEET_ID, TRUNO_SHEET))[1:]
            for r in truno_rows:
                store = _cell(r, T_STORE)
                visit = _cell(r, T_VISIT)
                if store and _in_month(visit):
                    truno_scheduled.append({
                        "store":  store,
                        "cart":   _cell(r, T_CART),
                        "visit":  visit,
                        "status": _cell(r, T_STATUS),
                    })
        except Exception as te:
            log.warning("monthly_summary: could not read TRUNO sheet: %s", te)

        month_label = datetime(y, m, 1).strftime("%B %Y")
        return {
            "month": month_label,
            "completed": completed,
            "completedCount": len(completed),
            "rsaScheduled": rsa_scheduled,
            "rsaScheduledCount": len(rsa_scheduled),
            "trunoScheduled": truno_scheduled,
            "trunoScheduledCount": len(truno_scheduled),
            "trackerUrl": webapp_url(),
            "trunoUrl": TRUNO_URL,
        }
    except Exception as e:
        return {"error": f"get_monthly_summary: {e}"}


def upcoming_visits(store_fragment=None, cart_number=None, days_ahead=60):
    """Return carts with a future (or today) RSA Scheduled Date, optionally filtered
    by store name fragment and/or cart number.  Also checks the Truno tracker for a
    visit date when the RSA Tracker row has no scheduled date yet.

    Returns a list sorted by visit date ascending, with a trackerUrl per cart.
    """
    try:
        today = datetime.now().date()
        cutoff = today + timedelta(days=int(days_ahead))
        sf = (store_fragment or "").lower().strip()
        cn = str(cart_number or "").strip().lstrip("0") or None   # normalise "007" → "7"

        # ── RSA Tracker scheduled dates ──────────────────────────────────────
        visits = []
        for r in _rsa_data():
            cart_id = _cell(r, C_CARTID)
            if not cart_id:
                continue
            if sf and sf not in _cell(r, C_STORE).lower():
                continue
            # cart number filter: match the numeric suffix of the serial id (e.g. "001" → "1")
            if cn:
                serial_num = re.sub(r"^.*?(\d+)$", r"\1", cart_id).lstrip("0") or "0"
                if serial_num != cn:
                    continue

            sched_raw = _cell(r, C_RSASCHED).strip()
            visit_dt = None
            source = "RSA Tracker"
            try:
                visit_dt = datetime.strptime(sched_raw, "%m/%d/%Y").date()
            except (ValueError, AttributeError):
                pass

            if visit_dt and today <= visit_dt <= cutoff:
                visits.append({
                    "cartId": cart_id,
                    "store": _cell(r, C_STORE),
                    "state": _cell(r, C_STATE),
                    "rsaStatus": _cell(r, C_RSASTATUS),
                    "rsaVendor": _cell(r, C_RSAVENDOR),
                    "visitDate": visit_dt.strftime("%m/%d/%Y"),
                    "source": source,
                    "trackerUrl": webapp_url(cart_id),
                })

        # ── Truno tracker visit dates (for carts not yet in RSA or missing sched date) ──
        seen_ids = {v["cartId"] for v in visits}
        try:
            truno_rows = _get_all(_ws(TRUNO_SPREADSHEET_ID, TRUNO_SHEET))[1:]
            for tr in truno_rows:
                t_store = _cell(tr, T_STORE)
                t_cart  = str(_cell(tr, T_CART) or "").strip()
                t_visit = str(_cell(tr, T_VISIT) or "").strip()
                if not t_store or not t_visit:
                    continue
                if sf and sf not in t_store.lower():
                    continue
                if cn:
                    t_num = re.sub(r"\D", "", t_cart).lstrip("0") or "0"
                    if t_num != cn:
                        continue
                try:
                    v_dt = datetime.strptime(t_visit, "%m/%d/%Y").date()
                except ValueError:
                    continue
                if not (today <= v_dt <= cutoff):
                    continue
                # build a synthetic id to check dedup; skip if already in RSA results
                synthetic = f"{t_store}|{t_cart}"
                if any(synthetic in v["cartId"] for v in visits):
                    continue
                visits.append({
                    "cartId": t_cart or synthetic,
                    "store": t_store,
                    "state": "",
                    "rsaStatus": str(_cell(tr, T_STATUS) or "").strip(),
                    "rsaVendor": "TRUNO",
                    "visitDate": v_dt.strftime("%m/%d/%Y"),
                    "source": "Truno",
                    "trackerUrl": TRUNO_URL,
                })
        except Exception:
            pass  # Truno read failure is non-fatal; RSA Tracker results still returned

        visits.sort(key=lambda v: datetime.strptime(v["visitDate"], "%m/%d/%Y"))
        return {"visits": visits, "total": len(visits), "daysAhead": days_ahead,
                "storeFilter": store_fragment or None, "cartFilter": cart_number or None}
    except Exception as e:
        return {"error": f"upcoming_visits: {e}"}


def list_carts(state=None, rsaStatus=None, wmStatus=None, overallStatus=None,
               dri=None, storeFragment=None, staleOnly=None, limit=None, since_days=None):
    try:
        rows = [r for r in _rsa_data() if _cell(r, C_CARTID)]
        sf = (storeFragment or "").lower()
        cutoff = (datetime.now() - timedelta(days=int(since_days))) if since_days is not None else None
        out = []
        for r in rows:
            if state and _cell(r, C_STATE) != state:
                continue
            if rsaStatus and _cell(r, C_RSASTATUS) != rsaStatus:
                continue
            if wmStatus and _cell(r, C_WMSTATUS) != wmStatus:
                continue
            if overallStatus and _cell(r, C_OVERALL) != overallStatus:
                continue
            if dri and dri.lower() not in _cell(r, C_DRI).lower():
                continue
            if sf and sf not in _cell(r, C_STORE).lower():
                continue
            if cutoff is not None:
                try:
                    compl_dt = datetime.strptime(_cell(r, C_RSACOMPL).strip(), "%m/%d/%Y")
                    if compl_dt < cutoff:
                        continue
                except (ValueError, AttributeError):
                    continue   # no valid completion date — exclude from date-filtered results
            if staleOnly:
                d = _cell(r, C_DAYS)
                try:
                    if float(d) < 10 or _cell(r, C_OVERALL) == "Passed":
                        continue
                except ValueError:
                    continue
            out.append(r)
        lim = min(int(limit), 50) if str(limit or "").isdigit() else 20
        sliced = out[:lim]
        return {
            "total": len(out), "shown": len(sliced),
            "carts": [{
                "cartId": _cell(r, C_CARTID), "store": _cell(r, C_STORE), "state": _cell(r, C_STATE),
                "rsaStatus": _cell(r, C_RSASTATUS), "rsaSchedDate": _cell(r, C_RSASCHED),
                "rsaComplDate": _cell(r, C_RSACOMPL),
                "wmStatus": _cell(r, C_WMSTATUS),
                "overallStatus": _cell(r, C_OVERALL), "days": _cell(r, C_DAYS) or None,
                "dri": _cell(r, C_DRI), "lastUpdated": _cell(r, C_LASTUPD),
            } for r in sliced],
            "trackerUrl": webapp_url(),
        }
    except Exception as e:
        return {"error": f"list_carts: {e}"}


def scheduled_no_truno():
    """Return RSA Tracker carts with RSA Status = 'Scheduled RSA' that have no
    matching row in the Truno tracker.  Matching is done by normalised store-name
    tokens + cart number so minor spelling differences don't cause false positives.
    Returns {carts: [...], total, trunoUrl}."""
    try:
        # ── Build a set of (norm_store, norm_cart) keys from Truno ───────────
        truno_rows = _get_all(_ws(TRUNO_SPREADSHEET_ID, TRUNO_SHEET))[1:]
        truno_keys = set()
        for tr in truno_rows:
            ts = _norm(_cell(tr, T_STORE))
            tc = re.sub(r"\D", "", str(_cell(tr, T_CART) or "")).lstrip("0") or "0"
            if ts:
                truno_keys.add((ts, tc))
                truno_keys.add((ts, ""))   # store-only key so a match on store alone counts

        # ── Find RSA Tracker carts stuck at Scheduled RSA with no Truno row ──
        out = []
        for r in _rsa_data():
            cart_id = _cell(r, C_CARTID)
            if not cart_id:
                continue
            if _cell(r, C_RSASTATUS) != "Scheduled RSA":
                continue
            rs = _norm(_cell(r, C_STORE))
            rc = re.sub(r"^.*?(\d+)$", r"\1", cart_id).lstrip("0") or "0"
            # Match if Truno has this store (with or without matching cart number)
            if (rs, rc) in truno_keys or (rs, "") in truno_keys:
                continue   # found in Truno — skip
            out.append({
                "cartId": cart_id,
                "store": _cell(r, C_STORE),
                "state": _cell(r, C_STATE),
                "rsaStatus": _cell(r, C_RSASTATUS),
                "rsaVendor": _cell(r, C_RSAVENDOR),
                "rsaSchedDate": _cell(r, C_RSASCHED),
                "overallStatus": _cell(r, C_OVERALL),
                "dri": _cell(r, C_DRI),
                "lastUpdated": _cell(r, C_LASTUPD),
                "trackerUrl": webapp_url(cart_id),
            })
        return {"carts": out, "total": len(out), "trunoUrl": TRUNO_URL,
                "trackerUrl": webapp_url()}
    except Exception as e:
        return {"error": f"scheduled_no_truno: {e}"}


def truno_confirmed_rows():
    """Truno rows where 'Forms sent to team?' (col J) = Yes — the trigger for
    ingesting test results. Returns store + cart + visit date per row."""
    vals = _get_all(_ws(TRUNO_SPREADSHEET_ID, TRUNO_SHEET))[1:]   # data from row 2
    out = []
    for r in vals:
        store = _cell(r, T_STORE)
        if store and "yes" in _cell(r, T_FORMS).lower():
            out.append({"store": store, "trunoStore": _cell(r, 2), "cart": _cell(r, T_CART),
                        "visit": _cell(r, T_VISIT), "status": _cell(r, T_STATUS),
                        "forms": _cell(r, T_FORMS)})
    return out


def _canon_truno_status(raw):
    """Free-form TRUNO status → scheduled | completed | cancelled | pending.
    Mirrors truno_monitor._canon_status; blank/other counts as 'pending' (a cart
    that's in the TRUNO tracker but still awaiting a confirmed visit date)."""
    s = str(raw or "").strip().lower()
    if "cancel" in s or "reschedule" in s or "closed" in s:
        return "cancelled"
    if "complet" in s:
        return "completed"
    if "schedul" in s and "pending" not in s:
        return "scheduled"
    return "pending"


def truno_progress_summary(max_list=8):
    """Summarize the TRUNO tracker for the Home tab: carts in progress (scheduled +
    awaiting a date), plus completed / cancelled for context, and a short list of the
    in-progress carts (soonest confirmed date first). Never raises."""
    try:
        rows = _truno_data()
        buckets = {"scheduled": 0, "pending": 0, "completed": 0, "cancelled": 0}
        in_progress = []
        for r in rows:
            store = _cell(r, T_STORE)
            if not store:
                continue
            cart = str(_cell(r, T_CART) or "").strip()
            # Skip rows whose Cart cell is actually a date (a shifted / malformed row).
            if re.search(r"\b(19|20)\d{2}\b", cart):
                continue
            st = _canon_truno_status(_cell(r, T_STATUS))
            buckets[st] = buckets.get(st, 0) + 1
            if st in ("scheduled", "pending"):
                in_progress.append({
                    "store": store,
                    "cart": cart,
                    "visit": str(_cell(r, T_VISIT) or "").strip(),
                    "status": "Scheduled" if st == "scheduled" else "Awaiting date",
                })

        def _key(v):
            try:
                return (0, datetime.strptime(v["visit"], "%m/%d/%Y"))
            except (ValueError, AttributeError):
                return (1, datetime.max)   # undated (awaiting) carts sort last
        in_progress.sort(key=_key)

        n_ip = len(in_progress)
        return {
            "inProgressTotal": buckets["scheduled"] + buckets["pending"],
            "scheduled": buckets["scheduled"],
            "pending": buckets["pending"],
            "completed": buckets["completed"],
            "cancelled": buckets["cancelled"],
            "inProgress": in_progress[:max_list],
            "moreInProgress": max(0, n_ip - max_list),
            "trunoUrl": TRUNO_URL,
        }
    except Exception as e:
        return {"error": f"truno_progress_summary: {e}"}


# ── Compliance (ported from GAS _lookupCompliance) ──────────────────────────────
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _gate(trigger):
    t = str(trigger or "").lower()
    if re.search(r"launch|first deploy|new cart|full cart swap|cart replacement", t):
        return "AT-LAUNCH"
    return "POST-LAUNCH"


def lookup_compliance(store, state, trigger=None, address=None):
    """Return ONLY the pre-launch and post-launch requirements for a store.
    Matches within the state by store name + address/location (nickname/city/county)."""
    out = {"matched": False, "state": str(state or "").upper().strip(), "store": store or ""}
    try:
        vals = _get_all(_ws(COMPLIANCE_SPREADSHEET_ID, COMPLIANCE_SHEET))
        data = vals[2:]                              # data from row 3
        st = out["state"]
        match_text = _norm(f"{store or ''} {address or ''}")   # store name + address/location
        in_state = [r for r in data if _cell(r, CG["state"]).upper() == st]
        if not in_state:
            out["note"] = "state not in compliance guide"
            out["candidates"] = compliance_candidates(store, None, address)   # loose matches across states
            out["slackText"] = _compliance_text(out)
            return out
        # Refine to a specific store: an EXACT nickname/retailer match wins; otherwise a
        # nickname / city / county token appearing in the store name or address the user gave.
        row = None
        for r in in_state:
            if match_text and any(_norm(_cell(r, CG[c])) == match_text for c in ("nickname", "retailer")):
                row = r
                break
        if not row:
            for r in in_state:
                for col in ("nickname", "city", "county"):
                    tok = _norm(_cell(r, CG[col]))
                    if len(tok) >= 4 and tok in match_text:
                        row = r
                        break
                if row:
                    break
        store_matched = row is not None
        if not row:
            row = in_state[0]
            # No confident store match → offer the closest names so the user can pick one.
            out["candidates"] = compliance_candidates(store, st, address) or compliance_candidates(store, None, address)
        out.update({
            "matched": True, "storeMatched": store_matched,
            "retailer": _cell(row, CG["retailer"]), "nickname": _cell(row, CG["nickname"]),
            "county": _cell(row, CG["county"]),
            "preReq": _cell(row, CG["preReq"]), "preDirective": _cell(row, CG["preDir"]),
            "preNotify": _cell(row, CG["preNotify"]),
            "postReq": _cell(row, CG["postReq"]), "postDirective": _cell(row, CG["postDir"]),
            "postNotify": _cell(row, CG["postNotify"]),
            "agency": _cell(row, CG["agency"]), "cadence": _cell(row, CG["cadence"]),
        })
        flags = []
        if re.search(r"confirm locally", f"{out['preReq']} {out['postReq']}", re.I):
            flags.append('Path is "Confirm Locally" — verify RSA vs county path before scheduling.')
        if st == "NY":
            flags.append("NY county exception: Dutchess / Orange / Westchester may allow conditional deployment.")
        if not store_matched:
            flags.append("Matched by state only (no store/address match) — confirm the specific store.")
        out["flags"] = flags
        out["slackText"] = _compliance_text(out)
        return out
    except Exception as e:
        out["error"] = f"lookup_compliance: {e}"
        return out


def _compliance_text(c):
    """Pre-launch + post-launch requirements only (no at-launch / authority dump)."""
    if not c.get("matched"):
        return (f":scroll: *RSA / W&M requirements — {c.get('store')} ({c.get('state') or '?'})*\n"
                ":warning: No matching store/state in the Compliance Guide — confirm manually.")
    loc = c["state"] + (f" · {c['county']} County" if c.get("county") else "")
    L = [f":scroll: *RSA / W&M requirements — {c.get('store')} ({loc})*",
         f"*Pre-launch — {c.get('preReq') or 'N/A'}*",
         f"   {c.get('preDirective') or 'N/A'}"]
    if c.get("preNotify"):
        L.append(f"   _Notify:_ {c['preNotify']}")
    L.append(f"*Post-launch — {c.get('postReq') or 'N/A'}*")
    L.append(f"   {c.get('postDirective') or 'N/A'}")
    if c.get("postNotify"):
        L.append(f"   _Notify:_ {c['postNotify']}")
    for f in c.get("flags", []):
        L.append(f":warning: {f}")
    return "\n".join(L)


def compliance_candidates(store, state=None, address=None, limit=8):
    """Loose matches for a store in the Compliance Guide — by nickname / retailer / city —
    so the agent can offer a pick list when there's no single confident match
    (e.g. 'bigbunny-1' → 'Big Bunny', 'Big Bunny Market'). A 2-letter `state` narrows the
    search; otherwise all states are searched. Ranked best-first; de-duped by (name, state)."""
    try:
        vals = _get_all(_ws(COMPLIANCE_SPREADSHEET_ID, COMPLIANCE_SHEET))
    except Exception:
        return []
    data = vals[2:]
    st = str(state or "").upper().strip()
    full = _norm(store)                                                # query as typed
    base = _norm(re.sub(r"[\s\-_#]*\d+\s*$", "", str(store or "")))     # …with a trailing store-number dropped
    qtok = {t for t in re.findall(r"[a-z]{3,}", str(store or "").lower())}
    if not full and not qtok:
        return []
    out = []
    for r in data:
        rstate = _cell(r, CG["state"]).upper()
        if st and rstate and rstate != st:
            continue
        retailer, nick, city = _cell(r, CG["retailer"]), _cell(r, CG["nickname"]), _cell(r, CG["city"])
        score = 0
        for nm in (nick, retailer, city):
            nn = _norm(nm)
            if not nn:
                continue
            if full and full == nn:
                score = max(score, 100)                        # exact, including the store number
            elif base and base == nn:
                score = max(score, 90)                         # exact once a trailing number is dropped
            elif full and len(full) >= 3 and (full in nn or nn in full):
                score = max(score, 85)                         # substring (number kept)
            elif base and len(base) >= 3 and (base in nn or nn in base):
                score = max(score, 80)                         # substring (number dropped)
            else:
                common = qtok & {t for t in re.findall(r"[a-z]{3,}", nm.lower())}
                if common:
                    score = max(score, 40 + 10 * len(common))  # shared word(s)
        if score:
            out.append({"retailer": retailer, "nickname": nick, "city": city,
                        "county": _cell(r, CG["county"]), "state": rstate, "score": score})
    out.sort(key=lambda c: (-c["score"], c.get("nickname") or c.get("retailer") or ""))
    seen, uniq = set(), []
    for c in out:
        k = ((c.get("nickname") or c.get("retailer") or "").lower(), c["state"])
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq[:limit]


# ── WRITES ──────────────────────────────────────────────────────────────────────
def update_cart(cartId, storeHint=None, rsaStatus=None, rsaVendor=None, rsaSchedDate=None,
                wmStatus=None, wmDate=None, overallStatus=None, dri=None, notes=None, appendNotes=True,
                trigger=None):
    try:
        ws = _ws(RSA_SPREADSHEET_ID, RSA_SHEET)
        values = _get_all(ws)[RSA_DATA_START - 1:]
        row, data = _find_row(values, cartId, storeHint)
        if not row:
            return {"error": f'Cart not found: "{cartId}"'}
        updates, labels = [], []
        def put(col, val, label):
            updates.append(gspread.Cell(row, col, val))
            labels.append(label)
        if trigger:       put(C_TRIGGER, trigger, "Service Trigger")
        if rsaVendor:     put(C_RSAVENDOR, rsaVendor, "RSA Vendor")
        if rsaSchedDate:  put(C_RSASCHED, rsaSchedDate, "RSA Scheduled Date")
        if rsaStatus:     put(C_RSASTATUS, rsaStatus, "RSA Status")
        if rsaStatus == "Completed RSA":
            put(C_RSACOMPL, _today(), "RSA Completion Date")
        if wmStatus:      put(C_WMSTATUS, wmStatus, "W&M Status")
        if wmDate:        put(C_WMDATE, wmDate, "W&M Date")
        if overallStatus: put(C_OVERALL, overallStatus, "Overall Status")
        if dri:           put(C_DRI, dri, "DRI")
        if notes:
            if appendNotes:
                existing = _cell(data, C_NOTES)
                note_val = f"{existing}\n[{_today()}] {notes}" if existing else f"[{_today()}] {notes}"
            else:
                note_val = notes
            put(C_NOTES, note_val, "Notes")
        if not updates:
            return {"error": "No fields to update were provided."}
        if rsaStatus or wmStatus:
            put(C_LASTUPD, _today(), "Last Updated")
        _update(ws, updates)
        _bust_rsa_cache()
        return {"cartId": _cell(data, C_CARTID), "store": _cell(data, C_STORE),
                "updated": labels, "trackerUrl": webapp_url(_cell(data, C_CARTID))}
    except Exception as e:
        return {"error": f"update_cart: {e}"}


def _days_stale(row):
    """Days since a cart's last RSA update: the tracker's 'Days' column (P) if numeric,
    else today − 'Last Updated' (R). Returns a float or None when it can't be told."""
    d = _cell(row, C_DAYS)
    try:
        return float(d)
    except (TypeError, ValueError):
        pass
    last = _cell(row, C_LASTUPD)
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return (datetime.now() - datetime.strptime(last.strip(), fmt)).days
        except (ValueError, AttributeError):
            continue
    return None


def bulk_set_rsa_status(cart_ids, rsaStatus="Completed RSA", overallStatus="Passed",
                        wmStatus=None, note=None, submittedBy="RSA Agent", dry_run=False,
                        min_stale_days=None):
    """Set the same RSA Status (and, by default, Overall Status) on MANY carts in ONE
    batched write — for clearing a backlog (e.g. the Winter Scales stale-RSA carts).
    Optionally set the W&M Status column too (wmStatus) — needed to silence the legacy
    W&M-keyed scheduling reminders, which look at the W&M column, not RSA/Overall.
    Matches each id against col A exactly (case-insensitive). When min_stale_days is set,
    ONLY carts whose days-since-update is strictly greater than that threshold are
    touched — a recently-updated cart is left alone and reported under 'skippedRecent'.
    With dry_run=True nothing is written; the returned lists let a caller preview first.
    Returns {count, found:[{cartId,row,store,days}], skippedRecent:[...], missing:[ids], ...}."""
    try:
        ws = _ws(RSA_SPREADSHEET_ID, RSA_SHEET)
        data = _get_all(ws)[RSA_DATA_START - 1:]
        index = {}
        for i, r in enumerate(data):
            cid = _cell(r, C_CARTID)
            if cid:
                index[cid.strip().lower()] = (RSA_DATA_START + i, r)
        cells, found, skipped_recent, missing, seen = [], [], [], [], set()
        for cid in cart_ids:
            key = str(cid or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            hit = index.get(key)
            if not hit:
                missing.append(cid)
                continue
            row, r = hit
            days = _days_stale(r)
            if min_stale_days is not None and not (isinstance(days, (int, float)) and days > min_stale_days):
                # not stale enough (or staleness unknown) → don't touch it
                skipped_recent.append({"cartId": _cell(r, C_CARTID), "store": _cell(r, C_STORE), "days": days})
                continue
            found.append({"cartId": _cell(r, C_CARTID), "row": row, "store": _cell(r, C_STORE), "days": days})
            if dry_run:
                continue
            if rsaStatus:
                cells.append(gspread.Cell(row, C_RSASTATUS, rsaStatus))
            if overallStatus:
                cells.append(gspread.Cell(row, C_OVERALL, overallStatus))
            if wmStatus:
                cells.append(gspread.Cell(row, C_WMSTATUS, wmStatus))
            if note:
                existing = _cell(r, C_NOTES)
                stamped = f"[{_today()} · {submittedBy}] {note}"
                cells.append(gspread.Cell(row, C_NOTES, f"{existing}\n{stamped}" if existing else stamped))
            cells.append(gspread.Cell(row, C_LASTUPD, _today()))
        if cells and not dry_run:
            _update(ws, cells)
            _bust_rsa_cache()
        return {"count": len(found), "found": found, "skippedRecent": skipped_recent,
                "missing": missing, "rsaStatus": rsaStatus, "overallStatus": overallStatus,
                "wmStatus": wmStatus, "minStaleDays": min_stale_days, "dryRun": dry_run}
    except Exception as e:
        return {"error": f"bulk_set_rsa_status: {e}"}


CSM_DRI   = os.environ.get("CSM_DRI_NAME", "CSM")        # DRI for the Completed-RSA + Pending-W&M handoff
HWOPS_DRI = os.environ.get("HWOPS_DRI_NAME", "HW Ops")   # default owner otherwise


def normalize_tracker(dry_run=False):
    """Apply the standing tracker rules in ONE batched write:
      • RSA Status → 'Completed RSA' where Overall Status == 'Passed' and
        RSA Status == 'Scheduled RSA' (stale scheduled flag on an already-passed cart).
      • Overall Status → 'Passed' where RSA Status == 'Completed RSA'.
      • DRI routing (only for auto-managed DRI values: 'HW Ops', 'CSM', 'TBD', blank —
        named individuals are never overwritten):
          - RSA Status != 'Completed RSA' → 'HW Ops'
          - RSA Status == 'Completed RSA' → 'CSM'
    Only rows that actually need a change are written, so repeat runs converge and go
    quiet. Does NOT touch 'Last Updated'. dry_run previews without writing.
    Returns {rsaFixed, overallPassed, driCsm, driHwops, driReverted, changedRows, dryRun}."""
    try:
        ws = _ws(RSA_SPREADSHEET_ID, RSA_SHEET)
        data = _get_all(ws)[RSA_DATA_START - 1:]
        cells, rsa_fixed_n, overall_n, csm_n, hwops_n, revert_n, changed_rows = [], 0, 0, 0, 0, 0, 0
        for i, r in enumerate(data):
            if not _cell(r, C_CARTID):
                continue
            row, changed = RSA_DATA_START + i, False
            rsa, wm, dri = _cell(r, C_RSASTATUS), _cell(r, C_WMSTATUS), _cell(r, C_DRI)
            # Rule 1: Overall=Passed but RSA still stuck at Scheduled RSA → correct to Completed RSA
            # (process first so the DRI/Overall rules below see the corrected status)
            if _cell(r, C_OVERALL) == "Passed" and rsa == "Scheduled RSA":
                cells.append(gspread.Cell(row, C_RSASTATUS, "Completed RSA"))
                rsa = "Completed RSA"   # update local so rules below cascade correctly
                rsa_fixed_n += 1; changed = True
            # Rule 2: Completed RSA → Overall Passed
            if rsa == "Completed RSA" and _cell(r, C_OVERALL) != "Passed":
                cells.append(gspread.Cell(row, C_OVERALL, "Passed")); overall_n += 1; changed = True
            # Rule 3: DRI routing
            if rsa == "Completed RSA" and dri != CSM_DRI:
                cells.append(gspread.Cell(row, C_DRI, CSM_DRI)); csm_n += 1; changed = True
            elif rsa != "Completed RSA" and dri != HWOPS_DRI:
                cells.append(gspread.Cell(row, C_DRI, HWOPS_DRI)); hwops_n += 1; changed = True
            if changed:
                changed_rows += 1
        if cells and not dry_run:
            _update(ws, cells)
            _bust_rsa_cache()
        return {"rsaFixed": rsa_fixed_n, "overallPassed": overall_n, "driCsm": csm_n,
                "driHwops": hwops_n, "driReverted": revert_n, "changedRows": changed_rows,
                "dryRun": dry_run}
    except Exception as e:
        return {"error": f"normalize_tracker: {e}"}


def _next_row(ws, col, start):
    colvals = _retry(ws.col_values, col)
    return max(start, len(colvals) + 1)


RSA_STATUS_NEW = "Pending Truno Scheduled"   # initial RSA status on onboarding (no date yet)


def add_cart(cartId, store, state="NJ", trigger="", rsaStatus=RSA_STATUS_NEW, rsaVendor="TBD",
             wmStatus="Pending W&M", overallStatus="Pending RSA", dri="HW Ops", notes=""):
    ws = _ws(RSA_SPREADSHEET_ID, RSA_SHEET)
    values = _get_all(ws)[RSA_DATA_START - 1:]
    existing, _ = _find_row(values, cartId, store)
    if existing:
        return {"error": f'Cart "{cartId}" already exists in the tracker.', "row": existing}
    r = _next_row(ws, C_CARTID, RSA_DATA_START)
    cells = [
        gspread.Cell(r, C_CARTID, cartId), gspread.Cell(r, C_STORE, store),
        gspread.Cell(r, C_STATE, (state or "NJ").upper()), gspread.Cell(r, C_TRIGGER, trigger or ""),
        gspread.Cell(r, C_SVCDATE, _today()), gspread.Cell(r, C_RSAREQ, "Yes"),
        gspread.Cell(r, C_RSAVENDOR, rsaVendor or "TBD"), gspread.Cell(r, C_RSASTATUS, rsaStatus or RSA_STATUS_NEW),
        gspread.Cell(r, C_WMREQ, "Yes"), gspread.Cell(r, C_WMSTATUS, wmStatus or "Pending W&M"),
        gspread.Cell(r, C_OVERALL, overallStatus or "Pending RSA"), gspread.Cell(r, C_DRI, dri or "HW Ops"),
        gspread.Cell(r, C_LASTUPD, _today()), gspread.Cell(r, C_NOTES, notes or "Added via RSA Agent bot"),
    ]
    _update(ws, cells)
    _bust_rsa_cache()
    return {"action": "added", "cartId": cartId, "store": store, "row": r, "trackerUrl": webapp_url(cartId)}


def ensure_results_column():
    """Add the 'RSA Test Results' header in column U if it isn't there yet."""
    ws = _ws(RSA_SPREADSHEET_ID, RSA_SHEET)
    try:
        current = _retry(ws.cell, RSA_HEADER_ROW, C_RSA_RESULTS).value
    except Exception:
        current = None
    if not current:
        _update(ws, [gspread.Cell(RSA_HEADER_ROW, C_RSA_RESULTS, "RSA Test Results")])
        return {"added": True}
    return {"added": False, "existing": current}


def set_test_result(cart_id, store=None, result_text="", passed=None, link=None):
    """Write an RSA inspection result onto a cart: RSA Status from pass/fail,
    the raw result text into the new RSA Test Results column (U), and a report link (T)."""
    try:
        ws = _ws(RSA_SPREADSHEET_ID, RSA_SHEET)
        values = _get_all(ws)[RSA_DATA_START - 1:]
        row, data = _find_row(values, cart_id, store)
        if not row:
            return {"error": f'Cart not found: "{cart_id}"', "cartId": cart_id}
        cells, labels = [], []
        if passed is True:
            cells.append(gspread.Cell(row, C_RSASTATUS, "Completed RSA")); labels.append("RSA Status → Completed RSA")
            cells.append(gspread.Cell(row, C_RSACOMPL, _today()))
            # W&M is out of scope, so a passed RSA is fully done → Overall = Passed.
            cells.append(gspread.Cell(row, C_OVERALL, "Passed")); labels.append("Overall → Passed")
        elif passed is False:
            cells.append(gspread.Cell(row, C_RSASTATUS, "Failed RSA")); labels.append("RSA Status → Failed RSA")
        if result_text:
            cells.append(gspread.Cell(row, C_RSA_RESULTS, result_text)); labels.append("RSA Test Results")
        if link:
            cells.append(gspread.Cell(row, C_TECH_LINK, link)); labels.append("Report link")
        cells.append(gspread.Cell(row, C_LASTUPD, _today()))
        _update(ws, cells)
        _bust_rsa_cache()
        return {"cartId": _cell(data, C_CARTID), "store": _cell(data, C_STORE), "updated": labels, "trackerUrl": webapp_url(_cell(data, C_CARTID))}
    except Exception as e:
        return {"error": f"set_test_result: {e}"}


# ── Tech calibration results (col V) ────────────────────────────────────────────
# A tech calibrates a scheduled cart and records load/shift readings; those land in
# the "Tech Results" column. This is informational and precedes the W&M test, so it
# does NOT change RSA/W&M status (unlike set_test_result).
_TECH_COLS = None      # (results_col, link_col) once resolved against the live header


def _find_header(hdr, *names):
    want = {n.lower() for n in names}
    for i, v in enumerate(hdr, start=1):
        if str(v).strip().lower() in want:
            return i
    return None


def ensure_tech_results_columns():
    """Make sure 'Tech Results' and 'Tech Results Link' headers exist on the RSA
    Tracker. If they're already there (any column), resolve to those; otherwise create
    them — at the default cols (V/W) when free, else appended after the last header so
    nothing is clobbered. Caches the resolved columns for set_tech_result()."""
    global _TECH_COLS
    ws = _ws(RSA_SPREADSHEET_ID, RSA_SHEET)
    vals = _get_all(ws)
    hdr = vals[RSA_HEADER_ROW - 1] if len(vals) >= RSA_HEADER_ROW else []

    def free(col):
        return len(hdr) < col or not str(hdr[col - 1]).strip()

    res = _find_header(hdr, "Tech Results", "Tech Result", "Tech Calibration")
    link = _find_header(hdr, "Tech Results Link", "Tech Result Link", "Tech Calibration Link")
    cells, last = [], len(hdr)
    if not res:
        res = C_TECH_RESULTS if free(C_TECH_RESULTS) else last + 1
        cells.append(gspread.Cell(RSA_HEADER_ROW, res, "Tech Results"))
        last = max(last, res)
    if not link:
        link = C_TECH_RESULTS_LINK if (free(C_TECH_RESULTS_LINK) and C_TECH_RESULTS_LINK != res) else last + 1
        cells.append(gspread.Cell(RSA_HEADER_ROW, link, "Tech Results Link"))
    if cells:
        _update(ws, cells)
    _TECH_COLS = (res, link)
    return {"techResultsCol": res, "techResultsLinkCol": link, "created": [c.value for c in cells]}


def _tech_cols():
    """Resolved (results_col, link_col) for the Tech Results columns. Uses the value
    cached by ensure_tech_results_columns() when present; otherwise resolves it READ-ONLY
    from the header row, so gap reports read the right column even when 'Tech Results' was
    appended somewhere other than the V/W defaults (was: blindly used V/W). Falls back to
    the V/W defaults on any error."""
    global _TECH_COLS
    if not _TECH_COLS:
        try:
            vals = _get_all(_ws(RSA_SPREADSHEET_ID, RSA_SHEET))
            hdr = vals[RSA_HEADER_ROW - 1] if len(vals) >= RSA_HEADER_ROW else []
            res = _find_header(hdr, "Tech Results", "Tech Result", "Tech Calibration") or C_TECH_RESULTS
            link = (_find_header(hdr, "Tech Results Link", "Tech Result Link", "Tech Calibration Link")
                    or C_TECH_RESULTS_LINK)
            _TECH_COLS = (res, link)
        except Exception:
            return (C_TECH_RESULTS, C_TECH_RESULTS_LINK)
    return _TECH_COLS


def set_tech_result(cart_id, store=None, readings_text="", link=None, note=None):
    """Write a tech calibration result onto a cart: the per-cart load/shift readings
    summary into the Tech Results column, and the source file link alongside (col W).
    Stamps Last Updated. Does NOT touch RSA/W&M status — calibration precedes the test."""
    try:
        res_col, link_col = _tech_cols()
        ws = _ws(RSA_SPREADSHEET_ID, RSA_SHEET)
        values = _get_all(ws)[RSA_DATA_START - 1:]
        row, data = _find_row(values, cart_id, store)
        if not row:
            return {"error": f'Cart not found: "{cart_id}"', "cartId": cart_id}
        cells, labels = [], []
        if readings_text:
            cells.append(gspread.Cell(row, res_col, readings_text)); labels.append("Tech Results")
        if link:
            cells.append(gspread.Cell(row, link_col, link)); labels.append("Tech Results link")
        if note:
            existing = _cell(data, C_NOTES)
            note_val = f"{existing}\n[{_today()}] {note}" if existing else f"[{_today()}] {note}"
            cells.append(gspread.Cell(row, C_NOTES, note_val)); labels.append("Notes")
        if not labels:
            return {"error": "Nothing to write (no readings or link)."}
        cells.append(gspread.Cell(row, C_LASTUPD, _today()))
        _update(ws, cells)
        return {"cartId": _cell(data, C_CARTID), "store": _cell(data, C_STORE),
                "updated": labels, "trackerUrl": webapp_url(_cell(data, C_CARTID))}
    except Exception as e:
        return {"error": f"set_tech_result: {e}"}


# ── Gap tracking — carts missing tech and/or W&M test results ────────────────────
def carts_missing_results(which="any"):
    """Carts in the RSA Tracker missing tech and/or W&M test results.
    which: 'tech' (no tech result) | 'test' (no W&M result) | 'both' (missing BOTH) |
           'any' (missing EITHER, the default)."""
    res_col, _ = _tech_cols()
    out = []
    for r in _rsa_data():
        cid = _cell(r, C_CARTID)
        if not cid:
            continue
        has_test = bool(_cell(r, C_RSA_RESULTS))
        has_tech = bool(_cell(r, res_col))
        miss_tech, miss_test = not has_tech, not has_test
        include = ((which == "tech" and miss_tech) or (which == "test" and miss_test) or
                   (which == "both" and miss_tech and miss_test) or
                   (which == "any" and (miss_tech or miss_test)))
        if include:
            out.append({"cartId": cid, "store": _cell(r, C_STORE), "state": _cell(r, C_STATE),
                        "rsaStatus": _cell(r, C_RSASTATUS), "hasTech": has_tech, "hasTest": has_test})
    return out


def results_completeness():
    """Counts for the gap report: carts with tech, test, both, or neither result."""
    res_col, _ = _tech_cols()
    rows = [r for r in _rsa_data() if _cell(r, C_CARTID)]
    total = len(rows)
    tech = sum(1 for r in rows if _cell(r, res_col))
    test = sum(1 for r in rows if _cell(r, C_RSA_RESULTS))
    both = sum(1 for r in rows if _cell(r, res_col) and _cell(r, C_RSA_RESULTS))
    neither = sum(1 for r in rows if not _cell(r, res_col) and not _cell(r, C_RSA_RESULTS))
    return {"total": total, "tech": tech, "test": test, "both": both, "neither": neither,
            "missingTech": total - tech, "missingTest": total - test, "trackerUrl": webapp_url()}


def is_nj(state):
    """True when a store's state is New Jersey. NJ is the only state where the
    agent asks TRUNO vs Winter Scales instead of auto-assigning TRUNO."""
    return str(state or "").strip().upper() in ("NJ", "NEW JERSEY")


def _norm_vendor(v):
    """Map free-text vendor input to a canonical tracker value
    (TRUNO / Winter Scales / DUMAC / TBD). Returns None when nothing was given."""
    if not v:
        return None
    s = str(v).strip().lower()
    if "winter" in s or s in ("ws", "wscale", "wscales"):
        return WINTERSCALES_VENDOR
    if "truno" in s:
        return "TRUNO"
    if "dumac" in s:
        return "DUMAC"
    if s == "tbd":
        return "TBD"
    return str(v).strip()


def _resolve_store_address(store, address_hint=None):
    """Best street address for a store — the Caper deployment data first, then any
    address the agent already collected. Returns '' when nothing reliable is found, so
    callers BLOCK (no Truno row / no vendor email) and ask rather than guessing.
    store_map imports rsa_sheets, so the import is lazy to avoid a circular import."""
    try:
        import store_map
        addr = store_map.store_address(store, address_hint)
    except Exception:
        addr = ""
    return (addr or str(address_hint or "")).strip()


def _email_winterscales(cart_label, store, state="NJ", trigger="", notes="", submittedBy="RSA Agent",
                        address=None):
    """Hand a cart off to Winter Scales (the NJ alternative to TRUNO) by emailing
    the Winter Scales contacts. There is no Winter Scales tracker sheet, so the
    email IS the request and ops follows up manually. Python has no mail transport,
    so the send goes through the GAS web app (which owns Gmail). Degrades
    gracefully: if the webhook isn't configured/reachable, the cart is still
    recorded and the reply tells ops to contact Winter Scales themselves."""
    recipients = list(WINTERSCALES_EMAILS)
    addr = _resolve_store_address(store, address)
    base = {"vendor": WINTERSCALES_VENDOR, "contactName": "Winter Scales",
            "emailTo": recipients, "address": addr}
    if not addr:
        # Block: don't email a scheduling request without the store's address.
        return {**base, "emailSent": False, "emailSkipped": True, "blocked": True,
                "needAddress": True,
                "reason": (f'Couldn\'t resolve the address for "{store}" — Winter Scales email '
                           "not sent. Reply with the store's street address to schedule.")}
    if not GAS_WEB_APP_URL or not recipients:
        return {**base, "emailSent": False, "emailSkipped": True,
                "reason": "Winter Scales email not configured — contact them manually."}
    payload = {
        "secret": AGENT_SECRET, "action": "scheduleWinterScales",
        "cartId": str(cart_label), "store": store, "state": state or "NJ",
        "address": addr,
        "trigger": trigger or "", "notes": notes or "",
        "submittedBy": submittedBy or "RSA Agent", "emailTo": recipients,
    }
    try:
        resp = requests.post(GAS_WEB_APP_URL, json=payload, timeout=20)
        data = resp.json() if hasattr(resp, "json") else {}
    except Exception as e:
        return {**base, "emailSent": False, "emailSkipped": True,
                "reason": f"Winter Scales email failed to send ({e}) — contact them manually."}
    if isinstance(data, dict) and data.get("error"):
        return {**base, "emailSent": False, "emailSkipped": True,
                "reason": f"{data['error']} — contact Winter Scales manually."}
    sent_to = data.get("emailTo") if isinstance(data, dict) and data.get("emailTo") else recipients
    return {**base, "emailSent": True, "emailTo": sent_to}


# ── Explicit RSA ↔ Truno map (written when the agent CREATES a Truno row) ─────────
# The reliable way to mirror a Truno visit back to the right RSA cart is to record the
# link at the moment we add the Truno row — no inference needed later. Keyed by the
# address (the same key the Truno sheet joins on), value maps each cart NUMBER to its RSA
# serial. The Truno monitor consults this FIRST, and only falls back to number/fuzzy
# matching for legacy rows the agent didn't create.
TRUNO_MAP_FILE = os.environ.get("TRUNO_MAP_FILE", "truno_map.json")


def _load_truno_map():
    try:
        with open(TRUNO_MAP_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_truno_map(data):
    try:
        tmp = TRUNO_MAP_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, TRUNO_MAP_FILE)
    except Exception as e:
        log.warning("could not save %s: %s", TRUNO_MAP_FILE, e)


def record_truno_map(address, store, cart_serials):
    """Persist the RSA↔Truno link for a just-created Truno row: address-key → {store,
    address, carts:{cartNum: serial}}. cart_serials is {cartNumber: rsa_serial}."""
    if not address or not cart_serials:
        return
    try:
        import store_map
        key = store_map.addr_key(address)
    except Exception:
        key = re.sub(r"[^a-z0-9]", "", str(address).lower())
    if not key:
        return
    data = _load_truno_map()
    entry = data.get(key) or {"store": store, "address": address, "carts": {}}
    entry["store"] = store or entry.get("store")
    entry["address"] = address or entry.get("address")
    entry.setdefault("carts", {})
    for cn, serial in cart_serials.items():
        if cn and serial:
            entry["carts"][str(cn).strip().lstrip("0") or str(cn).strip()] = serial
    entry["updated"] = _today()
    data[key] = entry
    _save_truno_map(data)


def _schedule_truno(cartId, store, visitDate=None, notes="", submittedBy="RSA Agent", address=None,
                    rsa_serials=None):
    """Create a TRUNO inspection REQUEST (status 'Pending'). We do NOT set a visit
    date — Nicole/Truno picks it and flips the row to 'Scheduled', at which point the
    GAS Truno monitor writes 'Scheduled RSA' + that date back to the RSA Tracker.

    The store's Address (col E) is resolved and written so the row is complete and
    joins cleanly to the Caper sheet. If no address can be resolved we BLOCK — nothing
    is written — and ask, rather than leaving Nicole a row she can't act on."""
    addr = _resolve_store_address(store, address)
    if not addr:
        return {"blocked": True, "needAddress": True, "store": store, "trunoUrl": TRUNO_URL,
                "emailSent": False, "emailSkipped": True,
                "error": (f'Couldn\'t resolve the address for "{store}" — no Truno row created. '
                          "Reply with the store's street address and I'll schedule it.")}
    ws = _ws(TRUNO_SPREADSHEET_ID, TRUNO_SHEET)
    visit = visitDate or ""                     # no fabricated date — Nicole schedules it
    trace = f"[RSA Agent · {submittedBy} · {_today()}]"
    note_out = f"{notes}  {trace}" if notes else trace
    r = _next_row(ws, T_STORE, 2)
    cells = [gspread.Cell(r, T_STORE, store), gspread.Cell(r, T_CART, cartId),
             gspread.Cell(r, T_ADDRESS, addr), gspread.Cell(r, T_VISIT, visit),
             gspread.Cell(r, T_NOTES, note_out),
             gspread.Cell(r, T_STATUS, "Pending")]      # awaiting Truno to schedule
    _update(ws, cells)
    # Record the explicit RSA↔Truno link now, so the writeback is a direct lookup later.
    try:
        record_truno_map(addr, store, rsa_serials or {})
    except Exception as e:
        log.warning("record_truno_map failed: %s", e)
    return {"visitDate": visit or None, "trunoRow": r, "trunoUrl": TRUNO_URL, "status": "Pending",
            "address": addr, "emailSent": False, "emailSkipped": True, "emailTo": NICOLE_EMAIL,
            "rsaSerials": list((rsa_serials or {}).values())}


def onboard_cart(cartId, store, state="NJ", trigger="", rsaStatus=None, wmStatus=None,
                 overallStatus=None, rsaVendor="TRUNO", dri=None, notes=None,
                 visitDate=None, submittedBy="Ops Manager", address=None, **_):
    # NJ rule: never silently force TRUNO. The vendor is whatever the caller
    # resolved (TRUNO by default for non-NJ; for NJ the agent asks first).
    vendor = _norm_vendor(rsaVendor) or "TRUNO"
    is_ws = vendor == WINTERSCALES_VENDOR
    result = {"cartId": cartId, "store": store, "trackerUrl": webapp_url(cartId), "vendor": vendor, "steps": {}}
    # Winter Scales carts are not auto-scheduled (no TRUNO request), so their RSA
    # status is the generic 'Pending RSA' rather than 'Pending Truno Scheduled'.
    new_status = rsaStatus or ("Pending RSA" if is_ws else RSA_STATUS_NEW)
    add_note = notes
    if is_ws:
        contacts = ", ".join(WINTERSCALES_EMAILS) or "Winter Scales"
        add_note = ((notes + " | ") if notes else "") + (
            f"NJ store — Winter Scales chosen (not TRUNO). Emailed {contacts} to schedule; "
            "ops to follow up with Winter Scales manually.")
    # Step 1 — RSA Tracker (add or update)
    try:
        existing, _data = _find_row(_rsa_data(), cartId, store)
        if existing:
            # On a re-onboard, set vendor + note; don't regress an existing status
            # unless the caller passed one explicitly.
            upd = update_cart(cartId, storeHint=store, rsaStatus=rsaStatus, rsaVendor=vendor,
                              wmStatus=wmStatus, overallStatus=overallStatus, dri=dri, notes=add_note)
            result["steps"]["step1_rsaTracker"] = {"action": "updated", **upd}
        else:
            add = add_cart(cartId, store, state=state, trigger=trigger,
                           rsaStatus=new_status, rsaVendor=vendor,
                           wmStatus=wmStatus or "Pending W&M", overallStatus=overallStatus or "Pending RSA",
                           dri=dri or "HW Ops", notes=add_note or "Added via RSA Agent bot")
            result["steps"]["step1_rsaTracker"] = add
    except Exception as e:
        result["steps"]["step1_rsaTracker"] = {"error": str(e)}
    # Step 2 — vendor hand-off: Winter Scales email, or the usual TRUNO request.
    if is_ws:
        try:
            result["steps"]["step2_winterScales"] = _email_winterscales(
                cartId, store, state=state, trigger=trigger, notes=notes or "",
                submittedBy=submittedBy, address=address)
        except Exception as e:
            result["steps"]["step2_winterScales"] = {"error": str(e)}
    else:
        # cartId is the RSA serial here; map its cart NUMBER → serial for the Truno row.
        _cm = re.search(r"_M3_0*(\d+[A-Za-z]?)", str(cartId))
        cart_serials = {(_cm.group(1).lstrip("0") or _cm.group(1)): cartId} if _cm else {}
        try:
            result["steps"]["step2_trunoSchedule"] = _schedule_truno(
                cartId, store, visitDate=visitDate, notes=notes or "", submittedBy=submittedBy,
                address=address, rsa_serials=cart_serials)
            result["visitDate"] = result["steps"]["step2_trunoSchedule"].get("visitDate")
        except Exception as e:
            result["steps"]["step2_trunoSchedule"] = {"error": str(e)}
    # Compliance directive
    try:
        result["compliance"] = lookup_compliance(store, state, trigger)
    except Exception as e:
        result["compliance"] = {"error": str(e)}
    return result


# ── Multi-cart onboarding (users give cart NUMBERS, not full serials) ───────────
def _store_nums(s):
    cleaned = re.sub(r"isc0*50", " ", str(s or "").lower())
    out = set()
    for g in re.findall(r"\d{2,}", cleaned):
        out.add(g)
        out.add(g.lstrip("0") or g)
    return out


def _serial_prefix(cart_id):
    m = re.match(r"(.*_M3_)\d+[A-Za-z]?$", str(cart_id))
    return m.group(1) if m else None


def derive_cart_id(store, cart_number, rows):
    """Build a full serial for store + cart number. Reuse the store's existing
    serial prefix when it's already in the tracker, else generate a consistent one."""
    num, letter = _cart_num_letter(cart_number)   # order-agnostic: 'A1' ≡ '1A'
    if not num:
        return None
    digits, letter = num, letter.upper()
    target = _store_nums(store)
    prefixes, pad = {}, 3
    for r in rows:
        cid = _cell(r, C_CARTID)
        if not cid:
            continue
        snums = set(_store_nums(_cell(r, C_STORE)))
        sm = re.search(r"_0*(\d+)_M3_", cid)
        if sm:
            snums.add(sm.group(1)); snums.add(sm.group(1).lstrip("0") or sm.group(1))
        if target and (snums & target):
            pre = _serial_prefix(cid)
            if pre:
                prefixes[pre] = prefixes.get(pre, 0) + 1
                cm = re.search(r"_M3_(\d+)", cid)
                if cm:
                    pad = max(pad, len(cm.group(1)))
    cart_str = digits.zfill(pad) + letter
    if prefixes:
        return max(prefixes, key=prefixes.get) + cart_str
    token = re.sub(r"[^A-Z0-9]+", "_", str(store or "").upper()).strip("_") or "STORE"
    return f"P_{token}_M3_{cart_str}"


def _cart_num_letter(token):
    """Split a cart label into (number, letter) regardless of order, so a form's 'A1'
    matches the tracker's '1A' (serial ..._M3_001A). '3A'/'A3'→('3','a'); '12'→('12','');
    'A'→(None,''). Returns (num_str_no_leading_zeros | None, letter_lowercase)."""
    s = str(token or "").strip()
    nm = re.search(r"\d+", s)
    lm = re.search(r"[A-Za-z]", s)
    if not nm:
        return None, ""
    return (nm.group(0).lstrip("0") or nm.group(0)), (lm.group(0).lower() if lm else "")


def match_cart(store, cart_number, rows):
    """Find the EXISTING serial for a store + cart number using the
    <store#>_M3_<cart#>[letter] pattern already in the tracker (the established
    nomenclature). The cart label is parsed order-agnostically, so a form's 'A1' matches
    a tracked '1A'. Returns the real serial, or None if the cart isn't tracked. The store
    is bridged to its RSA name via the alias file (store_map.resolve, applied by the
    caller) — see store_map.json.

    Letter-suffix tolerance: when the form gives a bare number (e.g. '13') and the tracker
    holds '13A', the cart is still matched — the letter is considered optional. When a
    letter IS present on the form, the exact-letter match is preferred but any same-number
    cart is returned if there's no exact hit.

    Non-_M3_ fallback: if a cart ID was entered without the standard prefix pattern
    (e.g. manually as '13A'), the trailing number+letter is extracted directly from the ID
    so those carts are still reachable."""
    want, want_let = _cart_num_letter(cart_number)
    if not want:
        return None
    target_nums = _store_nums(store)
    name = _norm(store)
    hits = []                            # (serial, serial_letter) for the store's cart #
    for r in rows:
        cid = _cell(r, C_CARTID)
        if not cid:
            continue
        cm = re.search(r"_M3_0*(\d+)([A-Za-z]?)", cid)
        if cm:
            cart_num = cm.group(1).lstrip("0") or cm.group(1)
            cart_let = cm.group(2).lower()
        else:
            # Fallback for IDs without the _M3_ pattern (manually entered, legacy, or
            # bare labels like "13A"). Extract trailing number[letter] from the ID.
            cm_raw = re.search(r"0*(\d+)([A-Za-z]?)$", cid.strip())
            if not cm_raw:
                continue
            cart_num = cm_raw.group(1).lstrip("0") or cm_raw.group(1)
            cart_let = cm_raw.group(2).lower()
        if cart_num != want:
            continue
        rstore = _norm(_cell(r, C_STORE))
        snums = set(_store_nums(_cell(r, C_STORE)))
        if cm:
            sm = re.search(r"_0*(\d+)_M3_", cid)
            if sm:
                snums.add(sm.group(1)); snums.add(sm.group(1).lstrip("0") or sm.group(1))
        if (target_nums and (snums & target_nums)) or (len(name) >= 4 and name in rstore):
            hits.append((cid, cart_let))
    if not hits:
        return None
    if want_let:                         # form gave a letter — prefer exact suffix match
        exact = [cid for cid, lt in hits if lt == want_let]
        if exact:
            return exact[0]
    else:                               # no letter given — prefer letterless cart, then any letter
        no_letter = [cid for cid, lt in hits if not lt]
        if no_letter:
            return no_letter[0]
    return hits[0][0]


def match_cart_strict(store, cart_number, rows):
    """Strict matcher for the tech-results path. Returns a serial ONLY when the store
    number AND the cart number together identify exactly one tracked cart.

    Unlike match_cart() this never falls back to a loose store-name substring or a bare
    numeric coincidence: the store must carry a number (e.g. 'WF-62' → 62) that matches
    the cart's store, the cart number must match, and the (store#, cart#) pair must be
    unique. If the store has no number, nothing matches, or the pair is ambiguous, it
    returns None — so a tech file is never attached to the wrong cart."""
    want, want_let = _cart_num_letter(cart_number)   # order-agnostic: 'A1' ≡ '1A'
    if not want:
        return None
    target_nums = _store_nums(store)
    if not target_nums:                      # no store number → can't confirm the store
        return None
    hits = []                                # (serial, serial_letter)
    for r in rows:
        cid = _cell(r, C_CARTID)
        if not cid:
            continue
        cm = re.search(r"_M3_0*(\d+)([A-Za-z]?)", cid)
        if not cm or (cm.group(1).lstrip("0") or cm.group(1)) != want:
            continue                         # cart number must match
        snums = set(_store_nums(_cell(r, C_STORE)))
        sm = re.search(r"_0*(\d+)_M3_", cid)
        if sm:
            snums.add(sm.group(1)); snums.add(sm.group(1).lstrip("0") or sm.group(1))
        if snums & target_nums:              # store number must match
            hits.append((cid, cm.group(2).lower()))
    if want_let:                             # the letter must match too (1A vs 1B vs 1)
        letter_uniq = sorted({cid for cid, lt in hits if lt == want_let})
        if len(letter_uniq) == 1:
            return letter_uniq[0]
    uniq = sorted({cid for cid, _ in hits})
    return uniq[0] if len(uniq) == 1 else None   # unique store#+cart# only


def onboard_carts(cart_numbers, store, state="NJ", trigger="", notes=None,
                  submittedBy="Ops Manager", visitDate=None, address=None, rsaVendor="TRUNO", **_):
    """Add one row per cart number for a store, hand off to the chosen vendor, and
    attach the store's pre/post-launch compliance directive. Default vendor TRUNO
    (one shared TRUNO visit); for NJ stores the agent asks first, and Winter Scales
    skips TRUNO scheduling in favour of an email hand-off to the Winter Scales team."""
    vendor = _norm_vendor(rsaVendor) or "TRUNO"
    is_ws = vendor == WINTERSCALES_VENDOR
    new_status = ("Pending RSA" if is_ws else RSA_STATUS_NEW)
    contacts = ", ".join(WINTERSCALES_EMAILS) or "Winter Scales"
    add_note = notes
    if is_ws:
        add_note = ((notes + " | ") if notes else "") + (
            f"NJ store — Winter Scales chosen (not TRUNO). Emailed {contacts} to schedule; "
            "ops to follow up with Winter Scales manually.")
    result = {"store": store, "state": state, "trigger": trigger, "vendor": vendor, "carts": []}
    if not store:
        return {"error": "Which store? Please include the store name/location."}
    if not cart_numbers:
        return {"error": "Which cart number(s)? e.g. 'onboard carts 1, 7 at ShopRite 113 NJ'."}
    try:
        rows = _rsa_data()
    except Exception as e:
        return {"error": f"onboard_carts read failed: {e}"}
    for cn in cart_numbers:
        cid = derive_cart_id(store, cn, rows)
        if not cid:
            result["carts"].append({"cartNum": cn, "error": "could not read cart number"})
            continue
        try:
            existing, _d = _find_row(rows, cid, store)
            if existing:
                update_cart(cid, storeHint=store, rsaVendor=(vendor if is_ws else None), notes=add_note)
                result["carts"].append({"cartNum": cn, "cartId": cid, "action": "updated"})
            else:
                add = add_cart(cid, store, state=state, trigger=trigger, rsaVendor=vendor,
                               rsaStatus=new_status, notes=add_note or "Added via RSA Agent bot")
                if add.get("error"):
                    result["carts"].append({"cartNum": cn, "cartId": cid, "error": add["error"]})
                else:
                    result["carts"].append({"cartNum": cn, "cartId": cid, "action": "added"})
                    rows = _rsa_data()        # refresh so the next cart appends below
        except Exception as e:
            result["carts"].append({"cartNum": cn, "cartId": cid, "error": str(e)})
    nums = ", ".join(str(c) for c in cart_numbers)
    if is_ws:
        # Winter Scales: no TRUNO request — email the Winter Scales team instead.
        try:
            result["winterScales"] = _email_winterscales(
                nums, store, state=state, trigger=trigger, notes=notes or "",
                submittedBy=submittedBy, address=address)
        except Exception as e:
            result["winterScales"] = {"error": str(e)}
    else:
        # Map each cart number → its RSA serial so the Truno row carries an explicit link.
        cart_serials = {str(c.get("cartNum")): c.get("cartId")
                        for c in result["carts"] if c.get("cartId")}
        try:
            result["truno"] = _schedule_truno(nums, store, visitDate=visitDate,
                                              notes=notes or "", submittedBy=submittedBy,
                                              address=address, rsa_serials=cart_serials)
            result["visitDate"] = result["truno"].get("visitDate")
        except Exception as e:
            result["truno"] = {"error": str(e)}
    try:
        result["compliance"] = lookup_compliance(store, state, trigger, address=address)
    except Exception as e:
        result["compliance"] = {"error": str(e)}
    return result
