#!/usr/bin/env python3
"""
Store mapping for the RSA Agent.

PRIMARY source — the Deployed Stores directory (deployed_stores.csv): the authoritative
list of live stores. Each store carries four user-selectable names — Store, STORE_NICKNAME,
External Store ID, and Retailer — plus a split address (Street/City/Zip/State). The Slack
bot validates every onboarding request against this directory (match_store): the ops
manager may only pick a store that exists here, by any of the four names. Once a unique
store is identified, its single-line 'Street, City, State Zip' address is written to the
Truno tracker (col E) when service is requested, and its USPS State is used directly.
See match_store(), load_deployed(), store_address(), store_state().

FALLBACK — the legacy TRUNO ↔ Caper ↔ RSA address-join (below), used only for a store
that isn't found in the Deployed Stores directory.

────────────────────────────────────────────────────────────────────────────────────
Legacy address-join between TRUNO and the RSA Tracker — joined on STREET ADDRESS.

Why address: a physical store is named differently in each system
  • Truno IC name (col A):  "Weis 226 Selinsgrove"
  • Truno store code (col B): "ISC0050-WKF-0113"
  • Caper deployment sheet:  "Weis 226"  (the internal/canonical name)
  • Scale Test Form (vision): the retailer's own number, e.g. "#180"
…and the store NUMBER alone is unreliable (Truno "Weis 226 Selinsgrove" vs RSA
"Weis 226" looked like two stores). The street ADDRESS is the stable join key.

This module joins the Truno tracker's address (col E) to the Caper deployment
sheet's address, producing one canonical record per physical store with every
name variant, then cross-checks the RSA Tracker. Stores it can't match on address
fall back to store number and are FLAGGED for you to confirm.

Caper data: export the "Caper Cart Deployment – Store Information" page to a CSV
in this folder (columns incl. a store name + an address) — the loader auto-detects
the columns. Or point CAPER_SHEET_ID at a Google Sheet shared with the service account.

Run from the project folder:
    python3 store_map.py               # build + print the mapping, write store_map.csv, flag gaps
    python3 store_map.py --interactive # confirm each uncertain store, save to store_map.json
"""
import os
import re
import csv
import glob
import json
import difflib
import argparse

import rsa_sheets as S

OVERRIDE_FILE   = os.environ.get("STORE_MAP_FILE", "store_map.json")
CSV_OUT         = os.environ.get("STORE_MAP_CSV", "store_map.csv")
CAPER_CSV       = os.environ.get("CAPER_STORES_CSV", "caper_stores.csv")
CAPER_SHEET_ID  = os.environ.get("CAPER_SHEET_ID", "")
CAPER_SHEET_TAB = os.environ.get("CAPER_SHEET_TAB", "")

# The Deployed Stores export is the PRIMARY store directory: the authoritative list
# of live stores with their selectable names and full split address. Drop the latest
# "Deployed Stores YYYY-MM-DD.csv" in this folder (or set DEPLOYED_STORES_CSV) and it
# becomes the default source for name validation, address, and state — the Caper /
# RSA address-join below is kept only as a fallback for stores not in this file.
DEPLOYED_CSV    = os.environ.get("DEPLOYED_STORES_CSV", "deployed_stores.csv")

# New-launch additions are written here (a separate writable file) so the read-only
# Deployed Stores export can be refreshed freely without losing manually added stores.
# load_deployed() merges both files automatically.
NEW_LAUNCHES_CSV = os.environ.get("NEW_LAUNCHES_CSV", "new_launches.csv")

T_ADDRESS    = 5    # Truno col E — Address
T_TRUNO_CODE = 2    # Truno col B — store code

CHAIN_BANNER = {"WKF": "ShopRite (Wakefern)", "SFM": "Sprouts", "SPR": "Sprouts",
                "SCH": "Schnucks", "DFD": "Davis Food & Drug", "MCK": "McKeevers",
                "WEI": "Weis", "GFH": "Gelson's"}

# ── Address normalization (the join key) ────────────────────────────────────────
_ABBR = {"street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr",
         "boulevard": "blvd", "lane": "ln", "highway": "hwy", "route": "rte", "rt": "rte",
         "place": "pl", "court": "ct", "parkway": "pkwy", "square": "sq", "terrace": "ter",
         "north": "n", "south": "s", "east": "e", "west": "w",
         "suite": "ste", "ste": "ste", "unit": "unit", "apt": "apt"}


def _norm_addr(addr):
    a = str(addr or "").lower()
    a = re.sub(r"[.,#/]", " ", a)
    return " ".join(_ABBR.get(t, t) for t in a.split())


