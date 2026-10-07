#!/usr/bin/env python3
"""
RSA Agent — Drive folder monitor (Truno-tracker driven).

Trigger: a Truno tracker row with "Forms sent to team? = Yes". For each such store,
we search the Drive folder for that store's Scale Test Form (by store name/number),
parse it via the gateway vision, and write the confirmed Pass/Fail onto the matching
RSA Tracker cart(s).

- Self-healing: each pass also (re)applies a store's form when its matching RSA
  cart still has NO result yet — so a form that landed before the cart existed, or
  was missed, gets picked up on a later pass. New files are always processed.
- `--backfill` re-applies EVERY forms-sent=Yes store's form across the whole Truno
  sheet (ignores the processed cache) — use it once to append all completed results.
- Only PDFs/images are taken here; GAS still handles Excel/CSV in the same folder.
- Errors (Truno/Drive read, missing form, parse failure) are surfaced via notify().

Prereqs: share the folder with the service-account email (Viewer) and enable the
Drive API. Started in the background by rsa_agent on launch; also runnable directly:
    python3 drive_monitor.py             # one self-healing pass
    python3 drive_monitor.py --backfill  # re-apply every forms-sent form now
"""

import os
import re
import json
import logging
import threading
import time

try:                                   # load .env so a standalone run gets model/keys/folder
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import rsa_sheets as sheets
import test_forms

log = logging.getLogger("rsa-agent.drive")

SA_KEY_FILE         = os.environ.get("GOOGLE_SA_KEY_FILE", "sa-key.json")
RSA_FORMS_FOLDER_ID = os.environ.get("RSA_FORMS_FOLDER_ID", "1nXWekoXow3-az02QHfVSx7PJQP4uA4vx")
POLL_SECONDS        = int(os.environ.get("RSA_FOLDER_POLL_SECONDS", "300"))
PROCESSED_FILE      = os.environ.get("RSA_PROCESSED_FILE", "processed_forms.json")
# Drive scope must allow file moves (not readonly). Re-run service-account auth if changed.
DRIVE_SCOPES        = ["https://www.googleapis.com/auth/drive"]
CSM_MENTION         = os.environ.get("CSM_MENTION", "")   # e.g. <@U123> or <!subteam^S123> — tagged when a cart passes

ARCHIVE_FOLDER_NAME  = os.environ.get("RSA_ARCHIVE_FOLDER_NAME",  "Archive")
NO_MATCH_FOLDER_NAME = os.environ.get("RSA_NO_MATCH_FOLDER_NAME", "No Match")

# Cached subfolder IDs (resolved once per process start)
_folder_cache: dict = {}

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


def _get_subfolder(name):
    """Return the Drive folder ID for a named child of RSA_FORMS_FOLDER_ID.
    Creates the folder if it doesn't exist.  Result is cached for the process lifetime."""
    if name in _folder_cache:
        return _folder_cache[name]
    q = (f"'{RSA_FORMS_FOLDER_ID}' in parents and trashed=false and "
         f"mimeType='application/vnd.google-apps.folder' and name='{name}'")
    resp = _retry(lambda: _service().files().list(
        q=q, fields="files(id,name)", spaces="drive").execute(num_retries=3))
    items = resp.get("files", [])
    if items:
        fid = items[0]["id"]
    else:
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
                "parents": [RSA_FORMS_FOLDER_ID]}
        f = _retry(lambda: _service().files().create(
            body=meta, fields="id").execute(num_retries=3))
        fid = f["id"]
        log.info("Created Drive subfolder '%s' (id=%s)", name, fid)
    _folder_cache[name] = fid
    return fid


def _move_file(file_id, dest_folder_id):
    """Move a Drive file from RSA_FORMS_FOLDER_ID into dest_folder_id."""
    try:
        _retry(lambda: _service().files().update(
            fileId=file_id,
            addParents=dest_folder_id,
            removeParents=RSA_FORMS_FOLDER_ID,
            fields="id,parents",
        ).execute(num_retries=3))
        return True
    except Exception as e:
        log.warning("Could not move file %s to folder %s: %s", file_id, dest_folder_id, e)
        return False



def _retry(fn, tries=3, base=1.5):
    """Run a Drive call, retrying transient network hiccups (broken pipe / connection
    reset / timeout) with backoff and a rebuilt connection. Non-transient errors raise."""
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
    """Transient read failures are logged (they self-heal next pass); only real/persistent
    errors are surfaced to Slack — so a momentary Drive blip doesn't flood the channel."""
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


