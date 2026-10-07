#!/usr/bin/env python3
"""
RSA Agent — Tech-results Drive folder monitor.

The tech-results sibling of drive_monitor.py. Where drive_monitor watches the W&M
Scale-Test-Form folder (Truno-gated, pass/fail → col U), THIS watches the tech
calibration folder and writes each cart's load/shift readings into the "Tech Results"
column. Linking is by store + cart number, exactly like the test path.

Key differences from the test monitor:
  • No Truno gate. Every cart should eventually have a tech result, so the trigger is
    simply "the cart exists in the tracker and has no tech result yet."
  • Self-healing: each pass (re)applies a file when a cart whose store appears in the
    filename still has no Tech Results — so a calibration sheet that landed before the
    cart was added, or was missed, gets picked up later. New files are always processed.
  • --backfill re-applies EVERY file in the folder (ignores the processed cache).
  • A periodic "results completeness" gap report flags carts missing tech and/or test
    results (all carts should ideally have BOTH).

Prereqs: share the tech-results folder with the service-account email (Viewer) and
enable the Drive API. Started in the background by rsa_agent on launch; also runnable:
    python3 tech_monitor.py             # one self-healing pass
    python3 tech_monitor.py --backfill  # re-apply every file now
    python3 tech_monitor.py --gaps      # print the results-completeness gap report
"""

import os
import re
import json
import logging
import threading
import time

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import rsa_sheets as sheets
import tech_forms

log = logging.getLogger("rsa-agent.tech-drive")

SA_KEY_FILE          = os.environ.get("GOOGLE_SA_KEY_FILE", "sa-key.json")
TECH_FOLDER_ID       = os.environ.get("TECH_RESULTS_FOLDER_ID", "1g7myq_dnWCJqOqYgVzAapks4aJ7UBrHX")
POLL_SECONDS         = int(os.environ.get("RSA_TECH_POLL_SECONDS", "300"))
PROCESSED_FILE       = os.environ.get("RSA_TECH_PROCESSED_FILE", "processed_tech.json")
GAP_REPORT_SECONDS   = int(os.environ.get("RSA_GAP_REPORT_SECONDS", "86400"))  # daily; 0 disables
DRIVE_SCOPES         = ["https://www.googleapis.com/auth/drive.readonly"]

_drive = None


def _service():
    global _drive
    if _drive is None:
        creds = Credentials.from_service_account_file(SA_KEY_FILE, scopes=DRIVE_SCOPES)
        _drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive


def _reset_service():
    global _drive
    _drive = None            # drop a stale HTTP connection so the next call rebuilds it


def _retry(fn, tries=3, base=1.5):
    """Retry transient network hiccups (broken pipe / connection reset / timeout) with
    backoff and a rebuilt connection; non-transient errors raise immediately."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if not sheets.is_transient_error(e):
                raise
            _reset_service()
            time.sleep(base * (i + 1))
    raise last


def _read_fail(what, e, say):
    """Log transient read failures (they self-heal next pass); only surface real/persistent
    errors to Slack so a momentary Drive blip doesn't flood the channel."""
    if sheets.is_transient_error(e):
        log.warning("transient read error on %s: %s — will retry next pass", what, e)
    else:
        say(f":warning: Could not read {what}: {e}")
    return {"error": str(e), "transient": sheets.is_transient_error(e)}


def _load_processed():
    try:
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_processed(ids):
    try:
        with open(PROCESSED_FILE, "w") as f:
            json.dump(sorted(ids), f)
    except Exception as e:
        log.warning("could not save %s: %s", PROCESSED_FILE, e)


