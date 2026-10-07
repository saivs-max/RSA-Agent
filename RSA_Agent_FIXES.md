# RSA Agent — Correctness, Data & Functional Fixes

**Date:** June 23, 2026  **Scope:** the `Bug` / `Logic` (correctness, data-integrity, functional) defects from the QA report, fixed in priority order. Security, reliability/locking, and UX/accessibility findings were **not** in this pass (see *Still open* below).

## Test status after fixes

- **Offline suites: 342 / 342 pass** — `test_offline` 179, `test_storemap` 13, `test_reconcile` 59, `test_tech_offline` 91 (0 failures).
- **All Python compiles**; **all 4 Apps Script files + the webapp inline JS pass syntax checks.**
- Each logic fix was re-verified by executing the real function with adversarial inputs.
- **Live caveat:** the sandbox can't reach Google/Sheets/Drive/the GAS endpoint, so the Apps Script (`.gs`) changes are syntax-verified and logic-reviewed but still need a **deploy-time smoke test** (run `monitorTrunoTracker`, `checkCompletedVisits`, `ingestInspectionFiles` once and eyeball the dashboard).

---

## Fixed (14 defects: 8 High, 6 Medium)

| ID | Sev | File | What changed |
|----|-----|------|--------------|
| **FUNC-01** | High | `rsa_sheets.py` `_find_row` | Exact Cart-ID match wins; a **bare number** (`"1"`) now matches only a serial's `_M3_<num>` suffix and must be pinned by the store hint or be unique — otherwise returns `None`. Kills the wrong-cart-write bug where `"1"` substring-matched `…_M3_001` of an unrelated store. Verified: ambiguous `"1"`→None; `"1"+Kroger`→correct; `"10"`→correct; full serial→exact. |
| **FUNC-02** | High | `drive_monitor.py` `_match_file` | File→store match now rejects a same-number file that **names a different chain** (e.g. `Davis 350` no longer grabs `McKeevers 350….pdf`), while still accepting the store's own named file and neutral bare-number/city-named files. Verified across 6 scenarios. |
| **FUNC-03** | High | `GAS_Automation.gs` ingest | Removed the bare `'p'` pass-keyword and added an **ambiguous class**: `pending / partial / postponed / problem / in-progress / review / tbd / incomplete` are no longer auto-passed **nor** mislabeled `Failed` — the W&M status is left unchanged. Explicit single-letter `P`/`F` still classify correctly. |
| **FUNC-04** | High | `GAS_Automation.gs` `checkCompletedVisits` | CSM nudge now fires **only** when W&M is empty/`Pending W&M`; `Failed W&M` (and Scheduled/Passed/Completed) are excluded — failed carts are no longer re-nudged forever. |
| **DATA-01** | High | `rsa_reconcile.py`, `GAS_BotEndpoints.gs` | Unknown state is now written **blank + flagged "state UNKNOWN — left blank for review"** instead of silently defaulting to `NJ`; `_botFullOnboardCart` no longer hard-codes vendor `TRUNO` (respects the caller). |
| **DATA-02** | High | `store_map.py` `state_from_address` | Re-ordered precedence to **ZIP → 'OH 45040' → trailing code → spelled-out name (last, locality-field only)**. Fixes confidently-wrong states: `Washington, DC`→DC (was WA), `100 Indiana St, Chicago IL`→IL (was IN), `Nevada, MO`→MO, `Delaware, OH`→OH. |
| **VIS-01** | High | `Code_getTrackerData.gs` | A cart is "done/hideable" **only when `Overall = Passed`** — `Completed RSA`/`Passed W&M` alone no longer hide a cart, so Completed-RSA + Pending-W&M carts stay visible (were dropped from the dashboard after 30 days). |
| **BUILD-01** | High | `caper_pdf_to_csv.py` | Fixed the `SyntaxError` (added `import sys`, unquoted line 2) and changed the hard-coded foreign output path to `CAPER_STORES_CSV`/CWD. The file now compiles and writes `caper_stores.csv` locally. |
| **ROUTE-01** | Med | `rsa_agent.py` `_route_upload` | A W&M **scale-test form saved as `.xlsx`** now routes to the W&M-test path (filename `scale test`/`w&m` overrides the "spreadsheet ⇒ tech" default). Calibration spreadsheets still route to tech; explicit captions still win. |
| **COL-02** | Med | `rsa_sheets.py` `_tech_cols` | Gap counters now **resolve the real "Tech Results" header column read-only** before counting (was: blindly used the V/W defaults), falling back to V/W only on error. |
| **MARK-01** | Med | `drive_monitor.py` | "Forms sent = Yes but no form found" now re-warns **once per store per week** instead of exactly once ever — a persistently-missing form is no longer forgotten. |
| **DAYS-01** | Med | `GAS_Automation.gs` CSM alert | The "(Nd)" figure now uses **real days-pending** (from `Last Updated`) instead of unrelated column P (which rendered as `NaNd`). |
| **IDEMP-02** | Med | `GAS_Automation.gs` `monitorTrunoTracker` | The idempotency key now includes the **visit date**, so a schedule→cancel→**re-schedule** to a new date is treated as a new event and the ETS Coffee-Break reminder fires for the real date. |
| **UX-02** | Med | `Index.html`, `GAS_AddCartManual.gs` | "Add Cart" now actually **persists Overall Status and DRI** — the webapp payload was missing both and the GAS backend dropped DRI; both add and update paths now carry them. |

---

## Verified, then *not* changed (rationale)

