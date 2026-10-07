#!/usr/bin/env python3
"""
Tech calibration parser — reads the technician's calibration record for a scheduled
cart and writes a compact readings summary onto the matching RSA Tracker cart(s) under
the "Tech Results" column. Linking is by store + cart number (same matcher as the W&M
test-results path in test_forms.py).

Two source formats are supported:

  1. The "Weight check checklist" SPREADSHEET (.xlsx / .csv) — the primary format. One
     row per cart with the increasing-load readings (5/25/80 lbs), the four shift-test
     corners, init values, and the calibration Y/N flags. Parsed directly (openpyxl/csv)
     — no vision, so it's exact.
  2. A PDF / photo of a TRUNO calibration sheet — parsed via the AI gateway's vision as
     a fallback (some techs photograph a paper form).

Used by the Slack-upload path (a tech drops the file) and the tech-results Drive folder
monitor — both call process_upload().
"""

import io
import os
import re
import csv
import json
import logging

from openai import OpenAI, APIConnectionError   # APITimeoutError subclasses APIConnectionError

import rsa_sheets as sheets
# Reuse the (already-tested) PDF/image → PNG → shrink → data-URL pipeline for the
# vision fallback path.
from test_forms import _to_pngs, _shrink, _b64_data_url

log = logging.getLogger("rsa-agent.tech")

AIGATEWAY_BASE_URL = os.environ.get(
    "AIGATEWAY_BASE_URL", "https://aigateway.instacart.tools/proxy/rovi_agent/openai/v1")
AIGATEWAY_API_KEY = os.environ.get("AIGATEWAY_API_KEY", "gateway-no-token-needed")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-4-8")
# Vision parses are slower than a chat turn, so allow more time; still bounded + retried
# so a flaky gateway can't hang the tech-form poller indefinitely.
AIGATEWAY_TIMEOUT     = float(os.environ.get("AIGATEWAY_VISION_TIMEOUT", "90"))
AIGATEWAY_MAX_RETRIES = int(os.environ.get("AIGATEWAY_MAX_RETRIES", "3"))

_llm = OpenAI(base_url=AIGATEWAY_BASE_URL, api_key=AIGATEWAY_API_KEY,
              timeout=AIGATEWAY_TIMEOUT, max_retries=AIGATEWAY_MAX_RETRIES)

SHEET_EXTS = (".xlsx", ".xlsm", ".xls", ".csv", ".tsv")

_POS = {"top left": "TL", "topleft": "TL", "top right": "TR", "topright": "TR",
        "bottom left": "BL", "bottomleft": "BL", "bottom right": "BR", "bottomright": "BR",
        "center": "C", "centre": "C"}


# ── small value helpers ─────────────────────────────────────────────────────────
def _v(x):
    s = str(x if x is not None else "").strip()
    return s if s else "n/a"


def _pos(p):
    s = str(p or "").strip().lower()
    return _POS.get(s, (p or "").strip() or "?")


def _cs(row, ci):
    """Stripped string for a grid cell (col index, may be None / out of range)."""
    if ci is None or ci >= len(row):
        return ""
    v = row[ci]
    return "" if v is None else str(v).strip()


def _craw(row, ci):
    if ci is None or ci >= len(row):
        return None
    return row[ci]


def _fmt_date(v):
    try:
        return v.strftime("%Y-%m-%d")           # datetime from openpyxl
    except Exception:
        s = str(v if v is not None else "").strip()
        return s.split(" ")[0] if s else ""     # trim a trailing 00:00:00


def _strip_lbs(label):
    """'25 lbs' -> '25', 'Bottom Left 25 lbs' -> '25'."""
    m = re.search(r"([\d.]+)\s*lbs", str(label).lower())
    return m.group(1) if m else (str(label or "").strip())


def _shift_pos(label):
    """'Bottom Left 25 lbs' -> 'Bottom Left'; bare '25 lbs' -> 'Center'."""
    s = re.sub(r"[\d.]+\s*lbs", "", str(label or ""), flags=re.I).strip(" -")
    return s or "Center"


# ── readings summary (stored in the Tech Results column) ────────────────────────
def summarize_readings(cart):
    """Compact per-cart readings string. Handles both the checklist format (load/shift
    + calibration flags) and the vision form (passed/comments)."""
    parts = []
    loads = cart.get("loadTests") or []
    if loads:
        parts.append("Load: " + ", ".join(f"{_v(t.get('expected'))}->{_v(t.get('actual'))}" for t in loads))
    shifts = cart.get("shiftTests") or []
    if shifts:
        parts.append("Shift: " + ", ".join(f"{_pos(t.get('position'))} {_v(t.get('actual'))}" for t in shifts))
    if str(cart.get("calCompleted") or "").strip():
        parts.append(f"Cal done: {cart['calCompleted']}")
    if str(cart.get("calRequired") or "").strip():
        parts.append(f"Cal req: {cart['calRequired']}")
    rt = str(cart.get("retorque") or "").strip()
    if rt and rt.upper() != "NA":
        parts.append(f"Retorque: {rt}")
    if str(cart.get("initBefore") or "").strip():
        parts.append(f"Init before: {cart['initBefore']}")
    if str(cart.get("status") or "").strip():
        parts.append(f"Status: {cart['status']}")
    p = cart.get("passed")
    if p is True:
        parts.append("Passed: Yes")
    elif p is False:
        parts.append("Passed: No")
    c = (cart.get("comments") or "").strip()
    if c:
        parts.append(c)
    return " · ".join(parts) if parts else "Calibration recorded (no readings parsed)"


