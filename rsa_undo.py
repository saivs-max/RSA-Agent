#!/usr/bin/env python3
"""
Undo the Truno backfill — delete every RSA Tracker row tagged
"Backfilled from Truno tracker" (the note rsa_reconcile.py stamps on each row it adds).

PRODUCTION-SAFE: only rows carrying that exact note are removed. Anything that
pre-existed, was added by the Slack bot ("Added via RSA Agent bot"), or entered by a
person is left untouched. Preview by default; --commit performs the deletion.

    python3 rsa_undo.py            # list exactly what would be removed
    python3 rsa_undo.py --commit   # remove those rows

Rows are deleted bottom-up in contiguous blocks (fewest API calls, indices stay valid),
with 429 back-off. After it runs, open the dashboard and Refresh.
"""
import time
import argparse

import gspread
import rsa_sheets as S

TAG = "Backfilled from Truno tracker"


def _retry(fn, what="Sheets call", tries=4, wait=30):
    for i in range(tries):
        try:
            return fn()
        except gspread.exceptions.APIError as e:
            if "429" in str(e) and i < tries - 1:
                print(f"  …rate limited on {what}; waiting {wait}s (try {i+1}/{tries})")
                time.sleep(wait)
                continue
            raise


def _runs(rows_sorted_asc):
    """Group ascending 1-indexed rows into contiguous (start, end) inclusive blocks."""
    runs = []
    for r in rows_sorted_asc:
        if runs and r == runs[-1][1] + 1:
            runs[-1][1] = r
        else:
            runs.append([r, r])
    return [tuple(x) for x in runs]


def find_backfilled(ws=None):
    ws = ws or S._ws(S.RSA_SPREADSHEET_ID, S.RSA_SHEET)
    vals = _retry(lambda: ws.get_all_values(), "read RSA Tracker")
    out = []
    for i, r in enumerate(vals):
        if i < S.RSA_DATA_START - 1:                 # skip header rows
            continue
        if S._cell(r, S.C_NOTES).strip().startswith(TAG):
            out.append({"row": i + 1, "serial": S._cell(r, S.C_CARTID),
                        "store": S._cell(r, S.C_STORE), "rsaStatus": S._cell(r, S.C_RSASTATUS)})
    return ws, out


def undo(commit=False):
    ws, rows = find_backfilled()
    if commit and rows:
        # Delete bottom-up so earlier rows keep their indices.
        for start, end in sorted(_runs(sorted(e["row"] for e in rows)), reverse=True):
            _retry(lambda s=start, e=end: ws.delete_rows(s, e), "delete rows")
            time.sleep(1)
    return rows


def main():
    ap = argparse.ArgumentParser(description="Undo the Truno backfill (delete tagged rows)")
    ap.add_argument("--commit", action="store_true", help="actually delete (default is a preview)")
    args = ap.parse_args()

    _, rows = find_backfilled()
    print("=" * 84)
    print(f"  UNDO Truno backfill — {len(rows)} row(s) tagged '{TAG}'"
          f"  {'(DELETING)' if args.commit else '(preview)'}")
    print("=" * 84)
    by_store = {}
    for e in rows:
        by_store.setdefault(e["store"], []).append(e)
    for store, items in sorted(by_store.items()):
        print(f"  {store or '(blank store)'}  —  {len(items)} cart(s)")
        for e in items:
            print(f"      row {e['row']:<5} {e['serial']:<36} {e['rsaStatus']}")

    if not rows:
        print("  Nothing tagged as backfilled — nothing to undo.")
        print("=" * 84)
        return

    if not args.commit:
        print("\n  Preview only — no changes made. Re-run with  --commit  to delete these rows.")
        print("=" * 84)
        return

    deleted = undo(commit=True)
    print(f"\n  ✅ Deleted {len(deleted)} backfilled row(s). Pre-existing / bot / manual rows untouched.")
    print("  Open the dashboard and hit Refresh to confirm.")
    print("=" * 84)


if __name__ == "__main__":
    main()