- **ZIP-01 — withdrawn (false positive).** The "missing" prefixes (428, 269, 578, 589, 694, 848…) are **unassigned USPS ranges** (KY ends at 427, WV at 268, SD at 577, etc.). `state_from_zip` returning `None` is **correct** — adding mappings would have *introduced* bugs.
- **STATE-DUP — left as-is.** `rsa_reconcile._state_from_addr` is a separate, **ZIP-anchored** parser (no spelled-out-name pass), so it never had the DATA-02 bug; merging the two parsers is a maintainability nicety with real regression risk against the reconcile test suite.
- **ADDR-01 (plaza street#+ZIP collision)** and **IDEMP-01 (reconcile duplicate-cart edge)** — deferred. Both are narrow edge cases; the candidate fixes risk reducing legitimate Caper joins / tripping the extensive reconcile dedup tests. Recommend pairing with live data before changing.
- **ENC-01 (CSV charset + locale date parsing in GAS)** — deferred; it's a multi-point change in the ingest path that needs the Apps Script runtime + real files to validate safely.
- **VENDOR-01 — accepted.** `_norm_vendor` returning an unrecognized vendor name verbatim is acceptable (a real vendor like "DUMAC" passes through); not a correctness defect.

---

## Still open (not part of this "correctness/data/functional" pass)

These remain in `RSA_Agent_QA_Report.docx` / `RSA_Agent_QA_Defect_Log.xlsx` and should be tackled next — the **Critical security items first**:

- **Security (Critical/High):** rotate `AGENT_SECRET` + the key in `.env.example` and confirm the Web App is domain-restricted (SEC-01/02); escape dashboard output (XSS, SEC-03); add a bot authorization allowlist (SEC-04); CSV/email injection (SEC-05/06).
- **Reliability:** `LockService` + atomic `processed_*.json` for the 4 concurrent writers (REL-01); de-duplicate overlapping triggers (REL-02); fix the binary-`.xlsx` ingest path (REL-03); add retries/timeouts (REL-05).
- **UX / a11y / responsive** webapp items.

---

## Follow-up enhancement — compliance loose-match picker (June 23, 2026)

Reported separately: a compliance lookup for a store like `bigbunny-1` returned no options even though the guide has **Big Bunny** and **Big Bunny Market**. Fixed:

- **`rsa_sheets.compliance_candidates()`** (new) — ranked loose matches by nickname / retailer / city, tiered so an exact match (incl. the store number) wins, then a number-dropped exact, then substrings, then shared words. State-optional (searches all states when none is given).
- **`lookup_compliance()`** — now tries an **exact** nickname/retailer match before the looser substring pass (so picking *Big Bunny Market* no longer resolves to *Big Bunny*), and attaches `candidates` when there's no confident single match.
- **`rsa_agent` Slack flow** — when ≥2 stores plausibly match (and none is a unique exact), the bot posts a **"Which store did you mean?" picker** with one button per candidate; clicking it shows that store's pre/post requirements. A unique numbered match (e.g. `ShopRite 113`) still answers directly; a single close match is used automatically.

Verified end-to-end offline (`bigbunny-1` → both stores offered; exact pick resolves the right row; `ShopRite 113` stays specific) and all 342 offline tests still pass.

## Production hotfix — Truno monitor flood, date-carts & notification links (June 23, 2026)

After deploying, the Truno monitor sent a burst of duplicate "TRUNO Visit Cancelled" notices and reset many carts to Pending RSA. Root cause + fixes (all `GAS_Automation.gs`):

- **Re-fire flood (regression from IDEMP-02).** That earlier fix appended the visit date to the dedup key (`rowSig`); on deploy the key shape changed, so every Truno row looked new and re-fired. **Reverted** the key to `store|cart|status` and **bumped the baseline flag** (`trunoBaselineV2`→`V3`) so the next run re-snapshots all current rows as already-handled and only genuinely new transitions fire afterward. The reschedule case (IDEMP-02's goal) is now handled safely by **clearing the 'scheduled' key when a cancellation is processed** — no cache-invalidating key change.
- **"Cart" cell containing a date.** A Truno row (e.g. Sprouts 917) had a date in the Cart column, so no cart number could be parsed and it nagged "needs confirmation." Added a **guard that skips rows whose Cart cell is a date** (logs once; no status reset, no DRI ping). The *store* matched fine — the monitor identifies the store via the Truno **code** (`ISC0050-SFM-0917` → 917), the same canonical identity the address/Caper mapping resolves to; the address join itself is Python-side and isn't callable from Apps Script.
- **Deep link.** The cancellation notice now has an **"Open cart in RSA Tracker →"** button (`_webAppUrl(cartId)` → `…/exec?cart=<id>`).

**Deploy note:** re-paste `GAS_Automation.gs` and redeploy. The first `monitorTrunoTracker` run after deploy is a silent re-baseline (no notifications); genuine new Truno transitions notify normally afterward.

## How to re-verify

```bash
cd "RSA Agent"
python3 tests/test_offline.py        # 179 passed
python3 tests/test_storemap.py       # 13 passed
python3 tests/test_reconcile.py      # 59 passed
python3 tests/test_tech_offline.py   # 91 passed
python3 -c "import py_compile,glob;[py_compile.compile(f,doraise=True) for f in glob.glob('*.py')]"  # all compile
# Apps Script: paste the .gs files and run monitorTrunoTracker / checkCompletedVisits /
# ingestInspectionFiles once, then open the dashboard to confirm at-risk carts stay visible.
```