# ── spreadsheet checklist parser (.xlsx / .csv) ─────────────────────────────────
def _norm_h(s):
    return re.sub(r"\s+", " ", str(s if s is not None else "").strip().lower())


def _grid_from_xlsx(file_bytes):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    # Prefer the sheet that actually has a "Cart No" header; else the first sheet.
    best = None
    for ws in wb.worksheets:
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        if any(_norm_h(c) in ("cart no", "cart no.", "cart number", "cart #", "cart#") for row in grid for c in row):
            return grid
        best = best or grid
    return best or []


def _grid_from_csv(file_bytes):
    text = file_bytes.decode("utf-8-sig", errors="replace")
    return [row for row in csv.reader(io.StringIO(text))]


def _find_header_row(grid):
    for i, row in enumerate(grid):
        if any(_norm_h(c) in ("cart no", "cart no.", "cart number", "cart #", "cart#") for c in row):
            return i
    return None


def _col_index(header, *keywords):
    for ci, v in enumerate(header):
        h = _norm_h(v)
        if h and any(k in h for k in keywords):
            return ci
    return None


def parse_checklist(grid, filename=""):
    """Parse a Weight-check checklist grid into the same shape apply_tech_form expects."""
    hr = _find_header_row(grid)
    if hr is None:
        return {"error": "not a recognized weight-check checklist (no 'Cart No' column found)"}
    header = list(grid[hr])
    sub = list(grid[hr + 1]) if hr + 1 < len(grid) else []
    ncol = max(len(header), len(sub))

    col_date   = _col_index(header, "test date")
    col_ret    = _col_index(header, "retailer")
    col_cart   = _col_index(header, "cart no", "cart number", "cart #", "cart#")
    col_initb  = _col_index(header, "initialization value before", "init value before", "before calibration")
    col_latest = _col_index(header, "latest calibration date")
    col_load   = _col_index(header, "increasing load", "load test")
    col_shift  = _col_index(header, "shift test")
    col_calreq = _col_index(header, "calibration required")
    col_inita  = _col_index(header, "initialization value after", "init value after", "after calibration")
    col_retorq = _col_index(header, "retorqu")
    col_caldone = _col_index(header, "calibration completed")
    col_status = _col_index(header, "status")

    load_cols = list(range(col_load, col_shift)) if (col_load is not None and col_shift is not None) else []
    shift_end = next((c for c in (col_calreq, col_inita, col_retorq, col_caldone, col_status) if c is not None), ncol)
    shift_cols = list(range(col_shift, shift_end)) if col_shift is not None else []

    def sublabel(ci):
        return _cs(sub, ci)

    carts, store_seen = [], None
    for row in grid[hr + 2:]:
        cart = _cs(row, col_cart)
        if not cart or _norm_h(cart) in ("cart no", "cart number"):
            continue
        store = _cs(row, col_ret) or store_seen or ""
        store_seen = store or store_seen
        loads = [{"expected": _strip_lbs(sublabel(c)), "actual": _cs(row, c)} for c in load_cols if _cs(row, c) != ""]
        shifts = [{"position": _shift_pos(sublabel(c)), "expected": _strip_lbs(sublabel(c)),
                   "actual": _cs(row, c)} for c in shift_cols if _cs(row, c) != ""]
        carts.append({
            "cart": cart, "loadTests": loads, "shiftTests": shifts,
            "initBefore": _cs(row, col_initb), "initAfter": _cs(row, col_inita),
            "latestCalDate": _fmt_date(_craw(row, col_latest)),
            "calRequired": _cs(row, col_calreq), "retorque": _cs(row, col_retorq),
            "calCompleted": _cs(row, col_caldone), "status": _cs(row, col_status),
            "serviceDate": _fmt_date(_craw(row, col_date)),
        })

    store = store_seen or _store_from_filename(filename)
    service = carts[0]["serviceDate"] if carts else ""
    if not carts:
        return {"error": "checklist had no cart rows", "store": store}
    return {"store": store, "retailer": store, "serviceDate": service,
            "technician": "", "carts": carts, "format": "checklist"}