def addr_key(addr):
    """Canonical address key: house# + zip when both present (most reliable),
    else house# + first street word, else the whole normalized string."""
    na = _norm_addr(addr)
    if not na:
        return ""
    hm = re.match(r"(\d+)", na)
    house = hm.group(1) if hm else ""
    zips = re.findall(r"\b\d{5}\b", na)
    z = next((x for x in zips if x != house), "")
    if house and z:
        return f"{house}|{z}"
    sw = re.match(r"\d+\s+([a-z0-9]+)", na)
    if house and sw:
        return f"{house}|{sw.group(1)}"
    return na


def parse_truno_code(code):
    """'ISC0050-WKF-0113' -> ('WKF', '113'). Returns (chain|None, number|None)."""
    chain, num = None, None
    for p in re.split(r"[-_\s]+", str(code or "").upper()):
        if not p or p.startswith("ISC"):
            continue
        if re.fullmatch(r"0*\d+", p):
            num = p.lstrip("0") or "0"
        elif re.fullmatch(r"[A-Z]{2,6}", p) and chain is None:
            chain = p
    return chain, num


def _store_number(*texts):
    for t in texts:
        nums = S._store_nums(t) if t else None
        if nums:
            return sorted(nums, key=lambda x: (-len(x), x))[0].lstrip("0") or "0"
    return None


# ── Manual overrides (store_map.json) consulted by resolve() ────────────────────
def _overrides():
    try:
        with open(OVERRIDE_FILE) as f:
            d = json.load(f)
        return {S._norm(k): v for k, v in (d.get("aliases") or {}).items()}
    except Exception:
        return {}


def resolve(store):
    """Canonical store string for cart matching: applies any store_map.json alias,
    else returns the input unchanged (its store number is the fallback join key)."""
    if not store:
        return store
    ov = _overrides()
    n = S._norm(store)
    if n in ov:
        return ov[n]
    for alias, canon in ov.items():
        if alias and alias in n:
            return canon
    return store


# ══ Deployed Stores directory — the PRIMARY store list ═══════════════════════════
# One record per live store with the four user-selectable name fields and a full
# split address. This is what the Slack bot validates store input against, and the
# source the Truno tracker's Address (col E) is populated from.

# The selectable name fields, in match-priority order (most specific first).
# 'Store' is the unique canonical key; 'banner_alias' ("<Banner> <External Store ID>",
# e.g. "ShopRite 153") is the preferred ops name; nickname/external-id are near-unique;
# a Retailer names a GROUP of stores, so a retailer match is intentionally ambiguous.
# 'banner_alias' has no CSV column — it's computed in load_deployed().
_DEPLOYED_FIELDS = [
    ("store",        "Store"),
    ("banner_alias", None),
    ("internal_id",  None),
    ("nickname",     "STORE_NICKNAME"),
    ("external_id",  "External Store ID"),
    ("retailer",     "Retailer"),
]
_DEPLOYED_CACHE = None

# Deployment-environment prefixes stripped from the Store field to get the internal id
# (e.g. "prod-wakefern-23" → "wakefern-23", "qvs-wakefern-6" → "wakefern-6").
_ENV_PREFIX_RE = re.compile(r"^(?:prod|qvs|dev|stg|staging|test|qa|demo)-", re.I)

# Retailer → the customer-facing banner used in the store name. The Wakefern co-op's
# stores (retailer "shoprite"/"wakefern-*") are called "ShopRite <#>"; each retailer's
# banner is listed so "<Banner> <External Store ID>" reads the way ops refers to a store.
_RETAILER_BANNER = {
    "shoprite": "ShopRite", "wakefern": "ShopRite", "thefreshgrocer": "The Fresh Grocer",
    "fairway": "Fairway", "pricerite": "Price Rite", "sprouts": "Sprouts", "kroger": "Kroger",
    "mckeeverspricechopper": "Price Chopper", "mckeeversmarket": "McKeever's",
    "geisslers": "Geissler's", "queens": "Queens", "davis": "Davis", "schnucks": "Schnucks",
    "soelbergsmarket": "Soelberg's", "stewarts": "Stewart's", "wegmans": "Wegmans",
    "clarksmarket": "Clark's Market", "weis": "Weis", "bowmans": "Bowman's",
    "bigbunny": "Big Bunny", "coles": "Cole's", "foodtown": "Foodtown",
    "foodtownofhastings": "Foodtown", "bristolfarms": "Bristol Farms", "miggyscorpfive": "Miggy's",
}


