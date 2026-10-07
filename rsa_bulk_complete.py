#!/usr/bin/env python3
"""
Bulk-mark the Winter Scales stale-RSA backlog as Completed RSA (Overall → Passed).

These are the carts from the RSA Scheduling Reminders that were serviced by Winter
Scales but never got their RSA status recorded. W&M is out of scope, so a completed RSA
now means the cart is fully done → Overall Status = Passed.

SAFE BY DEFAULT: this is a dry run unless you pass --apply. The dry run reads the RSA
Tracker and prints exactly which carts would change and which ids weren't found.

    python3 rsa_bulk_complete.py                 # dry run — preview only
    python3 rsa_bulk_complete.py --apply         # write to the RSA Tracker
    python3 rsa_bulk_complete.py --ids A,B,C      # override the built-in list
    python3 rsa_bulk_complete.py --status "Completed RSA" --overall "Passed"

Runs on the machine that has the service-account creds (sa-key.json) + sheet access.
"""
import argparse

import rsa_sheets as S

# The RSA-reminder carts only (the W&M-reminder carts are intentionally excluded).
RSA_BACKLOG_CARTS = [
    "P_WF_SHOPRITE_35",
    # WF 10 — ShopRite 496
    "P_WF_SHOPRITE_496_M3_001", "P_WF_SHOPRITE_496_M3_003", "P_WF_SHOPRITE_496_M3_005",
    "P_WF_SHOPRITE_496_M3_006", "P_WF_SHOPRITE_496_M3_007", "P_WF_SHOPRITE_496_M3_011",
    "P_WF_SHOPRITE_496_M3_012", "P_WF_SHOPRITE_496_M3_013", "P_WF_SHOPRITE_496_M3_014",
    # WF 42 — ShopRite of Greenwich (437)
    "P_WF_SHOPRITE_437_M3_034", "P_WF_SHOPRITE_437_M3_043", "P_WF_SHOPRITE_437_M3_045",
    "P_WF_SHOPRITE_437_M3_046", "P_WF_SHOPRITE_437_M3_047",
    # WF 43 — ShopRite 457
    "P_WF_SHOPRITE_457_M3_002", "P_WF_SHOPRITE_457_M3_004", "P_WF_SHOPRITE_457_M3_005",
    "P_WF_SHOPRITE_457_M3_006", "P_WF_SHOPRITE_457_M3_007", "P_WF_SHOPRITE_457_M3_010",
    "P_WF_SHOPRITE_457_M3_011", "P_WF_SHOPRITE_457_M3_012", "P_WF_SHOPRITE_457_M3_014",
    # WF 52 — ShopRite of Elmwood Park (154)
    "P_WF_SHOPRITE_154_M3_013",
    # WF 59 — Montgomery ShopRite (239)
    "P_WF_SHOPRITE_239_M3_006", "P_WF_SHOPRITE_239_M3_018", "P_WF_SHOPRITE_239_M3_024",
    # WF 70 — ShopRite 529 + ShopRite of Evesham (both store 529)
    "P_WF_SHOPRITE_529_M3_004", "P_WF_SHOPRITE_529_M3_005", "P_WF_SHOPRITE_529_M3_006",
    "P_WF_SHOPRITE_529_M3_008", "P_WF_SHOPRITE_529_M3_012",
    "P_WF_SHOPRITE_529_M3_001", "P_WF_SHOPRITE_529_M3_002", "P_WF_SHOPRITE_529_M3_003",
    "P_WF_SHOPRITE_529_M3_007", "P_WF_SHOPRITE_529_M3_009", "P_WF_SHOPRITE_529_M3_011",
    "P_WF_SHOPRITE_529_M3_013", "P_WF_SHOPRITE_529_M3_014", "P_WF_SHOPRITE_529_M3_015",
    "P_WF_SHOPRITE_529_M3_016", "P_WF_SHOPRITE_529_M3_020",
    # WF 73 — ShopRite 472
    "P_WF_SHOPRITE_472_M3_003", "P_WF_SHOPRITE_472_M3_004", "P_WF_SHOPRITE_472_M3_005",
    "P_WF_SHOPRITE_472_M3_006", "P_WF_SHOPRITE_472_M3_008", "P_WF_SHOPRITE_472_M3_009",
    "P_WF_SHOPRITE_472_M3_011", "P_WF_SHOPRITE_472_M3_012", "P_WF_SHOPRITE_472_M3_013",
    "P_WF_SHOPRITE_472_M3_014", "P_WF_SHOPRITE_472_M3_015",
    # WF 73 — ShopRite of Watchung (473)
    "P_WF_SHOPRITE_473_M3_001", "P_WF_SHOPRITE_473_M3_002", "P_WF_SHOPRITE_473_M3_010",
    "P_WF_SHOPRITE_473_M3_019", "P_WF_SHOPRITE_473_M3_022", "P_WF_SHOPRITE_473_M3_027",
    "P_WF_SHOPRITE_473_M3_029", "P_WF_SHOPRITE_473_M3_034", "P_WF_SHOPRITE_473_M3_039",
    "P_WF_SHOPRITE_473_M3_040",
    # WF 51 — ShopRite 151  (missed in the first backlog pass)
    "P_WF_SHOPRITE_151_M3_003", "P_WF_SHOPRITE_151_M3_014",
    # WF 44 — ShopRite 497
    "P_WF_SHOPRITE_497_M3_032",
]