def _list_folder_files():
    """All PDF/image files currently in the folder (newest first)."""
    q = (f"'{RSA_FORMS_FOLDER_ID}' in parents and trashed=false and "
         "(mimeType='application/pdf' or mimeType contains 'image/')")
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


def _match_file(files, store):
    """Find the store's form in the folder by store number in the filename.
    Files are newest-first, so the first match is the latest form for the store.
    Year-like tokens (19xx/20xx) in the filename are ignored so a date in the
    name (e.g. ...2026-06-10.pdf) can't be mistaken for a store number. If the
    store has no number at all, fall back to a distinctive name token (>=4 chars)."""
    nums = sheets._store_nums(store)
    generic = ("scale", "test", "form", "store", "location")
    name_tokens = [t for t in re.findall(r"[a-z]{4,}", store.lower()) if t not in generic]
    # Known retail banners — used to REJECT a same-number file that clearly belongs to a
    # DIFFERENT chain (e.g. 'Davis 350' must not grab 'McKeevers 350 ….pdf'). A neutral
    # bare-number file ('0350 - date.pdf') or a city-named one is still accepted.
    CHAINS = {"shoprite", "sprouts", "kroger", "weis", "schnucks", "mckeevers", "davis",
              "gelson", "gelsons", "sobeys", "geisslers", "bowman", "aldi", "wakefern", "freshgrocer"}
    num_only = None
    for f in files:
        raw = {n for n in re.findall(r"\d{2,}", f["name"]) if not re.fullmatch(r"(19|20)\d{2}", n)}
        name_nums = raw | {n.lstrip("0") or n for n in raw}   # whole-token numbers (+ unpadded)
        flat = re.sub(r"[^a-z0-9]", "", f["name"].lower())
        if nums and (nums & name_nums):                       # store number matches a filename token
            if any(t in flat for t in name_tokens):
                return f                                      # our store name is in the file → confident
            other_chain = any(c in flat for c in CHAINS if c not in name_tokens)
            if num_only is None and not other_chain:
                num_only = f                                  # neutral bare-number file → fallback
            # else: filename names a different chain → skip (don't steal its form)
        elif not nums and any(t in flat for t in name_tokens):
            return f                                          # numberless store → a name token
    return num_only                                           # best neutral number match, or None


def _download(file_id):
    return _retry(lambda: _service().files().get_media(fileId=file_id).execute(num_retries=3))  # bytes


def _carts_missing_result(row, rsa_rows, has_result):
    """True if any cart on this Truno row isn't in the tracker yet, or is there
    but still has no RSA Test Result — i.e. this store's form should be applied."""
    carts = [c.strip() for c in re.split(r"[,/]+", str(row.get("cart") or "")) if c.strip()]
    if not carts:
        return True                              # unknown cart(s) → safest to apply
    for cn in carts:
        serial = sheets.match_cart(row.get("store", ""), cn, rsa_rows)
        if not serial or not has_result.get(serial):
            return True
    return False


