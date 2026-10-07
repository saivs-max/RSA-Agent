#!/usr/bin/env python3
"""
Reconcile the TRUNO tracker into the RSA Tracker.

Goal: every cart on the Truno tracker should exist in the RSA Tracker — with its
status and test result copied over — and NO duplicates.

How duplicates are prevented (three layers):
  1. match_cart(store, cart#) — finds an existing serial by the store#+cart# pattern
     already in the tracker, so a cart that's there under its real serial is skipped.
  2. A per-run "seen" set — a serial planned/added once this run is never repeated,
     even if the same cart appears in several Truno rows.
  3. add_cart's own exact-serial guard + a re-read of the tracker after every add,
     so the next match sees what was just written. Re-running is idempotent.

Run from the project folder (needs sa-key.json + deps + network):
    python3 rsa_reconcile.py                      # PREVIEW only (no writes)
    python3 rsa_reconcile.py --commit             # add the missing carts
    python3 rsa_reconcile.py --commit --update-existing   # also fill results onto carts already present (only where blank)

The dashboard updates automatically once carts land in the RSA Tracker (just Refresh it).
"""
import re
import time
import argparse
import gspread
import rsa_sheets as S

try:
    import store_map as _sm
except Exception:
    _sm = None


def _canon(store):
    """Resolve a Truno store to its canonical name via the address-based store map:
      • a store that matches an existing RSA store → that RSA name  (so it DEDUPES)
      • a net-new store → its Caper deployment name (so it's added under that name)
    Falls back to the Truno store text if no map/alias is available."""
    if not _sm:
        return store
    try:
        return _sm.resolve(store) or store
    except Exception:
        return store

T_TEST_RESULT = 8          # Truno col H — "Test Results"
T_ADDRESS     = 5          # Truno col E — "Address" (the join key to the Caper sheet)
T_TRUNO_CODE  = 2          # Truno col B — store code, e.g. "ISC0050-WKF-0116" (chain + #)
DEFAULT_STATE = "NJ"       # used only when the store's state can't be inferred


# ── API-efficient helpers (2 reads total + batched writes, with 429 backoff) ────
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


def _batch_write(ws, cells):
    """Write all cells in as few requests as possible (chunked defensively)."""
    CHUNK = 5000
    for i in range(0, len(cells), CHUNK):
        part = cells[i:i + CHUNK]
        _retry(lambda p=part: ws.update_cells(p), "write")


def _read_truno():
    vals = _retry(lambda: S._ws(S.TRUNO_SPREADSHEET_ID, S.TRUNO_SHEET).get_all_values(), "read Truno")
    out = []
    for r in vals[1:]:                                   # skip header row
        store = S._cell(r, S.T_STORE)
        carts = [c.strip() for c in re.split(r"[,/]+", S._cell(r, S.T_CART)) if c.strip()]
        if not store or not carts:
            continue
        out.append({"store": store, "carts": carts, "visit": S._cell(r, S.T_VISIT),
                    "result": S._cell(r, T_TEST_RESULT), "status": S._cell(r, S.T_STATUS),
                    "address": S._cell(r, T_ADDRESS), "code": S._cell(r, T_TRUNO_CODE)})
    return out