def _norm_name(s):
    """Loose key for name matching: lowercase, alphanumerics only. So 'Price Chopper
    #200', 'price chopper 200' and 'Price Chopper 200' all collapse to one key."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def banner_of(retailer):
    """Customer-facing banner for a retailer string (Wakefern/'shoprite' → 'ShopRite',
    'mckeevers-price-chopper' → 'Price Chopper'). Unlisted retailers fall back to a
    title-cased form (dropping a trailing 'market')."""
    key = _norm_name(retailer)
    if key in _RETAILER_BANNER:
        return _RETAILER_BANNER[key]
    words = [w for w in re.split(r"[^A-Za-z0-9]+", str(retailer or "").strip()) if w]
    if len(words) > 1 and words[-1].lower() == "market":
        words = words[:-1]
    return " ".join(w.capitalize() for w in words)


def _store_num_from_ext(ext_id):
    """Ops-facing store number from an External Store ID when it's a clean numeric id
    (optional zero-pad + optional single letter suffix): '00226'→'226', '29C'→'29',
    '153'→'153'. Compound ('014-00353') or slug ('big_bunny_market') ids return '' so the
    caller falls back to the nickname."""
    m = re.fullmatch(r"0*(\d+)[A-Za-z]?", str(ext_id or "").strip())
    return m.group(1) if m else ""


def banner_alias(retailer, ext_id):
    """'<Banner> <store #>' (e.g. 'ShopRite 153'), or '' when the external id isn't a
    clean number or the banner is unknown."""
    num = _store_num_from_ext(ext_id)
    banner = banner_of(retailer)
    return f"{banner} {num}" if (num and banner) else ""


def internal_id_of(store):
    """Internal id from the Store field with the deployment-env prefix removed
    ('prod-wakefern-23' → 'wakefern-23')."""
    return _ENV_PREFIX_RE.sub("", str(store or "").strip())


def _deployed_path():
    """Locate the Deployed Stores CSV: the configured path if it exists, else the
    newest 'Deployed Stores*.csv' in the working folder (so a fresh export drops in
    without reconfiguring)."""
    if DEPLOYED_CSV and os.path.exists(DEPLOYED_CSV):
        return DEPLOYED_CSV
    hits = sorted(glob.glob("Deployed Stores*.csv") + glob.glob("deployed_stores*.csv"),
                  key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    return hits[0] if hits else DEPLOYED_CSV


def _build_address(street, city, state, zip_):
    """Single-line 'Street, City, State Zip' (the format written to Truno col E).
    Skips empty components so a partial record still yields a usable address."""
    street, city = str(street or "").strip(), str(city or "").strip()
    state, zip_  = str(state or "").strip().upper(), str(zip_ or "").strip()
    locality = " ".join(p for p in (state, zip_) if p)        # 'MO 64024'
    return ", ".join(p for p in (street, city, locality) if p)


def _parse_deployed_csv(path):
    """Read one deployed-stores CSV (main export or new_launches.csv) and return a list
    of normalised record dicts. Shared by load_deployed() and add_to_deployed()."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rec = {
                "store":       (r.get("Store") or "").strip(),
                "nickname":    (r.get("STORE_NICKNAME") or "").strip(),
                "external_id": (r.get("External Store ID") or "").strip(),
                "retailer":    (r.get("Retailer") or "").strip(),
                "street":      (r.get("Street") or "").strip(),
                "city":        (r.get("City") or "").strip(),
                "zip":         (r.get("Zip") or "").strip(),
                "state":       (r.get("State") or "").strip().upper(),
                "phone":       (r.get("Phone") or "").strip(),
            }
            if not (rec["store"] or rec["nickname"] or rec["external_id"] or rec["retailer"]):
                continue
            rec["address"]      = _build_address(rec["street"], rec["city"], rec["state"], rec["zip"])
            rec["banner"]       = banner_of(rec["retailer"])
            rec["banner_alias"] = banner_alias(rec["retailer"], rec["external_id"])
            rec["internal_id"]  = internal_id_of(rec["store"])
            rec["display"]      = rec["banner_alias"] or rec["nickname"] or rec["store"]
            rec["_keys"]        = {fld: _norm_name(rec.get(fld, "")) for fld, _ in _DEPLOYED_FIELDS}
            out.append(rec)
    return out


def load_deployed(path=None):
    """Return [{store, banner, banner_alias, internal_id, nickname, external_id, retailer,
    street, city, zip, state, phone, address, display, _keys}] — one record per deployed
    store. Merges the primary Deployed Stores export (read-only) with new_launches.csv
    (writable), so manually added new-launch stores survive a CSV refresh."""
    p = path or _deployed_path()
    out = _parse_deployed_csv(p) if (p and os.path.exists(p)) else []
    # Merge new-launch additions (separate writable file) — skip any duplicate store keys.
    existing_keys = {r["store"] for r in out if r["store"]}
    nl = NEW_LAUNCHES_CSV
    if nl and os.path.exists(nl):
        for rec in _parse_deployed_csv(nl):
            if rec["store"] not in existing_keys:
                out.append(rec)
                existing_keys.add(rec["store"])
    return out


def deployed_directory(refresh=False):
    """Cached Deployed Stores records (load once per process)."""
    global _DEPLOYED_CACHE
    if refresh or _DEPLOYED_CACHE is None:
        _DEPLOYED_CACHE = load_deployed()
    return _DEPLOYED_CACHE