# The tech checklist is a spreadsheet; some techs may photograph a paper form, so we
# also accept PDFs/images (handled by the vision fallback in tech_forms).
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _list_folder_files():
    """All checklist/PDF/image files in the tech-results folder (newest first)."""
    q = (f"'{TECH_FOLDER_ID}' in parents and trashed=false and ("
         "mimeType='application/pdf' or mimeType contains 'image/' or "
         f"mimeType='{XLSX_MIME}' or mimeType='application/vnd.ms-excel' or "
         f"mimeType='{GSHEET_MIME}' or mimeType='text/csv')")
    out, page = [], None
    while True:
        resp = _retry(lambda: _service().files().list(
            q=q, pageToken=page, pageSize=100, spaces="drive",
            orderBy="modifiedTime desc",
            fields="nextPageToken, files(id,name,mimeType,webViewLink,modifiedTime)").execute(num_retries=3))
        out += resp.get("files", [])
        page = resp.get("nextPageToken")
        if not page:
            break
    return out


def _download(f):
    """Return (bytes, forced_ext). Native Google Sheets are EXPORTED as .xlsx; everything
    else (uploaded .xlsx/.csv, PDFs, images) is downloaded as-is."""
    if f.get("mimeType") == GSHEET_MIME:
        data = _retry(lambda: _service().files().export_media(fileId=f["id"], mimeType=XLSX_MIME).execute(num_retries=3))
        return data, ".xlsx"
    return _retry(lambda: _service().files().get_media(fileId=f["id"]).execute(num_retries=3)), None


def _file_store_has_missing_tech(name, rsa_rows, has_tech):
    """Without parsing, decide if a file is worth (re)processing on a self-heal pass:
    do any carts whose store number appears in the filename still lack a tech result?
    Year-like tokens (19xx/20xx) are ignored so a date in the name isn't read as a store#."""
    raw = {n for n in re.findall(r"\d{2,}", name or "") if not re.fullmatch(r"(19|20)\d{2}", n)}
    nums = raw | {n.lstrip("0") or n for n in raw}
    if not nums:
        return True                                  # can't tell from the name → re-check to be safe
    for r in rsa_rows:
        cid = sheets._cell(r, sheets.C_CARTID)
        if not cid:
            continue
        snums = set(sheets._store_nums(sheets._cell(r, sheets.C_STORE)))
        sm = re.search(r"_0*(\d+)_M3_", cid)
        if sm:
            snums |= {sm.group(1), sm.group(1).lstrip("0") or sm.group(1)}
        if (snums & nums) and not has_tech.get(cid):
            return True
    return False


def process_tech_files(notify=None, force=False):
    """For every file in the tech-results folder, parse the calibration readings and
    write them onto the matching RSA cart(s) under Tech Results.

    A file is applied when: it's NEW, OR --backfill/force is set, OR a cart whose store
    appears in its filename still has no Tech Results (self-healing). Re-applying the
    same readings is idempotent, so this is safe to run on every poll and to backfill."""
    def say(msg):
        log.info(msg)
        if notify:
            try:
                notify(msg)
            except Exception:
                pass

    processed = _load_processed()
    try:
        files = _list_folder_files()
    except Exception as e:
        return _read_fail("the tech-results Drive folder", e, say)

    # One RSA read per pass → which carts already carry a tech result (for self-healing).
    try:
        res_col, _ = sheets._tech_cols()
        rsa_vals = sheets._ws(sheets.RSA_SPREADSHEET_ID, sheets.RSA_SHEET).get_all_values()
        rsa_rows = rsa_vals[sheets.RSA_DATA_START - 1:]
    except Exception as e:
        return _read_fail("the RSA Tracker", e, say)
    has_tech = {}
    for r in rsa_rows:
        cid = sheets._cell(r, sheets.C_CARTID)
        if cid:
            has_tech[cid] = bool(sheets._cell(r, res_col))

    done = []
    for f in files:
        if not (force or f["id"] not in processed or _file_store_has_missing_tech(f["name"], rsa_rows, has_tech)):
            continue
        try:
            data, ext = _download(f)
            name = f["name"]
            if ext and not name.lower().endswith(ext):     # exported Google Sheet → route to checklist parser
                name += ext
            summary = tech_forms.process_upload(data, name, link=f.get("webViewLink"))
        except Exception as e:
            summary = {"error": f"{type(e).__name__}: {e}"}
        processed.add(f["id"])
        done.append((f["name"], summary))
        say(_fmt(f["name"], summary))
        for r in (summary.get("results") or []) if isinstance(summary, dict) else []:
            if r.get("ok") and r.get("cartId"):       # don't re-trigger on the next pass
                has_tech[r["cartId"]] = True
        time.sleep(2)                                 # pace Sheets/vision calls to stay under quota

    _save_processed(processed)
    return {"processed": len(done), "results": done}


