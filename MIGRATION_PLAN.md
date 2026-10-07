# RSA Agent — Automation & Mapping Migration to the Python Bot

**Goal:** move automation and store/cart mapping off Google Apps Script into the Python bot, with **address as the primary matching key** (via `store_map` + the Caper deployment data) instead of the GAS number/name inference that's been mis-firing.

**Status (June 24, 2026):** Phase 1 (the Truno status monitor — the source of the recent flood) is **built and offline-tested**. Phases 2–3 are scoped below; one decision (email path) is pending.

---

## Why move it

- The mapping that actually works (address → Caper → canonical store) lives in **Python** (`store_map.py`). The GAS `monitorTrunoTracker` re-implements matching with store number/name inference and can't use it — that's the class of bug behind the bad matches.
- Idempotency in GAS uses Script Properties keyed by a string; a key-shape change silently re-fires everything (the flood). Python pollers use an **atomic processed file** with an explicit baseline, which is easier to reason about and recover.
- Consolidating automation in one always-on process (the bot already runs Slack Socket Mode + the Drive/Tech pollers) removes the GAS/Python dual-writer races flagged in the QA report (REL-01/02).

## Target architecture

| Concern | Today | After migration |
|---|---|---|
| Truno status → RSA Tracker | GAS `monitorTrunoTracker` | **Python `truno_monitor.py`** (address-keyed) ✅ built |
| W&M form ingest → results | Python `drive_monitor.py` | unchanged (already Python) |
| Tech calibration ingest | Python `tech_monitor.py` | unchanged (already Python) |
| CSM "pending W&M" nudges | GAS `checkCompletedVisits` | **Python** (Phase 2) |
| ETS coffee-break reminder | GAS (inside monitor) | **Python** (folded into `truno_monitor`) |
| Results-gap / daily report | GAS `masterDailyRun` + Python gap report | **Python** (Phase 2) |
| Store/cart mapping | GAS `_matchTrunoRow` + Python `store_map` | **Python `store_map` only** (address-first) |
| Outbound email (Nicole / Winter Scales / CSM) | GAS `GmailApp` | **pending decision** (see below) |
| Web app UI + dashboard reads | GAS `Index.html` / `Code_getTrackerData.gs` | **stays in GAS** (it's a GAS-served UI) |
| `doPost` write endpoints | GAS `GAS_BotEndpoints.gs` | retire after the bot owns writes (the bot already writes via gspread) |

## Address-as-primary-key design (implemented in `truno_monitor.py`)

For each Truno row, resolve the store number in this order:
1. **Address (primary):** `store_map.addr_key(Truno col E)` → Caper deployment row → canonical store **number**.
2. Store **code** fallback: `ISC0050-WKF-0113` → `113`.
3. Store **name** fallback: a number in the name.

Then match the RSA cart by the serial's `_<store#>_M3_<cart#>[letter]` segment, **unique (store#, cart#) only** — anything ambiguous is flagged for a human, never guessed. Cart cells that are dates (bad/shifted rows) are skipped.

## Phase 1 — Truno monitor (DONE, offline-tested)

`truno_monitor.py` + `tests/test_truno_offline.py` (21 cases, all green). Handles Scheduled (→ Scheduled RSA + visit date + TRUNO + ETS reminder), Cancelled (→ Pending RSA, clear date, ping DRI with a tracker deep-link); Completed stays owned by the form pipeline. Atomic processed file, baseline-on-first-run, cancel-clears-scheduled-key (so reschedules re-fire). Started as a poller from `rsa_agent.py`.

**Cutover steps:**
1. In Apps Script → Triggers, **disable `monitorTrunoTracker`** (leave the code; just stop the trigger).
2. Start/restart the bot: `python3 rsa_agent.py`. On first launch the Truno monitor **baselines silently** (snapshots current rows, no alerts), then only new transitions notify.
   - Or run a one-off baseline first: `python3 truno_monitor.py --baseline`.
3. Optional: `python3 truno_monitor.py --backfill` to act on every actionable row once.

## Phase 2 — CSM nudges, ETS, gap/daily report (next)

Port `checkCompletedVisits` (CSM "Completed RSA but W&M still pending > 3 days") and the daily results-gap report into Python (reuse `rsa_sheets.results_completeness` / `carts_missing_results`, already built). Disable the GAS `masterDailyRun` / `checkCompletedVisits` triggers at cutover.

## Phase 3 — Email + endpoint retirement

Move the onboarding/Winter-Scales/CSM emails off GAS once the email path is chosen, then retire the `doPost` write endpoints (the bot already writes via gspread). Keep `Index.html` (the web app) and the dashboard reads in GAS.

## ⚠ Decision needed — outbound email

Python has no mail transport; today GAS owns Gmail. Options:
1. **Thin GAS mail shim (recommended):** keep one tiny GAS endpoint that only sends mail; Python calls it (the bot already calls GAS). No new infra/credentials.
2. **Send from Python (SMTP/API):** cleaner separation; needs a mail service + credentials.
3. **Slack-only:** replace emails with Slack DMs/@mentions (requires Nicole/Winter Scales in Slack).

Phases 1–2 are **Slack-only and don't need this decision**; it only gates Phase 3.

## Risks & mitigations

- **Double-acting during cutover:** disable the GAS trigger *before* (or same time as) starting the Python monitor, so they don't both write. The Python monitor's baseline prevents a startup flood.
- **Caper freshness:** address matching is only as good as `caper_stores.csv`; regenerate it when deployments change (`caper_pdf_to_csv.py`, now fixed). Falls back to code/name when the address isn't in Caper.
- **No live test here:** Phase 1 is offline-tested; run `--baseline` then watch one real Scheduled/Cancelled transition before trusting it broadly.