def deployed_address(rec):
    """The single-line address for a matched record (or '')."""
    return (rec or {}).get("address", "") if isinstance(rec, dict) else ""


def match_store(query, limit=8):
    """Resolve a user-typed store reference against the Deployed Stores directory,
    matching on ANY of the four selectable names. Returns a dict:

      status='matched'   → exactly one store; 'store'=record, 'matchedBy'=field
      status='ambiguous' → an exact name match hitting several stores (e.g. a retailer,
                           or the few duplicate nicknames/ids); 'candidates'=records
      status='suggest'   → no exact match but close names exist; 'candidates'=records
      status='none'      → nothing close

    'count' is the total number of stores the query touched (candidates may be capped
    at 'limit' for display). Matching is case/space/punctuation-insensitive."""
    res = {"status": "none", "query": query, "store": None, "matchedBy": None,
           "candidates": [], "count": 0}
    nq = _norm_name(query)
    directory = deployed_directory()
    if not nq or not directory:
        return res

    # 1) Exact match, by field priority. The first field that matches decides how we
    #    label it; a single hit resolves, several hits are ambiguous (user must pick).
    for fld, _label in _DEPLOYED_FIELDS:
        hits, seen = [], set()
        for rec in directory:
            if rec["_keys"].get(fld) == nq and rec["store"] not in seen:
                hits.append(rec); seen.add(rec["store"])
        if hits:
            res["matchedBy"] = fld
            res["count"] = len(hits)
            if len(hits) == 1:
                res.update(status="matched", store=hits[0], candidates=hits)
            else:
                res.update(status="ambiguous", candidates=hits[:limit])
            return res

    # 2) No exact match → fuzzy suggestions. Specific fields (store/nickname/id) are
    #    weighted above Retailer, since a retailer alone matches a whole chain — without
    #    this a query like 'sprouts 15' would be buried under 20+ generic 'sprouts' rows.
    _W = {"store": 1.0, "banner_alias": 1.0, "internal_id": 1.0,
          "nickname": 1.0, "external_id": 1.0, "retailer": 0.5}
    scored = {}
    for rec in directory:
        best = 0.0
        for fld, _label in _DEPLOYED_FIELDS:
            k = rec["_keys"].get(fld) or ""
            if not k:
                continue
            if nq and (nq in k or k in nq):
                raw = 0.9 if len(nq) >= 3 else 0.6
            else:
                raw = difflib.SequenceMatcher(None, nq, k).ratio()
            best = max(best, raw * _W[fld])
        if best >= 0.6:
            prev = scored.get(rec["store"])
            if prev is None or best > prev[0]:
                scored[rec["store"]] = (best, rec)
    ranked = [rec for _s, rec in sorted(scored.values(), key=lambda t: -t[0])]
    if ranked:
        res.update(status="suggest", candidates=ranked[:limit], count=len(ranked))
    return res


def store_names(field=None):
    """All selectable store names for one field (or every field) — for help text /
    auto-complete. field in {'store','nickname','external_id','retailer'}."""
    directory = deployed_directory()
    if field:
        return sorted({rec[field] for rec in directory if rec.get(field)})
    return sorted({rec[fld] for rec in directory for fld, _ in _DEPLOYED_FIELDS if rec.get(fld)})


# ── Load the Caper deployment data (name + address) ─────────────────────────────
def _detect_cols(header):
    h = [str(x).lower() for x in header]
    def find(*keys):
        for i, col in enumerate(h):
            if any(k in col for k in keys):
                return i
        return None
    return (find("store name", "store_name", "location", "name"),
            find("address", "street", "addr"),
            find("store_id", "store id", "external id", "external_id", "wf id"),
            find("store_number", "store number", "store #"))


def load_caper():
    """Return [{name, address, store_id, number}] from caper_stores.csv or a Sheet."""
    rows = None
    if CAPER_SHEET_ID:
        book = S._client().open_by_key(CAPER_SHEET_ID)
        sh = book.worksheet(CAPER_SHEET_TAB) if CAPER_SHEET_TAB else book.sheet1
        rows = sh.get_all_values()
    elif os.path.exists(CAPER_CSV):
        with open(CAPER_CSV, newline="") as f:
            rows = list(csv.reader(f))
    if not rows or len(rows) < 2:
        return []
    name_i, addr_i, id_i, num_i = _detect_cols(rows[0])
    def get(r, i):
        return r[i].strip() if i is not None and i < len(r) else ""
    out = []
    for r in rows[1:]:
        name, addr = get(r, name_i), get(r, addr_i)
        sid, num = get(r, id_i), get(r, num_i)
        if name or addr:
            out.append({"name": name, "address": addr, "store_id": sid,
                        "number": (num or _store_number(name, addr) or "")})
    return out


