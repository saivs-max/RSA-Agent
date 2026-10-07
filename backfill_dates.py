#!/usr/bin/env python3
"""
Backfill missing RSA Scheduled Date and RSA Completion Date on completed carts.

Two-pass matching strategy:
  1. PRIMARY — resolve_truno_row() (same logic as truno_monitor: explicit map + Caper + fuzzy)
  2. FALLBACK — retailer chain + cart number match.  Used when primary finds nothing.
     Strips the trailing store number from the RSA store name (e.g. "Sprouts 20" → "Sprouts"),
     then finds TRUNO rows for the same chain that include the cart number.  If only one
     TRUNO row matches unambiguously, its visit date is used.  This handles stores whose
     TRUNO store number differs from the RSA store number (e.g. Sprouts 20 ↔ Sprouts 920).

Idempotent: rows that already have both dates are skipped.

Usage:
    python3 backfill_dates.py           # preview what would change (dry run)
    python3 backfill_dates.py --write   # actually write to the tracker
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import gspread
import rsa_sheets as sheets
import store_map
import truno_monitor

log = logging.getLogger("rsa-agent.backfill")

# How often the auto-backfill runs (default 6 hours).  Deliberately long because
# it's a catch-up job — new completions are written immediately by truno_monitor /
# drive_monitor; this only fills in gaps that existed before those monitors ran.
BACKFILL_POLL_SECONDS = int(
    __import__("os").environ.get("RSA_BACKFILL_POLL_SECONDS", str(6 * 60 * 60))
)

# Suppress repeated "no TRUNO match" alerts for the same cart: fire once and persist
# the alerted set to disk so restarts don't re-alert the same carts.
_NO_MATCH_STATE = os.environ.get("RSA_BACKFILL_STATE_FILE", "backfill_state.json")
_alerted_no_match: set = set()


def _load_state():
    """Load persisted state (alerted cart IDs + last-notified date) from disk."""
    global _alerted_no_match
    try:
        with open(_NO_MATCH_STATE) as f:
            data = json.load(f)
        _alerted_no_match = set(data.get("alertedNoMatch", []))
        return data
    except Exception:
        _alerted_no_match = set()
        return {}


def _save_state(extra=None):
    """Persist state so it survives restarts. extra = dict of additional keys to merge."""
    try:
        existing = {}
        try:
            with open(_NO_MATCH_STATE) as f:
                existing = json.load(f)
        except Exception:
            pass
        existing["alertedNoMatch"] = sorted(_alerted_no_match)
        if extra:
            existing.update(extra)
        tmp = _NO_MATCH_STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(existing, f)
        os.replace(tmp, _NO_MATCH_STATE)
    except Exception as e:
        log.warning("could not save %s: %s", _NO_MATCH_STATE, e)


def _last_notified_date():
    try:
        with open(_NO_MATCH_STATE) as f:
            return json.load(f).get("lastNotified")
    except Exception:
        return None


_load_state()   # populate on module import

COMPLETION_STATUSES = {"completed rsa", "failed rsa"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _retailer_name(store):
    """Strip trailing store number to get just the chain name.
    'Sprouts 20' → 'sprouts'  |  'Weis 226' → 'weis'  |  "Clark's" → 'clarks'"""
    s = re.sub(r"[\s\-#]+\d+\s*$", "", str(store or "")).strip()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _retailer_match(rsa_chain, truno_chain):
    """True if the two retailer chain names refer to the same store group."""
    if not rsa_chain or not truno_chain:
        return False
    # One contains the other (handles 'sprouts' ⊂ 'sproutsfarmersmarket')
    return rsa_chain == truno_chain or rsa_chain in truno_chain or truno_chain in rsa_chain


def _cart_num_from_serial(cart_id):
    """Cart number (no leading zeros, no letter) from an RSA serial.
    Returns (num, letter) for M3 serials, or (None, None) for non-M3 serials."""
    m = re.search(r"_M3_0*(\d+)([A-Za-z]?)$", str(cart_id))
    if not m:
        return None, None
    num = m.group(1).lstrip("0") or m.group(1)
    let = m.group(2).lower()
    return num, let  # e.g. ("5", "") or ("13", "a")


def _store_num_from_serial(cart_id):
    """For non-M3 serials (e.g. P_WF_SHOPRITE_35), extract the trailing number
    as a store-number hint for TRUNO matching."""
    if re.search(r"_M3_", str(cart_id)):
        return None   # M3 serials use cart numbers, not store numbers
    m = re.search(r"_(\d+)$", str(cart_id))
    if not m:
        return None
    return m.group(1).lstrip("0") or m.group(1)


def _truno_cart_set(cart_cell):
    """Normalised set of cart numbers from a TRUNO T_CART cell (comma/slash separated)."""
    nums = set()
    for c in re.split(r"[,/\s]+", str(cart_cell or "")):
        c = c.strip().lstrip("0")
        if c:
            nums.add(c.lower())
    return nums


# ── Core ──────────────────────────────────────────────────────────────────────

def run(dry_run=False, verbose=False):
    """Backfill C_RSASCHED and C_RSACOMPL for completed RSA carts that are missing them.

    Returns {"filled": int, "skipped": int, "details": [...], "noMatch": [...], "dryRun": bool}."""
    try:
        rsa_ws   = sheets._ws(sheets.RSA_SPREADSHEET_ID, sheets.RSA_SHEET)
        rsa_rows = rsa_ws.get_all_values()[sheets.RSA_DATA_START - 1:]
    except Exception as e:
        return {"error": f"RSA read failed: {e}"}
    try:
        truno_rows = sheets._ws(sheets.TRUNO_SPREADSHEET_ID, sheets.TRUNO_SHEET).get_all_values()[1:]
    except Exception as e:
        return {"error": f"TRUNO read failed: {e}"}
    try:
        caper_by_addr, caper_by_num = store_map.caper_index()
    except Exception:
        caper_by_addr, caper_by_num = {}, {}
    truno_map = sheets._load_truno_map()

    # ── PASS 1: primary resolution (explicit map + Caper + fuzzy) ─────────────
    serial_to_visit = {}   # RSA serial → TRUNO visit date
    truno_parsed    = []   # cache for fallback pass

    for tr in truno_rows:
        t_store = sheets._cell(tr, sheets.T_STORE)
        t_code  = sheets._cell(tr, truno_monitor.T_TRUNO_CODE)
        t_addr  = sheets._cell(tr, sheets.T_ADDRESS)
        t_cart  = sheets._cell(tr, sheets.T_CART)
        t_visit = sheets._cell(tr, sheets.T_VISIT)
        if not (t_store or t_code) or not t_visit:
            continue
        truno_parsed.append({
            "store":    t_store,
            "cart":     t_cart,
            "visit":    t_visit,
            "chain":    _retailer_name(t_store),
            "cartNums": _truno_cart_set(t_cart),
        })
        res = truno_monitor.resolve_truno_row(
            t_store, t_code, t_addr, t_cart, rsa_rows,
            truno_map, caper_by_addr, caper_by_num)
        matched = [(row, serial) for row, serial in res.get("matches", [])]
        for _row, serial in matched:
            if serial not in serial_to_visit:
                serial_to_visit[serial] = t_visit
        if verbose and matched:
            for _row, serial in matched:
                print(f"  [P1] {t_store!r:35s} cart={t_cart!r:10s} visit={t_visit} → {serial}")

    # ── Main loop ─────────────────────────────────────────────────────────────
    cells, filled, no_match = [], [], []

    for idx, r in enumerate(rsa_rows):
        cart_id = sheets._cell(r, sheets.C_CARTID)
        if not cart_id:
            continue
        if sheets._cell(r, sheets.C_RSASTATUS).lower() not in COMPLETION_STATUSES:
            continue
        sched = sheets._cell(r, sheets.C_RSASCHED)
        compl = sheets._cell(r, sheets.C_RSACOMPL)
        if sched and compl:
            continue  # already populated — nothing to do

        visit = serial_to_visit.get(cart_id)

        # ── PASS 2 fallback: retailer chain + cart number (or store number) ─────
        if not visit:
            rsa_store        = sheets._cell(r, sheets.C_STORE)
            rsa_chain        = _retailer_name(rsa_store)
            cart_num, cart_let = _cart_num_from_serial(cart_id)
            store_num        = _store_num_from_serial(cart_id)  # non-M3 serials only

            if verbose:
                print(f"  [P2] {cart_id:50s} chain={rsa_chain!r:20s} "
                      f"cart_num={cart_num!r} store_num={store_num!r}")

            if rsa_chain:
                if cart_num:
                    # M3 serial — match on retailer chain + cart number
                    want_nums = {cart_num + cart_let, cart_num} if cart_let else {cart_num}
                    candidates = [
                        tr for tr in truno_parsed
                        if _retailer_match(rsa_chain, tr["chain"])
                        and tr["cartNums"] & want_nums
                    ]
                elif store_num:
                    # Non-M3 serial (e.g. P_WF_SHOPRITE_35) — match on retailer chain +
                    # store number appearing anywhere in the TRUNO store name
                    candidates = [
                        tr for tr in truno_parsed
                        if _retailer_match(rsa_chain, tr["chain"])
                        and re.search(r"\b0*" + re.escape(store_num) + r"\b", tr["store"])
                    ]
                else:
                    candidates = []

                if verbose:
                    print(f"       candidates: {[c['store'] + '/' + c['cart'] + '=' + c['visit'] for c in candidates]}")

                if len(candidates) == 1:
                    visit = candidates[0]["visit"]
                elif len(candidates) > 1:
                    if verbose:
                        print(f"       ⚠ ambiguous — skipping")

        if not visit:
            no_match.append({"cartId": cart_id,
                             "store": sheets._cell(r, sheets.C_STORE),
                             "status": sheets._cell(r, sheets.C_RSASTATUS)})
            continue

        sheet_row = idx + sheets.RSA_DATA_START
        wrote = []
        if not sched:
            if not dry_run:
                cells.append(gspread.Cell(sheet_row, sheets.C_RSASCHED, visit))
            wrote.append("RSA Scheduled Date")
        if not compl:
            if not dry_run:
                cells.append(gspread.Cell(sheet_row, sheets.C_RSACOMPL, visit))
            wrote.append("RSA Completion Date")
        if not dry_run:
            cells.append(gspread.Cell(sheet_row, sheets.C_LASTUPD, sheets._today()))
        filled.append({"cartId": cart_id,
                       "store": sheets._cell(r, sheets.C_STORE),
                       "visit": visit, "wrote": wrote})

    if cells and not dry_run:
        rsa_ws.update_cells(cells)

    return {"filled": len(filled), "skipped": len(no_match),
            "details": filled, "noMatch": no_match,
            "dryRun": dry_run, "trackerUrl": sheets.webapp_url()}


# ── CLI ───────────────────────────────────────────────────────────────────────

def start_poller(notify=None, interval=BACKFILL_POLL_SECONDS):
    """Run the date backfill in a daemon thread, posting results to Slack via notify().

    On each pass:
    • Carts whose missing date(s) were filled → one Slack message listing them.
    • Carts with no TRUNO match → one Slack message per *new* unresolved cart (not
      repeated every pass — suppressed after the first alert so the channel stays clean).
    """
    def _say(text):
        log.info(text)
        if notify:
            try:
                notify(text)
            except Exception:
                pass

    def loop():
        log.info("Backfill poller running every %ss", interval)
        while True:
            try:
                result = run(dry_run=False)
                if result.get("error"):
                    _say(f":warning: *Date backfill error:* {result['error']}")
                else:
                    filled   = result.get("details", [])
                    no_match = result.get("noMatch", [])

                    if filled:
                        lines = [":calendar: *Backfilled missing RSA dates*"]
                        for d in filled:
                            cols = " + ".join(d["wrote"])
                            lines.append(f"  • `{d['cartId']}` ({d['store']}) — {d['visit']} → {cols}")
                        msg = "\n".join(lines)
                        log.info(msg)
                        # Post to Slack at most once per day — restarts don't re-post.
                        from datetime import datetime as _dt
                        today = _dt.now().strftime("%Y-%m-%d")
                        if notify and _last_notified_date() != today:
                            try:
                                notify(msg)
                                _save_state({"lastNotified": today})
                            except Exception:
                                pass

                    # Log unresolved carts — log only, no Slack post.
                    new_no_match = [nm for nm in no_match
                                    if nm["cartId"] not in _alerted_no_match]
                    if new_no_match:
                        for nm in new_no_match:
                            log.info("No TRUNO visit date: %s (%s) — %s",
                                     nm["cartId"], nm["store"], nm["status"])
                            _alerted_no_match.add(nm["cartId"])
                        _save_state()

            except Exception as e:
                log.error("Backfill poller error: %s", e)

            time.sleep(interval)

    t = threading.Thread(target=loop, name="backfill-dates", daemon=True)
    t.start()
    return t


def diagnose(keyword=None):
    """Dump TRUNO rows (optionally filtered by keyword) showing raw column values,
    the extracted chain/cartNums, and whether a visit date was found.  Use this to
    debug why a store isn't resolving."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    try:
        truno_rows = sheets._ws(sheets.TRUNO_SPREADSHEET_ID, sheets.TRUNO_SHEET).get_all_values()
    except Exception as e:
        print(f"❌ Could not read TRUNO sheet: {e}")
        return

    print(f"TRUNO sheet: {len(truno_rows)} rows total (including header)\n")
    print(f"{'Row':>4}  {'Store (col A)':30s}  {'Cart (col C)':15s}  {'Visit (col F)':15s}  {'Status (col K)':20s}  chain")
    print("-" * 110)
    kw = keyword.lower() if keyword else None
    shown = 0
    for i, row in enumerate(truno_rows):
        t_store  = sheets._cell(row, sheets.T_STORE)
        t_cart   = sheets._cell(row, sheets.T_CART)
        t_visit  = sheets._cell(row, sheets.T_VISIT)
        t_status = sheets._cell(row, sheets.T_STATUS)
        chain    = _retailer_name(t_store) if t_store else ""
        raw_cart = row[sheets.T_CART - 1] if len(row) >= sheets.T_CART else "(missing)"
        raw_visit= row[sheets.T_VISIT - 1] if len(row) >= sheets.T_VISIT else "(missing)"
        if kw and kw not in (t_store + t_cart + t_visit + t_status).lower():
            continue
        marker = "✓" if t_visit else " "
        print(f"{marker}{i+1:>4}  {t_store:30s}  {str(raw_cart):15s}  {str(raw_visit):15s}  {t_status:20s}  {chain}")
        shown += 1
    print(f"\n{shown} rows shown (✓ = has visit date)")


