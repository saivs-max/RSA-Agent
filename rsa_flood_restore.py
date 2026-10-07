#!/usr/bin/env python3
"""
rsa_flood_restore.py — undo the carts wrongly reset to "Pending RSA" by the Truno-monitor
re-fire flood (June 2026).

Why this is safe: the cancellation handler only ever changed three cells — RSA Status (J)
→ "Pending RSA", cleared RSA Scheduled Date (H), and stamped Last Updated (R). It left
W&M Status (M), Overall (O), RSA Completion Date (K) and RSA Test Results (U) UNTOUCHED.
So a cart that now reads "Pending RSA" but still shows RSA-was-completed evidence
(Overall = Pending W&M / Passed, OR a W&M status, OR a completion date, OR a Pass/Fail
test result) was demonstrably knocked back — we restore its RSA Status from that evidence.

Carts that merely lost a *scheduled* date (no completion evidence) can't be restored from
the RSA sheet alone — their date lives only in Truno. Those are listed for review; re-run
`rsa_reconcile.py` (mirror) to re-derive Scheduled RSA + date from the current Truno rows.

Usage (run from the project folder, with .env + sa-key.json):
    python3 rsa_flood_restore.py                 # DRY RUN — shows what it would change
    python3 rsa_flood_restore.py --commit        # apply the high-confidence restores
    python3 rsa_flood_restore.py --date=06/23/2026   # only carts last-updated that day (default: today)
    python3 rsa_flood_restore.py --all           # ignore the Last-Updated date filter
"""
import sys
import gspread
import rsa_sheets as S


def build_plan(rows, target_date, all_dates):
    """Return (restore, review) given RSA data rows. Pure → unit-testable offline."""
    restore, review = [], []
    for i, r in enumerate(rows):
        cid = S._cell(r, S.C_CARTID)
        if not cid:
            continue
        if S._cell(r, S.C_RSASTATUS) != "Pending RSA":          # only carts now at Pending RSA
            continue
        last = S._cell(r, S.C_LASTUPD)
        if not all_dates and last != target_date:               # scope to the flood day by default
            continue
        store   = S._cell(r, S.C_STORE)
        overall = S._cell(r, S.C_OVERALL)
        wm      = S._cell(r, S.C_WMSTATUS)
        compl   = S._cell(r, S.C_RSACOMPL)
        result  = S._cell(r, S.C_RSA_RESULTS)
        sheet_row = S.RSA_DATA_START + i
        rlow = result.lower()
        # RSA-was-completed evidence (any one is sufficient; all are intact post-flood):
        completed = (overall in ("Pending W&M", "Passed")
                     or wm in ("Scheduled W&M", "Passed W&M", "Completed W&M", "Failed W&M")
                     or bool(compl)
                     or "pass" in rlow)
        if completed:
            to = "Failed RSA" if ("fail" in rlow and "pass" not in rlow) else "Completed RSA"
            restore.append({"row": sheet_row, "cartId": cid, "store": store,
                            "from": "Pending RSA", "to": to, "overall": overall,
                            "wm": wm, "compl": compl, "setCompl": (to == "Completed RSA" and not compl)})
        else:
            review.append({"row": sheet_row, "cartId": cid, "store": store, "lastUpd": last})
    return restore, review


def main():
    commit    = "--commit" in sys.argv
    all_dates = "--all" in sys.argv
    target = S._today()
    for a in sys.argv:
        if a.startswith("--date="):
            target = a.split("=", 1)[1].strip()

    ws = S._ws(S.RSA_SPREADSHEET_ID, S.RSA_SHEET)
    rows = ws.get_all_values()[S.RSA_DATA_START - 1:]
    restore, review = build_plan(rows, target, all_dates)

    scope = "all dates" if all_dates else f"Last-Updated = {target}"
    print("=" * 88)
    print(f"  RSA flood restore   ({'COMMIT' if commit else 'DRY RUN — no writes'})   ·   scope: {scope}")
    print("=" * 88)

    print(f"\n  RESTORE (high-confidence — RSA was completed but got reset): {len(restore)} cart(s)")
    for e in restore:
        why = e["overall"] or e["wm"] or ("completed " + e["compl"] if e["compl"] else "pass result")
        print(f"    ↺ {e['cartId']:<34} {e['store'][:26]:<26} Pending RSA → {e['to']:<13} (evidence: {why})")

    print(f"\n  REVIEW (lost a scheduled date — restore via Truno/reconcile, not here): {len(review)} cart(s)")
    for e in review:
        print(f"    ? {e['cartId']:<34} {e['store'][:26]:<26} now Pending RSA, updated {e['lastUpd']}")
    if review:
        print("      → run `python3 rsa_reconcile.py --commit` to re-derive Scheduled RSA + date from Truno,")
        print("        or set these manually. (Their scheduled date was cleared and lives only in Truno.)")

    if not commit:
        print(f"\n  Dry run only. Re-run with --commit to apply the {len(restore)} restore(s).")
        return
    if not restore:
        print("\n  Nothing to restore.")
        return

    cells = []
    note = f"[Restored after Truno-monitor flood reset {S._today()}]"
    for e in restore:
        cells.append(gspread.Cell(e["row"], S.C_RSASTATUS, e["to"]))
        if e["setCompl"]:
            cells.append(gspread.Cell(e["row"], S.C_RSACOMPL, S._today()))
        cells.append(gspread.Cell(e["row"], S.C_LASTUPD, S._today()))
        existing = S._cell(rows[e["row"] - S.RSA_DATA_START], S.C_NOTES)
        cells.append(gspread.Cell(e["row"], S.C_NOTES, f"{existing}\n{note}" if existing else note))
    ws.update_cells(cells)
    print(f"\n  ✅ Restored {len(restore)} cart(s). W&M / Overall / results were untouched by the flood and are unchanged.")


if __name__ == "__main__":
    main()