def caper_index(caper=None):
    """Index the Caper rows by address key and by store number for fast lookup."""
    caper = caper if caper is not None else load_caper()
    by_addr, by_num = {}, {}
    for c in caper:
        k = addr_key(c.get("address", ""))
        if k:
            by_addr.setdefault(k, []).append(c)
        n = (c.get("number") or "").lstrip("0") or c.get("number")
        if n:
            by_num.setdefault(n, []).append(c)
    return by_addr, by_num


# ── Build the Truno ↔ Caper ↔ RSA mapping (address-first) ────────────────────────
def build(truno_rows=None, caper=None, rsa_rows=None):
    if truno_rows is None:
        truno_rows = S._ws(S.TRUNO_SPREADSHEET_ID, S.TRUNO_SHEET).get_all_values()[1:]
    if caper is None:
        caper = load_caper()
    if rsa_rows is None:
        rsa_rows = S._ws(S.RSA_SPREADSHEET_ID, S.RSA_SHEET).get_all_values()[S.RSA_DATA_START - 1:]

    caper_by_addr, caper_by_num = {}, {}
    for c in caper:
        k = addr_key(c["address"])
        if k:
            caper_by_addr.setdefault(k, []).append(c)
        num = _store_number(c["name"], c["address"])
        if num:
            caper_by_num.setdefault(num, []).append(c)

    rsa_by_num = {}
    for r in rsa_rows:
        cid = S._cell(r, S.C_CARTID)
        if not cid:
            continue
        store = S._cell(r, S.C_STORE)
        nums = set(S._store_nums(store))
        m = re.search(r"_0*(\d+)_M3_", cid)
        if m:
            nums.add(m.group(1).lstrip("0") or m.group(1))
        for num in nums:
            rsa_by_num.setdefault(num, set()).add(store or cid)

    out, seen = [], set()
    for r in truno_rows:
        ic, code, addr = S._cell(r, S.T_STORE), S._cell(r, T_TRUNO_CODE), S._cell(r, T_ADDRESS)
        if (not ic and not code) or (ic, code, addr) in seen:
            continue
        seen.add((ic, code, addr))
        chain, code_num = parse_truno_code(code)
        number = code_num or _store_number(ic, addr)
        e = {"trunoIC": ic, "trunoCode": code, "trunoAddr": addr, "chain": chain,
             "banner": CHAIN_BANNER.get(chain or "", ""), "number": number,
             "caper": "", "caperAddr": "", "rsa": "", "basis": "", "status": ""}

        ak = addr_key(addr)
        caper_hit = caper_by_addr.get(ak, []) if ak else []
        if caper_hit:
            e["basis"] = "address"
        elif number and caper_by_num.get(number):
            caper_hit = caper_by_num[number]
            e["basis"] = "number"

        if len(caper_hit) == 1:
            e["caper"], e["caperAddr"] = caper_hit[0]["name"], caper_hit[0]["address"]
        elif len(caper_hit) > 1:
            e["caper"] = " | ".join(sorted({c["name"] for c in caper_hit}))

        # Cross-check to the RSA Tracker by the best number we have.
        rnum = _store_number(e["caper"]) or number
        rsa_stores = sorted(rsa_by_num.get(rnum, [])) if rnum else []
        e["rsa"] = " | ".join(rsa_stores)

        if not addr and not caper_hit:
            e["status"] = "no_address"
        elif not caper_hit:
            e["status"] = "no_caper_match"
        elif len(caper_hit) > 1:
            e["status"] = "ambiguous"
        elif not rsa_stores:
            e["status"] = "matched_no_rsa"          # store known, but not yet in RSA = net-new
        elif len(rsa_stores) > 1:
            e["status"] = "rsa_ambiguous"
        else:
            e["status"] = "matched"
        out.append(e)
    return out


# ── State resolution: store → USPS state, from the Caper deployment ADDRESS ───────
# Per ops: never default a state. Determine it from the store's address (a ZIP alone
# pins the state); else a saved per-store override; otherwise the caller must ASK.
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
STATE_ABBRS = set(STATE_NAMES.values())