def _fmt(name, summary):
    if summary.get("error"):
        return f":warning: `{name}`: {summary['error']}"
    res = summary.get("results", [])
    ok = sum(1 for r in res if r.get("ok"))
    miss = [str(r["cart"]) for r in res if not r.get("ok")]
    line = f":wrench: `{name}` — {summary.get('store', '?')}: wrote tech calibration for *{ok}/{len(res)}* cart(s)"
    if summary.get("technician"):
        line += f"  ·  tech {summary['technician']}"
    if miss:
        line += f"  ·  :warning: not matched in tracker: {', '.join(miss)}"
    return line


# ── Gap report: carts missing tech and/or W&M test results ──────────────────────
def gap_report():
    """(completeness counts, list of carts missing either result)."""
    return sheets.results_completeness(), sheets.carts_missing_results(which="any")


def fmt_gap_report(comp, missing, limit=15):
    lines = [f":bar_chart: *Results completeness* — *{comp['both']}/{comp['total']}* carts have BOTH tech + test results.",
             f"Missing tech: *{comp['missingTech']}*  ·  Missing test: *{comp['missingTest']}*  ·  Missing both: *{comp['neither']}*"]
    for c in missing[:limit]:
        tags = ([] if c["hasTech"] else ["tech"]) + ([] if c["hasTest"] else ["test"])
        lines.append(f"• `{c['cartId']}` — {c.get('store') or '?'} ({c.get('state') or '?'}) · missing: {', '.join(tags)}")
    if len(missing) > limit:
        lines.append(f"…and {len(missing) - limit} more. Ask the bot: _show carts missing results_.")
    return "\n".join(lines)


def post_gap_report(notify):
    try:
        comp, missing = gap_report()
        if notify and (missing or comp.get("total")):
            notify(fmt_gap_report(comp, missing))
    except Exception as e:
        log.warning("gap report failed: %s", e)


def start_poller(notify=None, interval=POLL_SECONDS):
    """Run the tech-folder scan (and the periodic gap report) in a daemon thread."""
    def loop():
        log.info("Tech monitor running every %ss on folder %s", interval, TECH_FOLDER_ID)
        last_gap = time.time()     # start the clock now; first report fires after a full interval
        while True:
            try:
                r = process_tech_files(notify=notify)
                if r.get("processed"):
                    log.info("Tech monitor wrote results for %s file(s)", r["processed"])
            except Exception as e:
                log.error("Tech monitor loop error: %s", e)
            if GAP_REPORT_SECONDS and (time.time() - last_gap) >= GAP_REPORT_SECONDS:
                post_gap_report(notify)
                last_gap = time.time()
            time.sleep(interval)

    t = threading.Thread(target=loop, name="tech-monitor", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Apply tech calibration sheets to the RSA Tracker")
    ap.add_argument("--backfill", action="store_true",
                    help="re-apply EVERY file in the tech folder now (ignores the processed cache)")
    ap.add_argument("--gaps", action="store_true", help="print the results-completeness gap report and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        sheets.ensure_tech_results_columns()
    except Exception as e:
        print("Warning: could not verify Tech Results columns:", e)

    if args.gaps:
        comp, missing = gap_report()
        print(fmt_gap_report(comp, missing, limit=100).replace("*", "").replace(":bar_chart:", "").replace("•", " -"))
        raise SystemExit(0)

    print("Backfilling ALL tech files…" if args.backfill else "Running one self-healing tech pass…")
    res = process_tech_files(notify=None, force=args.backfill)
    if res.get("error"):
        print("Error:", res["error"])
    else:
        print(f"\nDone — applied {res.get('processed', 0)} file(s) to the RSA Tracker. Refresh the dashboard.")
