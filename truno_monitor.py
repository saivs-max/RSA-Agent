#!/usr/bin/env python3
"""
truno_monitor.py — Python Truno status monitor (replaces GAS `monitorTrunoTracker`).

Watches the TRUNO RSA tracker and mirrors visit-status transitions onto the RSA Tracker:
  • Scheduled  → RSA Status "Scheduled RSA" + the Truno visit date + vendor TRUNO; ETS reminder
  • Cancelled  → reset RSA Status to "Pending RSA", clear the scheduled date, ping the DRI
  • Completed  → IGNORED here (the Python form pipeline — drive_monitor/test_forms — reads the
                 actual Scale Test Form and writes Pass→Completed RSA / Fail→Failed RSA + CSM tag)

ADDRESS IS THE PRIMARY KEY. A Truno row is matched to RSA carts by resolving the store from its
ADDRESS (col E) against the Caper deployment sheet (store_map), then matching the cart number to
the RSA serial's `_<store#>_M3_<cart#>` segment. Store code (ISC0050-WKF-0113) and store name are
only fallbacks. A match is used ONLY when unique; anything ambiguous is flagged for a human.

Idempotent: a processed-key file (store|cart|status — NOT the date) is written atomically; the
first run baselines (snapshots current rows, no notifications); a cancellation clears the prior
'scheduled' key so a later re-schedule re-fires. Cart cells that are dates (bad/shifted rows) are
skipped, never acted on.

Run from the project folder:
    python3 truno_monitor.py            # one pass
    python3 truno_monitor.py --baseline # snapshot current rows as handled, no writes/alerts
    python3 truno_monitor.py --backfill # act on every actionable row (ignore the processed cache)
Started in the background by rsa_agent on launch (poller).
"""
import os
import re
import json
import time
import difflib
import logging
import threading

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import gspread

import rsa_sheets as sheets
import store_map

log = logging.getLogger("rsa-agent.truno")

PROCESSED_FILE  = os.environ.get("TRUNO_PROCESSED_FILE", "processed_truno.json")
POLL_SECONDS    = int(os.environ.get("RSA_TRUNO_POLL_SECONDS", "7200"))   # 2h, matches the old trigger
MONITOR_CHANNEL = os.environ.get("RSA_MONITOR_CHANNEL", "C0ARJQWG7RP")
ETS_MENTION     = os.environ.get("ETS_MENTION", "")     # e.g. <!subteam^S123> — ETS coffee-break group
T_TRUNO_CODE    = store_map.T_TRUNO_CODE                 # col B — store code (ISC0050-WKF-0113)

# Optional DRI-name → Slack-id map (JSON in env) so cancellations can @-mention the DRI.
try:
    _DRI_SLACK = {k.lower(): v for k, v in json.loads(os.environ.get("RSA_DRI_SLACK_MAP", "{}")).items()}
except Exception:
    _DRI_SLACK = {}


def _canon_status(raw):
    """Free-form Truno status → one of scheduled|cancelled|completed|'' (ignored).

    Rules (checked in order):
    • cancelled  — contains "cancel", "reschedule", or "closed"
    • completed  — contains "complet"
    • scheduled  — contains "schedul" but NOT "pending" (so "Pending Scheduled with
                   manager" is ignored until TRUNO confirms an actual date)
    • ''         — anything else: pending, blank, duplicate, totals, etc.
    """
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    if "cancel" in s or "reschedule" in s or "closed" in s:
        return "cancelled"
    if "complet" in s:
        return "completed"
    if "schedul" in s and "pending" not in s:
        return "scheduled"          # explicit "Scheduled" only — not "Pending Scheduled …"
    return ""


def _is_date_cart(raw, s):
    """True when the Cart cell is actually a date (bad data / a shifted Truno row) and so can't
    identify a cart — those rows are skipped, never acted on."""
    if hasattr(raw, "strftime") or raw.__class__.__name__ in ("datetime", "date"):
        return True
    return bool(re.search(r"\b(19|20)\d{2}\b", s) or
                re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|mon|tue|wed|thu|fri|sat|sun)", s, re.I))


def _dri_mention(dri):
    sid = _DRI_SLACK.get(str(dri or "").strip().lower())
    return f"<@{sid}>" if sid else (dri or "DRI")