# 3-digit ZIP prefix → state. Ranges are non-overlapping. The DC/VA split at 200/201
# (20170 Herndon is VA, not DC) and the El Paso 885→TX carve-out are real USPS quirks
# — verified against ZIP lookups, not guessed.
_ZIP3_RANGES = [
    (5, 5, "NY"), (6, 7, "PR"), (8, 8, "VI"), (9, 9, "PR"),
    (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"), (39, 49, "ME"), (50, 59, "VT"),
    (60, 69, "CT"), (70, 89, "NJ"), (100, 149, "NY"), (150, 196, "PA"), (197, 199, "DE"),
    (200, 200, "DC"), (201, 201, "VA"), (202, 205, "DC"), (206, 219, "MD"), (220, 246, "VA"),
    (247, 268, "WV"), (270, 289, "NC"), (290, 299, "SC"), (300, 319, "GA"), (320, 349, "FL"),
    (350, 369, "AL"), (370, 385, "TN"), (386, 397, "MS"), (398, 399, "GA"), (400, 427, "KY"),
    (430, 459, "OH"), (460, 479, "IN"), (480, 499, "MI"), (500, 528, "IA"), (530, 549, "WI"),
    (550, 567, "MN"), (570, 577, "SD"), (580, 588, "ND"), (590, 599, "MT"), (600, 629, "IL"),
    (630, 658, "MO"), (660, 679, "KS"), (680, 693, "NE"), (700, 714, "LA"), (716, 729, "AR"),
    (730, 749, "OK"), (750, 799, "TX"), (800, 816, "CO"), (820, 831, "WY"), (832, 838, "ID"),
    (840, 847, "UT"), (850, 865, "AZ"), (870, 884, "NM"), (885, 885, "TX"), (889, 898, "NV"),
    (900, 961, "CA"), (967, 968, "HI"), (970, 979, "OR"), (980, 994, "WA"), (995, 999, "AK"),
]


def state_from_zip(zip5):
    """Map a 5-digit ZIP to its USPS state, or None."""
    m = re.match(r"\s*(\d{3})", str(zip5 or ""))
    if not m:
        return None
    p = int(m.group(1))
    for lo, hi, st in _ZIP3_RANGES:
        if lo <= p <= hi:
            return st
    return None


def state_from_address(address):
    """Best-effort USPS state for an address. Order of trust:
      1. a ZIP at the END of the string (the postal ZIP — NOT a leading street number),
      2. a spelled-out state name,
      3. a ZIP right after a 2-letter state code ('OH 45040'),
      4. a trailing ', XX' state code with no ZIP.
    A bare 5-digit string (e.g. '20170') counts as a ZIP. Returns (state|None, basis|None).
    Conservative on purpose: '23269 N Scottsdale Rd' has no real ZIP → None (ask),
    rather than mistaking the street number for one."""
    a = str(address or "").strip()
    if not a:
        return None, None
    m = re.search(r"(\d{5})(?:-\d{4})?\s*(?:,?\s*(?:USA|US))?\s*$", a)    # 1. ZIP at the end
    if m:
        st = state_from_zip(m.group(1))
        if st:
            return st, "zip"
    m = re.search(r"\b([A-Za-z]{2})\b[, ]+(\d{5})(?:-\d{4})?\b", a)        # 2. 'OH 45040'
    if m and m.group(1).upper() in STATE_ABBRS:
        return (state_from_zip(m.group(2)) or m.group(1).upper()), "zip"
    m = re.search(r",\s*([A-Za-z]{2})\.?\s*$", a)                         # 3a. trailing ', XX'
    if m and m.group(1).upper() in STATE_ABBRS:
        return m.group(1).upper(), "abbr"
    _DIRECTIONAL = {"NE", "NW", "SE", "SW"}                               # 3b. 'City XX' (space)
    m = re.search(r"\s([A-Za-z]{2})\.?\s*$", a)                           #     (skip NE/NW/SE/SW —
    if m and m.group(1).upper() in STATE_ABBRS and m.group(1).upper() not in _DIRECTIONAL:
        return m.group(1).upper(), "abbr"                                 #      street directionals)
    # 4. spelled-out state name — LEAST trusted, and only in the locality field (after the
    #    last comma) so a street/city word ('Indiana St', 'Washington Crossing') can't fire it.
    tail = a.rsplit(",", 1)[-1] if "," in a else a
    low = " " + re.sub(r"[^a-z ]+", " ", tail.lower()) + " "
    for name, ab in sorted(STATE_NAMES.items(), key=lambda kv: -len(kv[0])):
        if f" {name} " in low:
            return ab, "name"
    return None, None


def _state_overrides():
    """Per-store state overrides saved in store_map.json under 'states'."""
    try:
        with open(OVERRIDE_FILE) as f:
            d = json.load(f)
        return {S._norm(k): str(v).upper() for k, v in (d.get("states") or {}).items()}
    except Exception:
        return {}


def add_to_deployed(store, nickname, external_id, retailer, street, city, state, zip_, phone=""):
    """Append a new store to new_launches.csv (a separate writable file) and refresh
    the in-memory cache. The primary deployed_stores.csv is never touched, so it can
    be refreshed/replaced freely without losing new-launch entries.
    Returns the newly added record dict (as returned by match_store)."""
    global _DEPLOYED_CACHE
    p = NEW_LAUNCHES_CSV
    file_exists = os.path.exists(p)
    fieldnames = [
        "Partner", "PARTNER_ID_FK", "Retailer", "RETAILER_ID_FK",
        "Store", "STORE_NICKNAME", "STORE_ID_FK", "External Store ID",
        "IC WH", "IC WHL", "Available Carts", "Deployed Carts",
        "Launch Date", "Months Live", "Store Maturity", "live app versions",
        "Charger", "Street", "City", "Zip", "State", "Phone",
        "Internal Store #", "POS Provider", "POS Reseller",
        "Loyalty Provider", "Middleware Provider", "Coupon Provider",
    ]
    row = {
        "Partner": retailer, "PARTNER_ID_FK": "", "Retailer": retailer,
        "RETAILER_ID_FK": "", "Store": store, "STORE_NICKNAME": nickname,
        "STORE_ID_FK": "", "External Store ID": external_id,
        "IC WH": "", "IC WHL": "", "Available Carts": "", "Deployed Carts": "",
        "Launch Date": "", "Months Live": "", "Store Maturity": "new launch",
        "live app versions": "", "Charger": "", "Street": street, "City": city,
        "Zip": zip_, "State": str(state or "").upper(), "Phone": phone,
        "Internal Store #": "", "POS Provider": "", "POS Reseller": "",
        "Loyalty Provider": "", "Middleware Provider": "", "Coupon Provider": "",
    }
    with open(p, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()    # first entry — write the header row
        writer.writerow(row)
    # Refresh so match_store sees the new entry immediately
    _DEPLOYED_CACHE = load_deployed()
    m = match_store(store)
    return m.get("store") or {"store": store, "nickname": nickname,
                               "external_id": external_id, "retailer": retailer,
                               "address": _build_address(street, city, state, zip_),
                               "display": nickname or store}


def save_state_override(store, state):
    """Remember a confirmed store→state so the bot never has to ask again."""
    data = {}
    try:
        with open(OVERRIDE_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    states = data.get("states", {})
    states[store] = str(state).upper()
    data["states"] = states
    with open(OVERRIDE_FILE, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    return states[store]


def _loose_num(store):
    """Most-specific store number incl. single digits ('Sprouts 6' → '6')."""
    nums = re.findall(r"\d+", str(store or ""))
    if not nums:
        return None
    return max(nums, key=len).lstrip("0") or "0"


def _chain_word(s):
    m = re.search(r"[A-Za-z][A-Za-z'&]+", str(s or ""))
    return m.group(0).lower() if m else ""


def _caper_match(store, caper=None):
    """Caper rows for a store like 'Sprouts 15' — matched on store NUMBER and a shared
    chain word, so 'Sprouts 15' can't collide with 'Kroger 15'."""
    caper = caper if caper is not None else load_caper()
    num = _loose_num(store)
    cw = _chain_word(store)
    hits = []
    for c in caper:
        cnum = (c.get("number") or "")
        cnum = cnum.lstrip("0") or cnum
        if num and cnum == num:
            ccw = _chain_word(c.get("name", ""))
            if not cw or not ccw or cw == ccw or cw in ccw or ccw in cw:
                hits.append(c)
    return hits


def store_address(store, address_hint=None, caper=None):
    """Best street address for a store, used to populate the Truno tracker's Address
    column (col E) and the vendor emails. Order of trust:
      1. a unique FULL Caper deployment address (has letters → a real street), since
         that column is literally the Truno↔Caper join key — writing it back makes the
         join exact,
      2. an address the caller/agent already collected (the user-typed hint),
      3. a unique but bare Caper value (e.g. a lone ZIP) as a last resort.
    Returns '' when nothing reliable is found OR the Caper match is ambiguous AND no
    hint was given — the caller should then BLOCK and ask, never guess."""
    # PRIMARY: the Deployed Stores directory. A unique name match yields the exact,
    # full 'Street, City, State Zip' — this is what gets written to the Truno tracker
    # (col E) when service is requested. Falls through to Caper only if not found.
    m = match_store(store)
    if m["status"] == "matched":
        a = deployed_address(m["store"])
        if a and re.search(r"[A-Za-z]", a):
            return a
    hint = str(address_hint or "").strip()
    hits = _caper_match(store, caper)
    addrs = sorted({(h.get("address") or "").strip() for h in hits if (h.get("address") or "").strip()})
    caper_addr = addrs[0] if len(addrs) == 1 else ""
    if caper_addr and re.search(r"[A-Za-z]", caper_addr):   # a real street address wins
        return caper_addr
    if hint:                                                # else whatever the agent gathered
        return hint
    return caper_addr                                       # a bare ZIP, or '' (→ block/ask)


def store_state(store, caper=None):
    """Resolve a store's USPS state from saved overrides + the Caper deployment
    address. Returns {store, state, address, basis, confident, candidates}. NEVER
    guesses a default — if it can't tell, confident=False and the caller should ask."""
    res = {"store": store, "state": None, "address": "", "basis": None,
           "confident": False, "candidates": []}
    if not store:
        return res
    n = S._norm(store)
    ov = _state_overrides()
    if n in ov:                                  # a manually-confirmed state still wins
        res.update(state=ov[n], basis="override", confident=True)
        return res
    # PRIMARY: the Deployed Stores directory carries the USPS state directly.
    m = match_store(store)
    if m["status"] == "matched":
        st = (m["store"].get("state") or "").upper()
        if st in STATE_ABBRS:
            res.update(state=st, address=deployed_address(m["store"]),
                       basis="deployed", confident=True, candidates=[st])
            return res
    hits = _caper_match(store, caper)
    found = []
    for h in hits:
        st, basis = state_from_address(h.get("address", ""))
        if st:
            found.append((st, basis, h.get("address", "")))
    uniq = sorted({s for s, _, _ in found})
    if len(uniq) == 1:
        st = uniq[0]
        b = next(bb for s, bb, _ in found if s == st)
        a = next(aa for s, _, aa in found if s == st)
        res.update(state=st, address=a, basis=f"caper-{b}", confident=True, candidates=uniq)
    elif uniq:                                   # address maps to >1 state → ambiguous
        res.update(candidates=uniq, address="; ".join(h.get("address", "") for h in hits))
    return res


def _save_aliases(new_aliases):
    data = {}
    try:
        with open(OVERRIDE_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    al = data.get("aliases", {})
    al.update({k: v for k, v in new_aliases.items() if k and v})
    data["aliases"] = al
    with open(OVERRIDE_FILE, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def main():
    ap = argparse.ArgumentParser(description="Build the Truno ↔ Caper ↔ RSA store map (address-joined)")
    ap.add_argument("--interactive", action="store_true",
                    help="confirm each uncertain store, saving choices to store_map.json")
    args = ap.parse_args()

    caper = load_caper()
    if not caper:
        print(f"⚠  No Caper data found. Export the Caper Store-Information table to '{CAPER_CSV}' "
              f"(needs a store-name + address column), or set CAPER_SHEET_ID. "
              f"Falling back to store-number matching only — accuracy will be limited.\n")

    rows = build(caper=caper)
    OK = {"matched", "matched_no_rsa"}
    matched   = [e for e in rows if e["status"] in OK]
    uncertain = [e for e in rows if e["status"] not in OK]

    print("=" * 100)
    print(f"  TRUNO ↔ CAPER ↔ RSA store map — {len(matched)} resolved, {len(uncertain)} need confirmation")
    print("=" * 100)
    for e in matched:
        tag = "NET-NEW (not in RSA)" if e["status"] == "matched_no_rsa" else (e["rsa"] or "")
        print(f"  ✓[{e['basis']:<7}] {e['trunoIC'][:26]:<26} → {(e['caper'] or '?')[:22]:<22} → {tag}")

    with open(CSV_OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Truno IC name", "Truno code", "Truno address", "chain", "banner",
                    "store #", "Caper name", "Caper address", "RSA store(s)", "match basis", "status"])
        for e in rows:
            w.writerow([e["trunoIC"], e["trunoCode"], e["trunoAddr"], e["chain"] or "", e["banner"],
                        e["number"] or "", e["caper"], e["caperAddr"], e["rsa"], e["basis"], e["status"]])
    print(f"\n  Full mapping → {CSV_OUT}")

    # Confident aliases: Truno IC name (and code) → the canonical RSA store, when known.
    auto = {}
    for e in matched:
        canon = e["rsa"].split(" | ")[0] if e["rsa"] else e["caper"]
        if canon:
            for k in (e["trunoIC"], e["trunoCode"]):
                if k:
                    auto[k] = canon

    confirmed = {}
    if uncertain:
        why = {"no_address": "no address on the Truno row",
               "no_caper_match": "address not found in the Caper sheet",
               "ambiguous": "address maps to several Caper stores",
               "matched_no_rsa": "net-new (not in RSA yet)",
               "rsa_ambiguous": "matches several RSA stores"}
        print(f"\n  ⚠ {len(uncertain)} need confirmation:")
        for e in uncertain:
            print(f"    • {e['trunoIC'] or e['trunoCode']}  [{e['trunoAddr'] or 'no address'}]  — {why.get(e['status'], e['status'])}")
            if args.interactive:
                ans = input("        → canonical RSA store name (blank = skip): ").strip()
                if ans and e["trunoIC"]:
                    confirmed[e["trunoIC"]] = ans

    _save_aliases({**auto, **confirmed})
    print(f"\n  Saved {len(auto) + len(confirmed)} alias(es) to {OVERRIDE_FILE}.")
    if uncertain and not args.interactive:
        print("  Re-run with --interactive to map the flagged stores, or edit store_map.json directly.")
    print("=" * 100)


if __name__ == "__main__":
    main()