def _store_from_filename(filename):
    """Fallback store id from a name like 'WF62_ Weight check checklist_...xlsx'."""
    base = re.split(r"[_\-.]", str(filename or ""))[0].strip()
    return base or (filename or "")


# ── vision fallback for PDF / image calibration sheets ──────────────────────────
_VISION_PROMPT = (
    "You read TRUNO scale calibration sheets that a technician fills out when calibrating "
    "a self-checkout cart's scale. Extract the store and, for EACH cart, the calibration "
    "READINGS. Return ONLY JSON, no prose:\n"
    '{"retailer": str, "store": str, "serviceDate": str, "technician": str, '
    '"carts": [{"cart": str, "serial": str, '
    '"loadTests": [{"expected": str, "actual": str}], '
    '"shiftTests": [{"position": str, "expected": str, "actual": str}], '
    '"passed": true|false|null, "comments": str}]}\n'
    "loadTests = the increasing load readings (e.g. 5, 25, 80 lbs): expected vs actual. "
    "shiftTests = the corner readings Top Left / Top Right / Bottom Left / Bottom Right. "
    "If a value is blank or 'n/a', use 'n/a'. 'store' = retailer + store number. List every cart."
)


def _extract_via_vision(file_bytes, filename):
    try:
        pages = _to_pngs(file_bytes, filename)
    except ImportError:
        return {"error": "PDF support isn't installed — run: pip install pymupdf"}
    except Exception as e:
        return {"error": f"could not read the file ({type(e).__name__}): {e}"}
    if not pages:
        return {"error": "could not render any pages from the file"}
    content = [{"type": "text", "text": _VISION_PROMPT}]
    for raw in pages:
        content.append({"type": "image_url", "image_url": {"url": _b64_data_url(_shrink(raw))}})
    try:
        resp = _llm.chat.completions.create(
            model=AGENT_MODEL, max_tokens=2000,
            messages=[{"role": "user", "content": content}])
        text = (resp.choices[0].message.content or "{}").strip()
        text = re.sub(r"^```json\s*", "", text, flags=re.I)
        text = re.sub(r"^```\s*", "", text)
        text = re.sub(r"```$", "", text).strip()
        return json.loads(text)
    except APIConnectionError as e:
        log.warning("vision gateway unreachable (%s) after retries", type(e).__name__)
        return {"error": "couldn't reach the assistant backend to read the checklist "
                         "(network/gateway). It'll retry on the next pass — or re-upload."}
    except Exception as e:
        return {"error": f"vision parse failed ({type(e).__name__}): {e}"}


# ── unified extract (routes by file type) ───────────────────────────────────────
def extract_tech_form(file_bytes, filename):
    name = (filename or "").lower()
    try:
        if name.endswith((".xlsx", ".xlsm", ".xls")):
            return parse_checklist(_grid_from_xlsx(file_bytes), filename)
        if name.endswith((".csv", ".tsv")):
            return parse_checklist(_grid_from_csv(file_bytes), filename)
    except ImportError:
        return {"error": "spreadsheet support isn't installed — run: pip install openpyxl"}
    except Exception as e:
        return {"error": f"could not read the checklist ({type(e).__name__}): {e}"}
    return _extract_via_vision(file_bytes, filename)


# ── apply parsed readings to the RSA Tracker ────────────────────────────────────
def apply_tech_form(parsed, link=None):
    if parsed.get("error"):
        return {"error": parsed["error"]}
    store = parsed.get("store") or parsed.get("retailer") or ""
    carts = parsed.get("carts") or []
    if not store or not carts:
        return {"error": "form parsed but no store / carts found", "parsed": parsed}
    try:
        rows = sheets._rsa_data()
    except Exception as e:
        return {"error": f"tracker read failed: {e}"}
    results = []
    for c in carts:
        num = str(c.get("cart") or c.get("serial") or "").strip()
        if not num:
            continue
        readings = summarize_readings(c)
        # Strict: only attach the file when the store number AND cart number uniquely
        # identify one tracked cart — never link a tech file to the wrong/ambiguous cart.
        cid = sheets.match_cart_strict(store, num, rows)
        if not cid:
            results.append({"cart": num, "cartId": None, "ok": False,
                            "detail": "no confident store + cart match — not written", "readings": readings})
            continue
        res = sheets.set_tech_result(cid, store=store, readings_text=readings, link=link)
        results.append({"cart": num, "cartId": cid, "ok": not res.get("error"),
                        "detail": res.get("error") or ", ".join(res.get("updated", [])),
                        "readings": readings})
    return {"store": store, "serviceDate": parsed.get("serviceDate"),
            "technician": parsed.get("technician"), "format": parsed.get("format", "vision"),
            "results": results}


def process_upload(file_bytes, filename, link=None):
    """Full path for an uploaded calibration record: parse (checklist or vision), write."""
    return apply_tech_form(extract_tech_form(file_bytes, filename), link=link)