# ── Matching: EXPLICIT MAP first, then address/code/name/directory + fuzzy ───────
def _serial_parts(cid):
    """(store#, cart#, letter) from an RSA serial `_<store>_M3_<cart>[letter]`."""
    sm = re.search(r"_0*(\d+)_M3_0*(\d+)([A-Za-z]?)", str(cid))
    if not sm:
        return None, None, None
    return (sm.group(1).lstrip("0") or sm.group(1),
            sm.group(2).lstrip("0") or sm.group(2), sm.group(3).lower())


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _candidate_store_nums(store, code, address, caper_by_addr, caper_by_num):
    """EVERY plausible store number for a Truno row (so we exhaust options before giving
    up), plus a canonical store name. Signals: Truno address → Caper, the store code
    (ISC0050-WKF-0113 → 113), numbers in the store name, and the Deployed Stores
    directory (external id + the internal-id trailing number)."""
    nums, basis, canon = [], None, None
    ak = store_map.addr_key(address)
    hits = caper_by_addr.get(ak) if ak else None
    if hits:
        n = (hits[0].get("number") or "").lstrip("0") or hits[0].get("number")
        if n:
            nums.append(n); basis = "address"
    _chain, code_num = store_map.parse_truno_code(code)
    if code_num:
        nums.append(code_num); basis = basis or "code"
    for x in sheets._store_nums(store):
        nums.append(x.lstrip("0") or x)
    for q in (store, code):
        try:
            m = store_map.match_store(q)
        except Exception:
            m = {"status": "none"}
        if m.get("status") == "matched":
            rec = m["store"]; canon = rec.get("display")
            en = store_map._store_num_from_ext(rec.get("external_id", ""))
            if en:
                nums.append(en)
            im = re.search(r"(\d+)$", rec.get("internal_id", "") or "")
            if im:
                nums.append(im.group(1).lstrip("0") or im.group(1))
            basis = basis or "directory"
            break
    seen, out = set(), []
    for n in map(str, nums):
        if n and n not in seen:
            seen.add(n); out.append(n)
    return out, (basis or "name"), canon


def _fuzzy_store(rsa_name, targets):
    """Last-resort: does an RSA store NAME match the Truno/canonical store? Conservative —
    if both names carry numbers they must agree (so 'ShopRite 496' can't match '…472')."""
    rn = _norm(rsa_name)
    if not rn:
        return False
    for t in map(_norm, targets):
        if len(t) < 5:
            continue
        tn, rnn = re.findall(r"\d+", t), re.findall(r"\d+", rn)
        if tn and rnn and set(tn) != set(rnn):
            continue
        if t == rn or t in rn or rn in t:
            return True
        if difflib.SequenceMatcher(None, t, rn).ratio() >= 0.86:
            return True
    return False


def resolve_truno_row(store, code, address, cart_cell, rsa_rows, truno_map,
                      caper_by_addr, caper_by_num):
    """Map a Truno row to RSA cart(s). Returns dict with matches=[(row,serial)],
    ambiguous=[(cart,[serials])], unmatched=[cart], store_serials, store_found(bool),
    basis. Order: (1) the EXPLICIT map written when the agent created the row (exact),
    then (2) inference over every candidate store number + a fuzzy store-name fallback."""
    matches, ambiguous, unmatched = [], [], []
    cart_nums = [c.strip() for c in re.split(r"[,/]+", str(cart_cell or "")) if c.strip()]
    serial_index = {}
    for i, r in enumerate(rsa_rows):
        cid = sheets._cell(r, sheets.C_CARTID)
        if cid:
            serial_index[cid] = sheets.RSA_DATA_START + i

    # 1) EXPLICIT MAP — exact, no inference (agent-created rows).
    ak = store_map.addr_key(address)
    mapped = (truno_map.get(ak) or {}).get("carts", {}) if ak else {}
    used_map, remaining, matched_ids = False, [], set()
    for cn in cart_nums:
        key = cn.strip().lstrip("0") or cn.strip()
        serial = mapped.get(key) or mapped.get(cn.strip())
        if serial and serial in serial_index:
            matches.append((serial_index[serial], serial)); matched_ids.add(serial); used_map = True
        else:
            remaining.append(cn)
    basis = "map" if used_map else None

    # 2) INFERENCE for whatever the map didn't cover.
    cand_nums, inf_basis, canon = _candidate_store_nums(store, code, address, caper_by_addr, caper_by_num)
    want = set(cand_nums)
    targets = [x for x in (canon, store) if x]
    store_rows = []
    for i, r in enumerate(rsa_rows):
        cid = sheets._cell(r, sheets.C_CARTID)
        if not cid:
            continue
        s_num, c_num, c_let = _serial_parts(cid)
        if (s_num and s_num in want) or _fuzzy_store(sheets._cell(r, sheets.C_STORE), targets):
            store_rows.append((sheets.RSA_DATA_START + i, cid, c_num, c_let))
    store_serials = [r[1] for r in store_rows]
    store_found = used_map or bool(store_rows)

    if not cart_nums:                       # no cart detail → safe only if the store has one cart
        if len(store_rows) == 1:
            matches.append((store_rows[0][0], store_rows[0][1]))
        elif store_rows:
            ambiguous.append(("(unspecified)", list(store_serials)))
    else:
        for cn in remaining:
            if basis is None:
                basis = inf_basis
            m = re.match(r"0*(\d+)([A-Za-z]?)", cn.strip().upper())
            if not m:
                unmatched.append(cn); continue
            want_cart = m.group(1).lstrip("0") or m.group(1)
            want_let = m.group(2).lower()
            hits = [r for r in store_rows if r[2] == want_cart and r[1] not in matched_ids]
            if len(hits) > 1 and want_let:
                exact = [r for r in hits if r[3] == want_let]
                if exact:
                    hits = exact
            if len(hits) == 1:
                matches.append((hits[0][0], hits[0][1])); matched_ids.add(hits[0][1])
            elif not hits:
                unmatched.append(cn)         # no RSA cart with this number (store may still be known)
            else:
                ambiguous.append((cn, [r[1] for r in hits]))

    # A serial we already matched is never a candidate for another cart.
    ambiguous = [(cn, cand) for cn, cand in
                 ((cn, [c for c in cands if c not in matched_ids]) for cn, cands in ambiguous) if cand]
    return {"matches": matches, "ambiguous": ambiguous, "unmatched": unmatched,
            "store_serials": store_serials, "store_found": store_found, "basis": basis or "name"}


