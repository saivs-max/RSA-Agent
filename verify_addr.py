#!/usr/bin/env python3
"""
READ-ONLY pre-flight: how well do the Truno tracker addresses line up with the
Caper deployment sheet (caper_stores.csv) and the RSA Tracker?

Writes NOTHING (no sheet, no store_map.json) — safe to run against production before
any reconcile/undo. Use it to confirm the address keys match before committing.

    python3 verify_addr.py
"""
import collections
import store_map as M


def main():
    caper = M.load_caper()
    if not caper:
        print(f"⚠  No Caper data — put caper_stores.csv in this folder (or set CAPER_SHEET_ID).")
        return
    print(f"Caper rows: {len(caper)}   (with address key: "
          f"{sum(1 for c in caper if M.addr_key(c['address']))})\n")

    rows = M.build(caper=caper)              # reads live Truno + RSA; no writes
    c = collections.Counter(e["status"] for e in rows)
    addr_hits = sum(1 for e in rows if e["basis"] == "address")
    num_hits  = sum(1 for e in rows if e["basis"] == "number")

    print("=" * 84)
    print(f"  Truno stores: {len(rows)}   |   matched by ADDRESS: {addr_hits}   by number (fallback): {num_hits}")
    print("=" * 84)
    for k in ("matched", "matched_no_rsa", "ambiguous", "rsa_ambiguous", "no_caper_match", "no_address"):
        label = {"matched": "matched → existing RSA store",
                 "matched_no_rsa": "matched Caper, NET-NEW (not in RSA yet)",
                 "ambiguous": "address hits several Caper stores",
                 "rsa_ambiguous": "matches several RSA stores",
                 "no_caper_match": "address not found in Caper sheet",
                 "no_address": "no address on the Truno row"}[k]
        print(f"  {c.get(k, 0):>4}  {label}")

    flagged = [e for e in rows if e["status"] in ("no_caper_match", "no_address", "ambiguous")]
    if flagged:
        print("\n  ── Stores that DON'T cleanly map (need a look / a store_map.json alias) ──")
        for e in flagged:
            print(f"    {(e['trunoIC'] or e['trunoCode'])[:34]:<34} "
                  f"addr={(e['trunoAddr'] or '(blank)')[:38]:<38} key={M.addr_key(e['trunoAddr']) or '—'}")

    netnew = [e for e in rows if e["status"] == "matched_no_rsa"]
    if netnew:
        print(f"\n  ── NET-NEW (would be added, named from Caper) — {len(netnew)} ──")
        for e in netnew[:20]:
            print(f"    {(e['trunoIC'] or '?')[:30]:<30} → {e['caper']}")
        if len(netnew) > 20:
            print(f"    …and {len(netnew) - 20} more")

    print("\n  ── Sample of MATCHED → existing RSA (spot-check these look right) ──")
    shown = 0
    for e in rows:
        if e["status"] == "matched" and shown < 12:
            print(f"    {e['trunoIC'][:30]:<30} → {e['rsa']}   [{e['basis']}]")
            shown += 1
    print("\nRead-only — nothing was written. Review the above before running reconcile/undo.")


if __name__ == "__main__":
    main()