def _existing_match(names, ids, chain_toks, store_nums, cart, rsa_rows):
    """Find an existing RSA cart for this store+cart, checking EVERY candidate
    identifier — the Caper store name(s), the Caper store id(s), the resolved/Truno
    names, and the store NUMBER under a matching chain — before we conclude it's net-new.
    store_nums is the SET of candidate numbers (Truno code, Caper, and text) because the
    three sheets disagree (e.g. Weis Selinsgrove is #226 in Truno/RSA but #1 in Caper);
    a match on ANY of them counts. Kept EXACT (561 never matches 5610) and CHAIN-PRECISE
    (a "Sprouts 78" never matches a Wakefern #78)."""
    want = str(cart).strip().lstrip("0") or "0"
    snset = {str(n).lstrip("0") or "0" for n in (store_nums or set()) if n}
    for nm in names:
        if not nm:
            continue
        ser = S.match_cart(nm, cart, rsa_rows)
        if ser:
            got = re.search(r"_0*(\d+)_M3_", ser)
            if snset and got and (got.group(1).lstrip("0") or got.group(1)) not in snset:
                continue                       # matched a different store's serial
            return ser
    # Caper store-ids are distinctive (e.g. "prod-wakefern-50"); scan the serial,
    # store text and notes for them, with the cart number required to match.
    nids = [S._norm(i) for i in ids if i and len(S._norm(i)) >= 5]
    if nids:
        for r in rsa_rows:
            cid = S._cell(r, S.C_CARTID)
            if not cid:
                continue
            cm = re.search(r"_M3_0*(\d+)", cid)
            if not cm or (cm.group(1).lstrip("0") or cm.group(1)) != want:
                continue
            blob = S._norm(cid + " " + S._cell(r, S.C_STORE) + " " + S._cell(r, S.C_NOTES))
            if any(nid in blob for nid in nids):
                return cid
    # Chain-aware EXACT store-number match — the "WF 78" / "WF78" / "WF-78" / "ShopRite
    # 78" / serial _78_ case. The store number must match EXACTLY (561 ≠ 5610) and, when
    # the chain is known, a chain token must appear in the row (so "Sprouts 78" is never
    # mistaken for a Wakefern #78). Cart number must also match.
    ct = {S._norm(t) for t in chain_toks if t and len(S._norm(t)) >= 2}
    if snset:
        for r in rsa_rows:
            cid = S._cell(r, S.C_CARTID)
            if not cid:
                continue
            cm = re.search(r"_M3_0*(\d+)", cid)
            if not cm or (cm.group(1).lstrip("0") or cm.group(1)) != want:
                continue
            rstore = S._cell(r, S.C_STORE)
            snums = set(S._store_nums(rstore))
            sm = re.search(r"_0*(\d+)_M3_", cid)
            if sm:
                snums.add(sm.group(1).lstrip("0") or sm.group(1))
            if not (snset & snums):
                continue
            blob = S._norm(cid + " " + rstore)
            if not ct or any(t in blob for t in ct):
                return cid
    return None


def _new_row_cells(r, e, today):
    """gspread cells for a brand-new RSA row (mirrors add_cart + results)."""
    vals = {
        S.C_CARTID: e["serial"], S.C_STORE: e["store"], S.C_STATE: (e["state"] or "").upper(),
        S.C_SVCDATE: today, S.C_RSAREQ: "Yes", S.C_RSAVENDOR: "TRUNO",
        S.C_RSASTATUS: e["rsaStatus"], S.C_WMREQ: "Yes", S.C_WMSTATUS: "Pending W&M",
        S.C_OVERALL: e["overall"] or "Pending RSA", S.C_DRI: "HW Ops",
        S.C_LASTUPD: today, S.C_NOTES: "Backfilled from Truno tracker",
    }
    if e["passed"] is True:
        vals[S.C_RSACOMPL] = today
    if e["rsaStatus"] == "Scheduled RSA" and e["visit"]:
        vals[S.C_RSASCHED] = e["visit"]
    if e["result"]:
        vals[S.C_RSA_RESULTS] = e["result"]
    return [gspread.Cell(r, c, v) for c, v in vals.items() if v != ""]


def _passed(result):
    t = (result or "").lower()
    if "pass" in t:
        return True
    if "fail" in t:
        return False
    return None


_US_STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
              "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
              "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
              "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"}


def _state_from_addr(addr):
    """The 2-letter US state from an address — the token right before the ZIP, e.g.
    '… Las Vegas, NV 89117' → 'NV'. Returns None if not confidently found."""
    a = str(addr or "")
    m = re.search(r"\b([A-Za-z]{2})\s*,?\s+\d{5}(?:-\d{4})?\b", a)   # state right before ZIP
    if m and m.group(1).upper() in _US_STATES:
        return m.group(1).upper()
    cand = None
    for m in re.finditer(r"[,\s]([A-Za-z]{2})\b", a):                # fallback: last ", ST" token
        if m.group(1).upper() in _US_STATES:
            cand = m.group(1).upper()
    return cand