# ── Idempotency (atomic processed file) ──────────────────────────────────────────
def _load_processed():
    try:
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_processed(keys):
    try:
        tmp = PROCESSED_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(keys), f)
        os.replace(tmp, PROCESSED_FILE)     # atomic — never leaves a truncated file
    except Exception as e:
        log.warning("could not save %s: %s", PROCESSED_FILE, e)


def _sig(store, cart, status):
    return re.sub(r"[^a-z0-9|]", "_", f"{store}|{cart}|{status}".lower())


# ── Main pass ─────────────────────────────────────────────────────────────────────
def process_truno(notify=None, force=False, baseline=False):
    """One monitoring pass. notify(text) posts to Slack (optional). force ignores the cache
    (backfill). baseline snapshots current rows as handled without writing/alerting."""
    def say(msg):
        log.info(msg)
        if notify:
            try:
                notify(msg)
            except Exception:
                pass

    processed = _load_processed()
    first_run = not processed
    try:
        truno = sheets._truno_data(force=force)
        rsa_rows = sheets._rsa_data(force=force)
    except Exception as e:
        say(f":warning: Truno monitor could not read the sheets: {e}")
        return {"error": str(e)}

    try:
        caper_by_addr, caper_by_num = store_map.caper_index()
    except Exception:
        caper_by_addr, caper_by_num = {}, {}
    truno_map = sheets._load_truno_map()        # explicit RSA↔Truno links (agent-created rows)

    # De-duplicate Truno rows: when the same (store, cart) appears more than once,
    # keep only the latest row (highest row number). This prevents an older duplicate
    # row (e.g. a stale ShopRite 116 entry) from blocking the newer one from syncing.
    _last_idx: dict = {}
    for idx, r in enumerate(truno):
        s = sheets._cell(r, sheets.T_STORE).lower().strip()
        c = sheets._cell(r, sheets.T_CART).lower().strip()
        if s:
            _last_idx[(s, c)] = idx
    truno = [r for idx, r in enumerate(truno)
             if not sheets._cell(r, sheets.T_STORE).strip()
             or _last_idx.get((sheets._cell(r, sheets.T_STORE).lower().strip(),
                               sheets._cell(r, sheets.T_CART).lower().strip())) == idx]

    cells, acted = [], {"scheduled": 0, "cancelled": 0, "ambiguous": 0, "skipped": 0, "notFound": 0}
    for r in truno:
        store  = sheets._cell(r, sheets.T_STORE)
        code   = sheets._cell(r, T_TRUNO_CODE)
        addr   = sheets._cell(r, sheets.T_ADDRESS)
        cartRaw = r[sheets.T_CART - 1] if len(r) >= sheets.T_CART else ""
        cart   = sheets._cell(r, sheets.T_CART)
        visit  = sheets._cell(r, sheets.T_VISIT)
        status = _canon_status(sheets._cell(r, sheets.T_STATUS))
        if (not store and not code) or not status:
            continue

        sig = _sig(store + "|" + code, cart, status)
        if sig in processed and not force:
            continue
        if (baseline or first_run) and not force:
            processed.add(sig)              # snapshot only — no writes/alerts on the first/baseline pass
            continue
        if status == "completed":
            # Write the completion date back to the RSA Tracker.
            # The pass/fail result is owned by the form pipeline (drive_monitor/test_forms),
            # but the visit date belongs here — write C_RSACOMPL if it isn't already set.
            res = resolve_truno_row(store, code, addr, cart, rsa_rows, truno_map, caper_by_addr, caper_by_num)
            comp_cells = []
            for sheet_row, serial in res.get("matches", []):
                data = rsa_rows[sheet_row - sheets.RSA_DATA_START]
                existing_compl = sheets._cell(data, sheets.C_RSACOMPL)
                if not existing_compl:                             # don't overwrite a form-pipeline date
                    comp_cells.append(gspread.Cell(sheet_row, sheets.C_RSACOMPL, visit or sheets._today()))
                    comp_cells.append(gspread.Cell(sheet_row, sheets.C_LASTUPD, sheets._today()))
                    acted["completed"] = acted.get("completed", 0) + 1
                    say(f":white_check_mark: *RSA Completed* — `{serial}` at _{store}_"
                        + (f" on *{visit}*" if visit else "")
                        + f" (completion date written to tracker). "
                        + f"<{sheets.webapp_url(serial)}|Open in tracker>")
            if comp_cells:
                cells.extend(comp_cells)
            processed.add(sig)
            continue
        if _is_date_cart(cartRaw, cart):
            log.info("Truno monitor: %s Cart cell isn't a cart (%r) — skipped. Fix the Truno row.", store or code, cart)
            processed.add(sig)
            acted["skipped"] += 1
            continue

        res = resolve_truno_row(store, code, addr, cart, rsa_rows, truno_map, caper_by_addr, caper_by_num)
        matches, ambiguous, unmatched = res["matches"], res["ambiguous"], res["unmatched"]
        store_serials, store_found, basis = res["store_serials"], res["store_found"], res["basis"]

        if not store_found:
            # Tried the explicit map + address/code/name/directory + fuzzy — still nothing.
            # Alert ONCE (so it doesn't repeat every pass) but leave the row unprocessed so
            # it re-checks and self-heals once the RSA cart shows up.
            nf = "nf|" + sig
            if force or nf not in processed:
                acted["notFound"] += 1
                say(f":warning: *Truno row not found in the RSA Tracker* — {store or code} "
                    f"cart \"{cart}\" is *{status}*, but I couldn't map it to any RSA cart "
                    f"(tried the RSA↔Truno map, address, store code, name, the store directory, "
                    f"and fuzzy name matching). Please check the store/cart. "
                    f"<{sheets.webapp_url()}|Open tracker>")
                processed.add(nf)
            continue
        if not matches and not ambiguous and not unmatched:
            continue                        # nothing actionable this pass → retry

        for sheet_row, serial in matches:
            data = rsa_rows[sheet_row - sheets.RSA_DATA_START]
            if status == "scheduled":
                if not visit or visit.strip().upper() == "TBD":
                    # TRUNO status is "Scheduled" but no real date yet — wait until an
                    # actual date is set before writing anything or notifying.
                    log.info("Truno monitor: %s cart %s is Scheduled but visit date is %r — skipping until real date arrives.", store, serial, visit or "(blank)")
                    continue
                cells.append(gspread.Cell(sheet_row, sheets.C_RSAVENDOR, "TRUNO"))
                cells.append(gspread.Cell(sheet_row, sheets.C_RSASTATUS, "Scheduled RSA"))
                cells.append(gspread.Cell(sheet_row, sheets.C_RSASCHED, visit))
                cells.append(gspread.Cell(sheet_row, sheets.C_LASTUPD, sheets._today()))
                acted["scheduled"] += 1
                say(f":calendar: *RSA Scheduled* — `{serial}` at _{store}_"
                    + f" on *{visit}*" + f" (matched by {basis}). "
                    + f"<{sheets.webapp_url(serial)}|Open in tracker>")
            elif status == "cancelled":
                dri = sheets._cell(data, sheets.C_DRI)
                cells.append(gspread.Cell(sheet_row, sheets.C_RSASCHED, ""))          # clear scheduled date
                cells.append(gspread.Cell(sheet_row, sheets.C_RSASTATUS, "Pending RSA"))
                cells.append(gspread.Cell(sheet_row, sheets.C_LASTUPD, sheets._today()))
                acted["cancelled"] += 1
                say(f":x: *TRUNO Visit Cancelled* — {_dri_mention(dri)} cart `{serial}` at _{store}_ reset to "
                    f"*Pending RSA*; please reschedule with Truno. <{sheets.webapp_url(serial)}|Open in tracker>")

        if ambiguous or unmatched:
            acted["ambiguous"] += 1
            # Deep-link to the WEBAPP (never the raw sheet), scoped to this store: prefer a
            # cart we just matched, else any RSA serial for the store, else the webapp home.
            rep = (matches[0][1] if matches else (store_serials[0] if store_serials else None))
            link = sheets.webapp_url(rep) if rep else sheets.webapp_url()
            parts = []
            if matches:                      # tell the user what DID get tagged, so a matched
                done = ", ".join(f"`{serial}`" for _row, serial in matches)   # cart isn't read as
                parts.append(f"already {status} {done}")                      # "couldn't match"
            for cn, cands in ambiguous:      # a real tie — list only the genuine candidates
                clist = ", ".join(f"`{c}`" for c in sorted(set(cands))[:8])
                parts.append(f"cart *{cn}* could be {clist}")
            if unmatched:                    # no RSA cart with that number — don't guess
                parts.append("no RSA cart found for cart " + ", ".join(f"*{c}*" for c in unmatched))
            say(f":grey_question: *Truno match needs confirmation* — {store or code} cart \"{cart}\" is now "
                f"*{status}*: " + "; ".join(parts) + ". Please tag the right cart's RSA Status "
                f"in the webapp, or reply with the correct cart id. <{link}|Open in webapp>")

        # On a cancellation, forget the prior 'scheduled' key so a later re-schedule re-fires.
        if status == "cancelled":
            processed.discard(_sig(store + "|" + code, cart, "scheduled"))
        processed.add(sig)

    if cells and not (baseline or first_run):
        try:
            sheets._ws(sheets.RSA_SPREADSHEET_ID, sheets.RSA_SHEET).update_cells(cells)
        except Exception as e:
            say(f":warning: Truno monitor write failed: {e}")
            return {"error": str(e)}

    _save_processed(processed)
    if first_run and not force:
        log.info("Truno monitor: baseline snapshot of %d rows (no alerts).", len(processed))
    return {"baseline": bool(first_run or baseline), **acted}