def process_confirmed_forms(notify=None, force=False):
    """For every Truno row with 'Forms sent = Yes', find its form in the folder and
    apply the parsed Pass/Fail to the matching RSA cart(s).

    A store's form is applied when: it's a NEW file, OR --backfill/force is set, OR a
    matching cart still has no RSA Test Result yet (self-healing). Re-applying the
    same result is idempotent, so this is safe to run on every poll and to backfill."""
    def say(msg):
        log.info(msg)
        if notify:
            try:
                notify(msg)
            except Exception:
                pass

    processed = _load_processed()
    try:
        rows = sheets.truno_confirmed_rows()
    except Exception as e:
        return _read_fail("the Truno tracker", e, say)
    try:
        files = _list_folder_files()
    except Exception as e:
        return _read_fail("the Drive folder", e, say)

    # One RSA read per pass → which carts already carry a result (for self-healing).
    try:
        rsa_vals = sheets._ws(sheets.RSA_SPREADSHEET_ID, sheets.RSA_SHEET).get_all_values()
        rsa_rows = rsa_vals[sheets.RSA_DATA_START - 1:]
    except Exception as e:
        return _read_fail("the RSA Tracker", e, say)
    has_result = {}
    for r in rsa_rows:
        cid = sheets._cell(r, sheets.C_CARTID)
        if cid:
            has_result[cid] = bool(sheets._cell(r, sheets.C_RSA_RESULTS))

    done, seen = [], set()
    for row in rows:
        store = row.get("store", "")
        if store in seen:                       # one (latest) form per store per pass
            continue
        seen.add(store)
        f = _match_file(files, store)
        if not f:
            miss = "missing:" + re.sub(r"\W+", "_", store) + ":" + time.strftime("%Y-%U")
            if miss not in processed:           # re-warn at most once per store per week (not once ever)
                processed.add(miss)
                say(f":warning: *{store}* is marked Forms sent = Yes, but no matching form is in the folder yet.")
            continue
        # Apply when: forced, a new file, or a matching cart still lacks a result.
        is_new = f["id"] not in processed
        if not (force or is_new or _carts_missing_result(row, rsa_rows, has_result)):
            continue
        try:
            data = _download(f["id"])
            # Match carts by the TRUNO store (canonical), not the form's printed name.
            summary = test_forms.process_upload(data, f["name"], link=f.get("webViewLink"),
                                                store_hint=store)
        except Exception as e:
            summary = {"error": f"{type(e).__name__}: {e}"}
        processed.add(f["id"])
        done.append((f["name"], summary))

        # ── File routing: matched → Archive; unmatched → No Match ────────────
        if not summary.get("error"):
            any_matched = any(r.get("ok") for r in summary.get("results", []))
        else:
            any_matched = False
        dest_name = ARCHIVE_FOLDER_NAME if any_matched else NO_MATCH_FOLDER_NAME
        try:
            dest_id = _get_subfolder(dest_name)
            if _move_file(f["id"], dest_id):
                log.info("Moved '%s' → %s", f["name"], dest_name)
        except Exception as e:
            log.warning("Could not move '%s' to %s: %s", f["name"], dest_name, e)

        if is_new or force:
            say(_fmt(f["name"], summary))
        else:
            new_results = [r for r in summary.get("results", [])
                           if r.get("ok") and not has_result.get(r.get("cartId"))]
            if new_results:
                say(_fmt(f["name"], summary))
            else:
                log.info("Self-healing pass on %s — no new results written, suppressing repeat notification", f["name"])
        time.sleep(2)                           # pace Sheets/vision calls to stay under quota

    _save_processed(processed)
    return {"processed": len(done), "results": done}


def _fmt(name, summary):
    if summary.get("error"):
        return f":warning: `{name}`: {summary['error']}"
    res = summary.get("results", [])
    ok = sum(1 for r in res if r.get("ok"))
    miss = [str(r["cart"]) for r in res if not r.get("ok")]
    line = f":page_facing_up: `{name}` — {summary.get('store', '?')}: wrote *{ok}/{len(res)}* cart result(s)"
    if miss:
        line += f"  ·  :warning: not matched in tracker: {', '.join(miss)}"
    passed = [str(r["cart"]) for r in res if r.get("ok") and r.get("passed") is True]
    if passed:
        line += f"\n:white_check_mark: Passed RSA → *Passed* (complete): cart(s) {', '.join(passed)}."
        if CSM_MENTION:
            line += f" cc {CSM_MENTION}"
    return line


def start_poller(notify=None, interval=POLL_SECONDS):
    """Run the Truno-driven folder scan in a daemon thread."""
    def loop():
        log.info("Drive monitor running every %ss on folder %s", interval, RSA_FORMS_FOLDER_ID)
        while True:
            try:
                r = process_confirmed_forms(notify=notify)
                if r.get("processed"):
                    log.info("Drive monitor wrote results for %s form(s)", r["processed"])
            except Exception as e:
                log.error("Drive monitor loop error: %s", e)
            time.sleep(interval)

    t = threading.Thread(target=loop, name="drive-monitor", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Apply Truno Scale Test Forms to the RSA Tracker")
    ap.add_argument("--backfill", action="store_true",
                    help="re-apply EVERY forms-sent=Yes store's form now (ignores the processed cache)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("Backfilling ALL forms-sent=Yes forms…" if args.backfill
          else "Running one self-healing form pass…")
    res = process_confirmed_forms(notify=None, force=args.backfill)
    if res.get("error"):
        print("Error:", res["error"])
    else:
        print(f"\nDone — applied {res.get('processed', 0)} form(s) to the RSA Tracker. Refresh the dashboard.")
