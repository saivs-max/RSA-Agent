#!/usr/bin/env python3
"""
READ-ONLY: trace exactly what a given TRUNO tracker ROW would match/do on the RSA
Tracker under the current reconcile logic. Writes NOTHING.

    python3 trace_truno.py 15        # trace sheet row 15 of the Truno tracker

Row numbers are 1-indexed sheet rows (row 1 = header). It uses the real reconcile
functions (resolve, Caper address validation, the all-identifiers existence check,
the store-number guard, latest-entry, status derivation), so what it prints is what
reconcile would actually do.
"""
import sys
import re

import rsa_sheets as S
import store_map as M
import rsa_reconcile as R


def main():
    rownum = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    tvals = S._ws(S.TRUNO_SPREADSHEET_ID, S.TRUNO_SHEET).get_all_values()
    if rownum < 1 or rownum > len(tvals):
        print(f"Row {rownum} is out of range — the Truno sheet has {len(tvals)} rows.")
        return
    row = tvals[rownum - 1]

    store  = S._cell(row, S.T_STORE)
    carts  = [c.strip() for c in re.split(r"[,/]+", S._cell(row, S.T_CART)) if c.strip()]
    addr   = S._cell(row, R.T_ADDRESS)
    visit  = S._cell(row, S.T_VISIT)
    status = S._cell(row, S.T_STATUS)
    result = S._cell(row, R.T_TEST_RESULT)

    print("=" * 90)
    print(f"  TRUNO ROW {rownum}")
    print("=" * 90)
    print(f"  store (col A) : {store!r}")
    print(f"  cart  (col C) : {carts}")
    print(f"  address(col E): {addr!r}")
    print(f"  visit (col F) : {visit}   status (col K): {status!r}   result (col H): {result!r}")
    if not store or not carts:
        print("\n  → blank store or no cart numbers → reconcile skips this row.")
        return

    # 1) canonical name via the store map (alias-based)
    canon = R._canon(store)
    print(f"\n  1) store_map.resolve → canonical name: {canon!r}"
          + ("  (no alias; unchanged)" if canon == store else "  (aliased)"))

    # 2) validate the address against the Caper deployment sheet (ADDRESS ONLY — no
    #    number fallback). Chain + number also come from the Truno code (col B).
    code = S._cell(row, R.T_TRUNO_CODE)
    code_chain, code_num = R._code_chain_num(code)
    by_addr, _ = M.caper_index()
    ak = M.addr_key(addr)
    recs = by_addr.get(ak, []) if ak else []
    text_num = R._store_num(canon)
    caper_num = (R._store_num(recs[0].get("number", "")) or R._store_num(recs[0].get("name", ""))) if recs else None
    store_nums = {n for n in (text_num, code_num, caper_num) if n}
    snum = text_num or code_num or caper_num             # primary, for naming/display
    print(f"\n  2) Caper validation by address   (addr_key = {ak or '—'}; Truno code {code or '—'}):")
    if recs:
        for c in recs:
            print(f"        ✓ Caper: {c.get('name')!r}  id={c.get('store_id')!r}  #{c.get('number')}  addr={c.get('address')!r}")
    else:
        print("        ✗ no Caper record for this address (chain/# taken from the Truno code)")
    caper_name = recs[0]["name"] if recs else ""
    cand_names = {canon, store} | {c.get("name") for c in recs}
    cand_ids   = {c.get("store_id") for c in recs}
    chain_word = (R._chain_word(recs[0]["store_id"]) if recs else "") or code_chain
    chain_toks = set(R._CHAIN_TOKENS.get(chain_word, [chain_word])) if chain_word else set()
    print(f"\n  3) identifiers checked against the RSA Tracker:")
    print(f"        names: {sorted(n for n in cand_names if n)}")
    print(f"        ids  : {sorted(i for i in cand_ids if i)}   candidate #s: {sorted(store_nums)}")
    print(f"        chain tokens: {sorted(chain_toks)}")
    wf_forms = sorted({f"{t.upper()} {snum}" for t in chain_toks} |
                      {f"{t.upper()}{snum}" for t in chain_toks} |
                      {f"{t.upper()}-{snum}" for t in chain_toks}) if snum else []
    if wf_forms:
        print(f"        store-#{snum} forms accepted (chain-precise): {wf_forms}")

    # latest-entry note
    same = [i + 1 for i, rr in enumerate(tvals[1:], start=1)
            if S._norm(R._canon(S._cell(rr, S.T_STORE))) == S._norm(canon)
            and set(c.strip().lstrip("0") for c in re.split(r"[,/]+", S._cell(rr, S.T_CART)) if c.strip())
                & set(c.lstrip("0") for c in carts)]
    if len(same) > 1:
        print(f"\n  ⚠ this store+cart also appears on Truno rows {same} — reconcile uses the LATEST by visit date for status.")

    passed = R._passed(result)
    rsa_status, overall = R._derive_status(passed, status)
    print(f"\n  4) status this row implies: {rsa_status}" + (f"  (overall {overall})" if overall else ""))

    rsa_rows = S._rsa_data()
    print(f"\n  5) per-cart outcome on the RSA Tracker:")
    for cn in carts:
        existing = R._existing_match(cand_names, cand_ids, chain_toks, store_nums, cn, rsa_rows)
        if existing:
            er, erow = S._find_row(rsa_rows, existing, canon)
            cur = S._cell(erow, S.C_RSASTATUS) if erow else "?"
            rstore = S._cell(erow, S.C_STORE) if erow else "?"
            adv = R._adv(R._RSA_RANK, cur, rsa_status)
            to = rsa_status if adv else cur
            print(f"        cart {cn}: ✅ MATCHES existing  →  {existing}")
            print(f"                   RSA store: {rstore!r}   current RSA status: {cur!r}")
            print(f"                   forward-only mirror would set: {cur!r} → {to!r}"
                  + ("" if adv else "  (no change — would not regress)"))
        else:
            add_store = R._net_new_name(canon, snum, chain_word, chain_toks, rsa_rows)
            serial = S.derive_cart_id(add_store, cn, rsa_rows)
            print(f"        cart {cn}: ➕ NET-NEW (no RSA match under any identifier)")
            print(f"                   would ADD {serial}  named {add_store!r}  status {rsa_status!r}")

    print("\n  (read-only — nothing was written)")
    print("=" * 90)


if __name__ == "__main__":
    main()