def _state_for(store, smap):
    return smap.get(S._norm(store), "")        # blank when unknown — never guess a default


def _state_map(rsa_rows):
    """store-name (normalized) -> state, from carts already in the RSA Tracker."""
    m = {}
    for r in rsa_rows:
        store = S._norm(S._cell(r, S.C_STORE))
        st = S._cell(r, S.C_STATE)
        if store and st and store not in m:
            m[store] = st
    return m


# Forward order of statuses, so a mirror only ADVANCES a cart and never regresses it.
_RSA_RANK = {"pending truno scheduled": 0, "pending rsa": 0, "scheduled rsa": 1,
             "completed rsa": 2, "failed rsa": 2, "passed rsa": 3}
_OVERALL_RANK = {"pending rsa": 0, "pending w&m": 1, "scheduled w&m": 1,
                 "needs verification": 1, "failed": 2, "redispatch needed": 2, "passed": 3}


def _adv(rank_map, cur, new):
    """True if 'new' status is an advance over (or equal to) 'cur' — so the mirror never
    moves a more-complete cart backward. Unknown 'new' never writes; unknown 'cur' yields."""
    nr = rank_map.get((new or "").strip().lower())
    if nr is None:
        return False
    cr = rank_map.get((cur or "").strip().lower())
    return True if cr is None else nr >= cr


def _derive_status(passed, status):
    """Return (rsaStatus, overallStatus) for a Truno row. The overall (col O) is what
    the dashboard surfaces. W&M is out of scope, so a passed/done RSA is fully done →
    Overall = Passed; failed → Failed; scheduled/pending → Pending RSA."""
    s = (status or "").lower()
    if passed is True:
        return ("Completed RSA", "Passed")
    if passed is False:
        return ("Failed RSA", "Failed")
    if "schedul" in s:
        return ("Scheduled RSA", "Pending RSA")
    if "complet" in s:
        return ("Completed RSA", "Passed")         # RSA done → fully Passed (no W&M step)
    return (S.RSA_STATUS_NEW, "Pending RSA")       # "Pending Truno Scheduled"


def _store_num(store):
    """Best store number from a store string (longest digit run)."""
    nums = S._store_nums(store)
    return (sorted(nums, key=lambda x: (-len(x), x))[0].lstrip("0") or None) if nums else None


# Caper chain (from the store id) → tokens the RSA Tracker may use in a store label or
# serial. Keeps a store-number match CHAIN-PRECISE: "WF 78" matches a Wakefern/ShopRite
# row but never a "Sprouts 78".
_CHAIN_TOKENS = {"wakefern": ["wakefern", "wf", "wkf", "shoprite", "freshgrocer"],
                 "sprouts": ["sprouts", "sfm", "spr"], "schnucks": ["schnucks", "sch"],
                 "kroger": ["kroger", "kr", "krg", "qfc"], "weis": ["weis", "wei"],
                 "gelson": ["gelson", "gfh"], "davis": ["davis", "dfd"], "mckeever": ["mckeever", "mck"],
                 "geisslers": ["geisslers", "gei"], "soelbergs": ["soelbergs", "soe"], "clarks": ["clarks", "cla"]}
_ID_SKIP = {"prod", "qvs", "dev", "staging", "test", "isc", "stage"}

# chain word → the banner the RSA Tracker / Caper sheet uses, for CLEAN net-new names.
_CHAIN_BANNER = {"wakefern": "ShopRite", "sprouts": "Sprouts", "schnucks": "Schnucks",
                 "geisslers": "Geisslers", "kroger": "Kroger", "mckeever": "McKeevers",
                 "soelbergs": "Soelbergs", "davis": "Davis", "weis": "Weis",
                 "gelson": "Gelsons", "clarks": "Clarks"}