# Carts from the W&M Scheduling Reminders. W&M is out of scope, so these are marked
# Passed AND their W&M Status is set to a terminal value (--wm-status) so the legacy
# W&M-keyed reminder — which looks at the W&M column, not RSA/Overall — stops firing.
WM_REMINDER_CARTS = [
    # WF 8 — Brookdale ShopRite (176)
    "P_WF_SHOPRITE_176_M3_003", "P_WF_SHOPRITE_176_M3_006", "P_WF_SHOPRITE_176_M3_020",
    # WF 21 — ShopRite 136
    "P_WF_SHOPRITE_136_M3_005",
    # WF 31 — ShopRite 163
    "P_WF_SHOPRITE_163_M3_002", "P_WF_SHOPRITE_163_M3_003", "P_WF_SHOPRITE_163_M3_005",
    "P_WF_SHOPRITE_163_M3_013", "P_WF_SHOPRITE_163_M3_018", "P_WF_SHOPRITE_163_M3_020",
    "P_WF_SHOPRITE_163_M3_022",
    # WF 42 — ShopRite 437
    "P_WF_SHOPRITE_437_M3_023",
    # WF 49 — ShopRite 239
    "P_WF_SHOPRITE_239_M3_003", "P_WF_SHOPRITE_239_M3_004", "P_WF_SHOPRITE_239_M3_015",
    "P_WF_SHOPRITE_239_M3_020",
    # WF 51 — ShopRite 151
    "P_WF_SHOPRITE_151_M3_007",
    # WF 63 — ShopRite 297
    "P_WF_SHOPRITE_297_M3_001", "P_WF_SHOPRITE_297_M3_002", "P_WF_SHOPRITE_297_M3_003",
    "P_WF_SHOPRITE_297_M3_004", "P_WF_SHOPRITE_297_M3_007", "P_WF_SHOPRITE_297_M3_008",
    "P_WF_SHOPRITE_297_M3_009", "P_WF_SHOPRITE_297_M3_010", "P_WF_SHOPRITE_297_M3_011",
    "P_WF_SHOPRITE_297_M3_012", "P_WF_SHOPRITE_297_M3_013", "P_WF_SHOPRITE_297_M3_015",
    "P_WF_SHOPRITE_297_M3_016", "P_WF_SHOPRITE_297_M3_017", "P_WF_SHOPRITE_297_M3_018",
    "P_WF_SHOPRITE_297_M3_019", "P_WF_SHOPRITE_297_M3_020", "P_WF_SHOPRITE_297_M3_021",
    "P_WF_SHOPRITE_297_M3_022", "P_WF_SHOPRITE_297_M3_023", "P_WF_SHOPRITE_297_M3_024",
    "P_WF_SHOPRITE_297_M3_025", "P_WF_SHOPRITE_297_M3_026", "P_WF_SHOPRITE_297_M3_027",
    "P_WF_SHOPRITE_297_M3_028", "P_WF_SHOPRITE_297_M3_029", "P_WF_SHOPRITE_297_M3_030",
    # WF 66 — ShopRite 487
    "P_WF_SHOPRITE_487_M3_002", "P_WF_SHOPRITE_487_M3_033", "P_WF_SHOPRITE_487_M3_034",
    # WF 69 — ShopRite 134
    "P_WF_SHOPRITE_134_M3_014", "P_WF_SHOPRITE_134_M3_019",
    # WF 73 — ShopRite 472
    "P_WF_SHOPRITE_472_M3_007", "P_WF_SHOPRITE_472_M3_024", "P_WF_SHOPRITE_472_M3_026",
    "P_WF_SHOPRITE_472_M3_032",
]