FULL_RESYNC_SECONDS = int(os.environ.get("TRUNO_RESYNC_SECONDS", str(24 * 3600)))  # default: daily

def start_poller(notify=None, interval=POLL_SECONDS):
    """Run the Truno monitor in a daemon thread (started by rsa_agent on launch).

    Two passes run concurrently:
    • Incremental (every `interval` seconds, default 2h) — processes only new/changed
      rows using the processed cache; posts Slack notifications for each action.
    • Full resync (every TRUNO_RESYNC_SECONDS, default 24h) — force=True, notify=None:
      silently re-applies all Truno statuses to the RSA Tracker, catching any drift
      without generating repeat Slack noise.
    """
    def incremental_loop():
        log.info("Truno monitor (incremental) running every %ss", interval)
        while True:
            try:
                process_truno(notify=notify)
            except Exception as e:
                log.error("Truno incremental loop error: %s", e)
            time.sleep(interval)

    def resync_loop():
        log.info("Truno monitor (full resync) running every %ss", FULL_RESYNC_SECONDS)
        while True:
            time.sleep(FULL_RESYNC_SECONDS)   # wait first — incremental pass runs on startup
            try:
                res = process_truno(notify=None, force=True)
                changed = sum(v for k, v in res.items()
                              if k in ("scheduled", "completed", "cancelled") and isinstance(v, int))
                if changed:
                    log.info("Truno full resync: %d row(s) updated (scheduled=%s completed=%s cancelled=%s)",
                             changed, res.get("scheduled", 0), res.get("completed", 0), res.get("cancelled", 0))
                    if notify:
                        try:
                            notify(f":arrows_counterclockwise: Truno full resync: {changed} row(s) updated "
                                   f"(scheduled={res.get('scheduled',0)}, "
                                   f"completed={res.get('completed',0)}, "
                                   f"cancelled={res.get('cancelled',0)}).")
                        except Exception:
                            pass
            except Exception as e:
                log.error("Truno resync loop error: %s", e)

    threading.Thread(target=incremental_loop, name="truno-monitor", daemon=True).start()
    threading.Thread(target=resync_loop, name="truno-resync", daemon=True).start()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Mirror Truno visit statuses onto the RSA Tracker (address-keyed)")
    ap.add_argument("--baseline", action="store_true", help="snapshot current rows as handled (no writes/alerts)")
    ap.add_argument("--backfill", action="store_true", help="act on every actionable row (ignore the processed cache)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = process_truno(notify=None, force=args.backfill, baseline=args.baseline)
    print("Truno monitor:", res)