# Truno store-code chain (col B, e.g. ISC0050-WKF-0116) → chain word.
_TRUNO_CODE_CHAIN = {"WKF": "wakefern", "SFM": "sprouts", "SCH": "schnucks", "GEI": "geisslers",
                     "KRO": "kroger", "MCK": "mckeever", "SOE": "soelbergs", "DAV": "davis",
                     "WEI": "weis", "GFH": "gelson", "CLA": "clarks"}


def _chain_word(store_id):
    """Leading chain word of a Caper store id, e.g. 'prod-wakefern-78' → 'wakefern'."""
    parts = [p for p in re.split(r"[-_\s]+", str(store_id or "").lower())
             if p and p not in _ID_SKIP and not p.isdigit()]
    return parts[0] if parts else ""


def _chain_tokens(store_id):
    """Chain tokens from a Caper store id, e.g. 'prod-wakefern-78' →
    {wakefern, wf, wkf, shoprite, freshgrocer}; 'prod-newco-42' → {newco}."""
    cw = _chain_word(store_id)
    return set(_CHAIN_TOKENS.get(cw, [cw])) if cw else set()


def _code_chain_num(code):
    """Truno store code (col B) → (chain word, store number). 'ISC0050-WKF-0116' →
    ('wakefern','116'). Independent of the dirty Caper extraction."""
    if not _sm or not code:
        return ("", "")
    try:
        ch, num = _sm.parse_truno_code(code)
    except Exception:
        return ("", "")
    return (_TRUNO_CODE_CHAIN.get((ch or "").upper(), ""), (num or "").lstrip("0"))


def _clean_truno_name(s):
    """Strip a chain prefix like 'WF- ', 'KRO- ', 'WF 80 - ' and collapse spaces."""
    s = re.sub(r"^[A-Za-z]{2,4}\s*\d*\s*-\s*", "", str(s or "").strip())
    return re.sub(r"\s+", " ", s).strip()


def _sibling_store_name(store_num, chain_toks, rsa_rows):
    """An existing RSA store label for the same store (number + chain), so a NEW cart at
    a store already in the tracker inherits that store's name instead of a fresh one."""
    if not store_num:
        return None
    ct = {S._norm(t) for t in chain_toks if t and len(S._norm(t)) >= 2}
    for r in rsa_rows:
        st = S._cell(r, S.C_STORE)
        if store_num in set(S._store_nums(st)):
            blob = S._norm(S._cell(r, S.C_CARTID) + " " + st)
            if not ct or any(t in blob for t in ct):
                return st
    return None


def _net_new_name(store_text, store_num, chain_word, chain_toks, rsa_rows):
    """Clean name for a net-new cart: reuse an existing same-store label if present, else
    Banner + number ('ShopRite 116', 'Sprouts 558'), else the cleaned Truno name. NEVER
    the raw (dirty) Caper text."""
    sib = _sibling_store_name(store_num, chain_toks, rsa_rows)
    if sib:
        return sib
    banner = _CHAIN_BANNER.get(chain_word)
    if banner and store_num:
        return f"{banner} {store_num}"
    return _clean_truno_name(store_text) or store_text