def main():
    ap = argparse.ArgumentParser(description="Bulk-mark stale RSA carts Completed RSA (Overall Passed).")
    ap.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    ap.add_argument("--ids", help="comma-separated cart ids to use instead of the built-in backlog")
    ap.add_argument("--status", default="Completed RSA", help="RSA Status to set")
    ap.add_argument("--overall", default="Passed", help="Overall Status to set (W&M is out of scope)")
    ap.add_argument("--note", default="RSA completed via Winter Scales — backlog reconciled.",
                    help="dated note appended to each cart")
    ap.add_argument("--min-days", type=float, default=13,
                    help="only update carts with NO update in more than this many days "
                         "(default 13; use 0 to update regardless of staleness)")
    ap.add_argument("--wm", action="store_true",
                    help="process the W&M-reminder carts and also set the W&M Status column "
                         "(so the legacy W&M-keyed reminders stop)")
    ap.add_argument("--wm-status", default="Passed W&M",
                    help="value written to the W&M Status column in --wm mode (default 'Passed W&M')")
    ap.add_argument("--normalize", action="store_true",
                    help="apply the standing tracker rules to EVERY row: Overall=Passed where "
                         "RSA=Completed RSA, and DRI=CSM where W&M=Pending W&M")
    args = ap.parse_args()

    if args.normalize:
        res = S.normalize_tracker(dry_run=not args.apply)
        if res.get("error"):
            print("ERROR:", res["error"]); return
        print(f"Overall → Passed: {res['overallPassed']}   ·   DRI → CSM: {res['driCsm']}   "
              f"·   rows changed: {res['changedRows']}")
        print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}." +
              ("" if args.apply else "  Re-run with --apply to write these changes."))
        return

    if args.ids:
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    else:
        ids = WM_REMINDER_CARTS if args.wm else RSA_BACKLOG_CARTS
    wm_status = args.wm_status if args.wm else None
    threshold = args.min_days if args.min_days and args.min_days > 0 else None
    gate = f" · only if stale > {args.min_days:g} days" if threshold is not None else ""
    wm_note = f", W&M = '{wm_status}'" if wm_status else ""
    print(f"{len(ids)} cart(s) → RSA Status = '{args.status}', Overall = '{args.overall}'{wm_note}{gate}\n")

    res = S.bulk_set_rsa_status(ids, rsaStatus=args.status, overallStatus=args.overall,
                                wmStatus=wm_status, note=args.note, submittedBy="Ops (bulk)",
                                dry_run=not args.apply, min_stale_days=threshold)
    if res.get("error"):
        print("ERROR:", res["error"]); return

    for f in res["found"]:
        d = f.get("days")
        print(f"  ✓ {f['cartId']:<28} ({f['store']}) · {d:g}d" if isinstance(d, (int, float))
              else f"  ✓ {f['cartId']:<28} ({f['store']})")
    if res.get("skippedRecent"):
        print("\n  ⏭  skipped — not stale enough (≤ %g days) or staleness unknown:" % args.min_days)
        for s in res["skippedRecent"]:
            print(f"    • {s['cartId']} ({s['store']}) · {s['days']}")
    if res["missing"]:
        print("\n  ⚠ not found in the tracker (skipped):")
        for m in res["missing"]:
            print(f"    • {m}")

    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'} — {res['count']} to update, "
          f"{len(res.get('skippedRecent', []))} recent/unknown, {len(res['missing'])} missing.")
    if not args.apply:
        print("Re-run with --apply to write these changes to the RSA Tracker.")


if __name__ == "__main__":
    main()