def main():
    ap = argparse.ArgumentParser(description="Backfill missing RSA dates from TRUNO visit data")
    ap.add_argument("--write", action="store_true",
                    help="Write changes to the RSA Tracker (default: dry-run preview only)")
    ap.add_argument("--verbose", action="store_true",
                    help="Show debug output (which TRUNO rows matched and why)")
    ap.add_argument("--diagnose", metavar="KEYWORD", nargs="?", const="",
                    help="Dump raw TRUNO rows (optionally filtered by keyword, e.g. 'sprouts')")
    args = ap.parse_args()
    dry_run = not args.write

    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    if args.diagnose is not None:
        diagnose(keyword=args.diagnose or None)
        return

    if dry_run:
        print("🔍 DRY RUN — showing what would be filled. Pass --write to apply.\n")
    else:
        print("✏️  Writing missing dates to RSA Tracker…\n")

    if args.verbose:
        print("=== Verbose: Pass 1 (primary) matches ===")

    result = run(dry_run=dry_run, verbose=args.verbose)

    if args.verbose:
        print("=== Verbose: Pass 2 (fallback) attempts shown above ===\n")

    if result.get("error"):
        print(f"❌ Error: {result['error']}")
        sys.exit(1)

    n_filled  = result["filled"]
    n_skipped = result["skipped"]

    if n_filled == 0 and n_skipped == 0:
        print("✅ Nothing to backfill — all completed carts already have their dates.")
        return

    verb = "Would fill" if dry_run else "Filled"
    print(f"{verb} {n_filled} cart(s)   |   No TRUNO match: {n_skipped}\n")

    if result.get("details"):
        print("Carts updated:")
        for d in result["details"]:
            cols = ", ".join(d["wrote"])
            print(f"  {d['cartId']:50s}  {d['store']:25s}  {d['visit']}  →  {cols}")

    if result.get("noMatch"):
        print(f"\nNo TRUNO visit date found for {n_skipped} cart(s):")
        for nm in result["noMatch"]:
            print(f"  {nm['cartId']:50s}  {nm['store']:25s}  ({nm['status']})")
        print("\nTip: run with --verbose to see why each cart was skipped.")

    if dry_run and n_filled > 0:
        print(f"\nRun with --write to apply these {n_filled} change(s).")


if __name__ == "__main__":
    main()