def _visit_key(v):
    """Sortable key for a visit date so we can pick the LATEST Truno row."""
    v = (v or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return time.strptime(v, fmt)
        except Exception:
            pass
    return time.gmtime(0)            # unparseable/blank → earliest


def reconcile(commit=False, mirror=True, forward_only=True):
    # Two reads total: the RSA Tracker and the Truno tracker. Everything else is
    # computed in memory, and all writes go out in one batched request at the end.
    ws = S._ws(S.RSA_SPREADSHEET_ID, S.RSA_SHEET)
    all_values = _retry(lambda: ws.get_all_values(), "read RSA Tracker")
    rsa_rows = [list(x) for x in all_values[S.RSA_DATA_START - 1:]]
    smap = _state_map(rsa_rows)
    truno = _read_truno()
    today = S._today()

    # Every (canonical store, cart) from the WHOLE Truno tracker — keeping the LATEST
    # row per pair (by visit date, tie-broken by sheet order) so a stale/duplicate
    # entry can't win. This is what guarantees every Truno store+cart is accounted for.
    latest = {}
    for idx, row in enumerate(truno):
        store = _canon(row["store"])
        for cn in row["carts"]:
            key = (S._norm(store), str(cn).strip().lstrip("0").upper() or str(cn).strip().upper())
            cand = {"store": store, "trunoStore": row["store"], "cart": str(cn).strip(),
                    "visit": (row["visit"] or "").strip(), "result": (row["result"] or "").strip(),
                    "status": row["status"], "address": (row.get("address") or "").strip(),
                    "code": (row.get("code") or "").strip(), "idx": idx}
            cur = latest.get(key)
            if cur is None or (_visit_key(cand["visit"]), idx) >= (_visit_key(cur["visit"]), cur["idx"]):
                latest[key] = cand

    # Caper deployment index (by address + by store number) for the existence check
    # and for naming net-new stores from the deployment sheet.
    caper_by_addr = caper_by_num = None
    if _sm:
        try:
            caper_by_addr, caper_by_num = _sm.caper_index()
        except Exception:
            caper_by_addr = caper_by_num = None

    plan = {"add": [], "exists": [], "updated": [], "errors": [], "totalPairs": len(latest)}
    seen = set()                              # serials planned/added this run
    new_cells, upd_cells = [], []
    next_row = max(S.RSA_DATA_START, len(all_values) + 1)   # always append after all content

    for e0 in latest.values():
        store, cn = e0["store"], e0["cart"]
        passed = _passed(e0["result"])
        rsa_status, overall = _derive_status(passed, e0["status"])
        result_text, visit = e0["result"], e0["visit"]

        # Validate the Truno store against the Caper sheet by ADDRESS ONLY (no number
        # fallback — that mis-mapped e.g. Sprouts 029 → "Rosauers 29th Ave" on a bare
        # "29"). Chain + store number come from the Truno store code (col B), which is
        # reliable even when the address isn't in the Caper sheet.
        code_chain, code_num = _code_chain_num(e0.get("code"))
        caper_recs = []
        if caper_by_addr is not None:
            ak = _sm.addr_key(e0.get("address", "")) if e0.get("address") else ""
            caper_recs = caper_by_addr.get(ak, []) if ak else []
        text_num = _store_num(store)
        caper_num = (_store_num(caper_recs[0].get("number", "")) or _store_num(caper_recs[0].get("name", ""))) if caper_recs else None
        # ALL candidate numbers — the three sheets disagree (Truno code, Caper, text), so
        # the existence check matches on ANY of them (e.g. Weis Selinsgrove is #226 in
        # Truno/RSA but #1 in Caper; Aberdeen is #78 in Caper but 9490 in the Truno code).
        store_nums = {n for n in (text_num, code_num, caper_num) if n}
        store_num = text_num or code_num or caper_num          # primary, for net-new naming
        caper_name = caper_recs[0]["name"] if caper_recs else ""
        cand_names = {store, e0["trunoStore"]} | {c.get("name") for c in caper_recs}
        cand_ids = {c.get("store_id") for c in caper_recs}
        # Chain word (Caper store id, else the Truno code) → chain tokens that keep the
        # store-number match precise: "WF 78"/"WF78"/"WF-78"/"ShopRite 78" match a
        # Wakefern row, but a "Sprouts 78" never does.
        chain_word = (_chain_word(caper_recs[0]["store_id"]) if caper_recs else "") or code_chain
        chain_toks = set(_CHAIN_TOKENS.get(chain_word, [chain_word])) if chain_word else set()

        # 1) Already in the tracker under ANY identifier — human name, Caper name, full
        #    store id, or store NUMBER under a matching chain (covers WF 78 / WF78 / WF-78)?
        existing = _existing_match(cand_names, cand_ids, chain_toks, store_nums, cn, rsa_rows)
        if existing:
            plan["exists"].append({"store": store, "cart": cn, "serial": existing})
            # MIRROR the latest Truno status onto the existing cart — but only ADVANCE it
            # (forward_only): never knock a more-complete cart backward, and ignore blank /
            # "Untested" / pending Truno rows so real progress is never overwritten. W&M
            # columns are left untouched.
            truno_has = bool((e0["status"] or "").strip()) or passed is not None or bool(result_text)
            if mirror and truno_has:
                er, erow = S._find_row(rsa_rows, existing, store)
                if er:
                    cur = S._cell(erow, S.C_RSASTATUS)
                    cur_ov = S._cell(erow, S.C_OVERALL)
                    cells = []
                    if rsa_status and rsa_status != cur and (not forward_only or _adv(_RSA_RANK, cur, rsa_status)):
                        cells.append(gspread.Cell(er, S.C_RSASTATUS, rsa_status))
                        if passed is True:
                            cells.append(gspread.Cell(er, S.C_RSACOMPL, today))
                        if rsa_status == "Scheduled RSA" and visit:
                            cells.append(gspread.Cell(er, S.C_RSASCHED, visit))
                    else:
                        rsa_status = cur                          # not advanced → report no change
                    # Fix the OVERALL (col O) the dashboard shows — also forward-only.
                    if overall and overall != cur_ov and (not forward_only or _adv(_OVERALL_RANK, cur_ov, overall)):
                        cells.append(gspread.Cell(er, S.C_OVERALL, overall))
                    else:
                        overall = cur_ov
                    # Only write a REAL result — never "Untested"/blank over a real one.
                    if result_text and S._norm(result_text) not in ("", "untested") \
                            and result_text != S._cell(erow, S.C_RSA_RESULTS):
                        cells.append(gspread.Cell(er, S.C_RSA_RESULTS, result_text))
                    if cells:
                        cells.append(gspread.Cell(er, S.C_LASTUPD, today))
                        if commit:
                            upd_cells += cells
                        if rsa_status and len(erow) >= S.C_RSASTATUS:
                            erow[S.C_RSASTATUS - 1] = rsa_status      # reflect locally
                        if overall and len(erow) >= S.C_OVERALL:
                            erow[S.C_OVERALL - 1] = overall
                        plan["updated"].append({"serial": existing, "from": cur, "to": rsa_status or cur,
                                                "overallFrom": cur_ov, "overallTo": overall or cur_ov,
                                                "result": result_text})
            continue

        # 2) Net-new — CLEAN name: an existing same-store label if any, else Banner +
        #    store number ("ShopRite 116", "Sprouts 558"), else the cleaned Truno name.
        #    Never the raw Caper text (it extracted dates / store hours / OCR garbage).
        add_store = _net_new_name(store, store_num, chain_word, chain_toks, rsa_rows)
        serial = S.derive_cart_id(add_store, cn, rsa_rows)
        if not serial:
            plan["errors"].append({"store": add_store, "cart": cn, "error": "could not derive a serial"})
            continue
        if serial in seen:
            plan["exists"].append({"store": add_store, "cart": cn, "serial": serial + " (already this run)"})
            continue
        dup_row, _ = S._find_row(rsa_rows, serial, add_store)
        if dup_row:
            plan["exists"].append({"store": add_store, "cart": cn, "serial": serial})
            continue

        # State from the Truno address first (e.g. "… Las Vegas, NV 89117" → NV), then the
        # Caper address, then any state already on this store in RSA, then the default.
        addr_state = _state_from_addr(e0.get("address", "")) \
            or (_state_from_addr(caper_recs[0].get("address", "")) if caper_recs else None)
        state = addr_state or smap.get(S._norm(add_store)) or ""   # blank, not NJ, when unknown
        e = {"serial": serial, "store": add_store, "trunoStore": e0["trunoStore"], "cart": cn,
             "state": state, "rsaStatus": rsa_status, "overall": overall, "result": result_text,
             "passed": passed, "visit": visit,
             "stateGuessed": not addr_state and S._norm(add_store) not in smap}
        plan["add"].append(e)
        seen.add(serial)
        nr = [""] * 21
        nr[S.C_CARTID - 1] = serial
        nr[S.C_STORE - 1] = add_store
        nr[S.C_STATE - 1] = state
        rsa_rows.append(nr)                   # reflect so later pairs dedup against it
        if commit:
            new_cells += _new_row_cells(next_row, e, today)
        next_row += 1

    if commit:
        if new_cells:
            _batch_write(ws, new_cells)
        if upd_cells:
            _batch_write(ws, upd_cells)
    return plan


def _print_plan(plan, commit):
    verb = "ADDED" if commit else "WOULD ADD"
    print("=" * 78)
    print(f"  TRUNO → RSA reconciliation   ({'COMMIT' if commit else 'DRY RUN — no writes'})")
    print("=" * 78)
    print(f"\n  {verb}: {len(plan['add'])} cart(s)")
    for e in plan["add"]:
        star = "  ⚠ state UNKNOWN — left blank for review" if e.get("stateGuessed") else ""
        res = f"  · result: {e['result']}" if e["result"] else ""
        renamed = f"  (Truno: {e['trunoStore']})" if e.get("trunoStore") and e["trunoStore"] != e["store"] else ""
        print(f"    + {e['serial']:<34} {e['store'][:28]:<28} cart {e['cart']:<4} "
              f"[{e['state']}] {e['rsaStatus']}{res}{renamed}{star}")
    print(f"\n  ALREADY PRESENT (matched, no duplicate): {len(plan['exists'])} cart(s)")
    for e in plan["exists"]:                       # full audit — confirms each pair's fate
        print(f"    = {e['store'][:30]:<30} cart {e['cart']:<4} → {e['serial']}")
    if plan["updated"]:
        print(f"\n  UPDATED existing carts to mirror Truno (dashboard will reflect these): {len(plan['updated'])}")
        for u in plan["updated"]:
            arrow = f"{u.get('from') or '—'} → {u.get('to') or '—'}"
            ov = ""
            if u.get("overallTo") and u.get("overallTo") != u.get("overallFrom"):
                ov = f"   overall: {u.get('overallFrom') or '—'} → {u['overallTo']}"
            res = f"  · {u['result']}" if u.get("result") else ""
            print(f"    ~ {u['serial']:<34} {arrow}{ov}{res}")
    if plan["errors"]:
        print(f"\n  ⚠ ERRORS (NOT in tracker — need a look): {len(plan['errors'])}")
        for er in plan["errors"]:
            print(f"    ! {er['store']} cart {er['cart']}: {er['error']}")
    total = plan.get("totalPairs", len(plan["add"]) + len(plan["exists"]) + len(plan["errors"]))
    covered = len(plan["add"]) + len(plan["exists"])
    print("\n" + "=" * 78)
    print(f"  Truno store+cart pairs: {total}  →  added {len(plan['add'])}, "
          f"already present {len(plan['exists'])}, errors {len(plan['errors'])}")
    if not plan["errors"]:
        print(f"  ✅ every Truno store+cart ({covered}/{total}) is {'now' if commit else 'either already or will be'} in the RSA Tracker.")
    else:
        print(f"  ⚠ {len(plan['errors'])} pair(s) could NOT be placed — see ERRORS above.")
    if not commit:
        print("  Preview only. Re-run with  --commit  to write these to the RSA Tracker.")
    else:
        print("  Done. Open the dashboard and hit Refresh to see the new carts.")
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser(description="Reconcile Truno tracker into the RSA Tracker")
    ap.add_argument("--commit", action="store_true", help="actually write (default is a dry-run preview)")
    ap.add_argument("--no-mirror", action="store_true",
                    help="do NOT update existing carts (default: mirror the latest Truno status onto existing carts)")
    ap.add_argument("--exact-mirror", action="store_true",
                    help="mirror Truno EXACTLY, even backward (default: forward-only — never regress a more-complete cart)")
    args = ap.parse_args()
    plan = reconcile(commit=args.commit, mirror=not args.no_mirror, forward_only=not args.exact_mirror)
    _print_plan(plan, args.commit)


if __name__ == "__main__":
    main()
