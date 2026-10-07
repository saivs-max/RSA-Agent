# Tech Results pipeline

Parallel to the W&M **Test Results** flow, but for the tech **calibration** step that
happens *before* the W&M test. When a tech calibrates a scheduled cart, their load/shift
readings are captured into a new **Tech Results** column on the RSA Tracker.

Goal: every cart should end up with **both** a Tech Result (calibration) and a Test
Result (W&M pass/fail).

## Source format

The primary document is the **"Weight check checklist" spreadsheet** (`.xlsx`), e.g.
`WF62_ Weight check checklist_20260401.xlsx` — one row per cart with the increasing-load
readings (5 / 25 / 80 lbs), the four shift-test corners (BL/BR/TR/TL), init values, and
the calibration `Y/N` flags. It's parsed **directly** (openpyxl), so readings are exact —
no vision. The store is read from the *Retailer Name* column (e.g. `WF-62` → store 62)
and each row's *Cart No* is matched to a tracker serial by **store + cart number**.

A PDF/photo of a paper calibration sheet is also accepted (parsed via vision as a fallback).

## How readings get in

Both ways link to a cart by **store + cart number** (same matcher as Test Results):

1. **Drive folder** — drop the checklist into the tech-results folder. `tech_monitor.py`
   polls it (handles `.xlsx`, `.csv`, native Google Sheets, and PDF/photo), parses each
   cart's readings, and writes them to the matching cart. Self-healing: a sheet that lands
   before its cart exists is picked up on a later pass. New files are always processed;
   re-applying is idempotent.
   - Folder: `TECH_RESULTS_FOLDER_ID` (defaults to the live folder `1g7myq…UBrHX`).

2. **Slack bot** — upload the file to the bot. Any **spreadsheet** is auto-routed to Tech
   Results (W&M test forms are never spreadsheets). For a photographed calibration sheet,
   add the word `tech` or `calibration` to the message so it isn't mistaken for a W&M form.

## What lands in the column

A compact per-cart readings summary, e.g. (real WF-62 cart 1):

```
Load: 5->5, 25->25, 80->80.01, 25->25.01 · Shift: BL 25.01, BR 25.01, TR 25.01, TL 25.01 · Cal done: N · Cal req: N · Retorque: N · Init before: 201.09
```

Carts whose store+number aren't in the tracker yet are reported as unmatched (they need
onboarding first) rather than silently dropped.

Columns added to the RSA Tracker (auto-created if absent; resolved by header name):
- **V — Tech Results** (the readings summary)
- **W — Tech Results Link** (link to the source file)

Writing a tech result does **not** change RSA/W&M status — calibration is informational
and precedes the W&M test.

## Gap tracking

All carts should have both results. To see what's incomplete:
- Ask the bot: *"which carts are missing tech results?"* / *"carts missing test results"*
  / *"what's missing both?"*
- The tech monitor also posts a **daily completeness report** to `RSA_MONITOR_CHANNEL`
  (cadence: `RSA_GAP_REPORT_SECONDS`, default 24h).
- One-off from the CLI: `python3 tech_monitor.py --gaps`

## Operating

```bash
python3 tech_monitor.py            # one self-healing pass over the tech folder
python3 tech_monitor.py --backfill # re-apply EVERY file (ignores the processed cache)
python3 tech_monitor.py --gaps     # print the results-completeness gap report
```

The monitor also starts automatically in the background when `rsa_agent.py` launches.

### One-time setup
- Share the tech-results folder with the service account **`rsa-agent@rsa-agent.iam.gserviceaccount.com`** (Viewer).
- Set `TECH_RESULTS_FOLDER_ID` in `.env` (already defaulted to the live folder).
- Set `RSA_MONITOR_CHANNEL` to a Slack channel ID to receive monitor posts + the daily gap report.

## Webapp (dashboard)

`Index.html` now shows a **Tech Results** column next to RSA Test Results — in the
Tracker and Archive tables, the cart edit modal, and the CSV export. The cell shows the
readings summary (full text on hover) with a 🔧 link to the source checklist.

The dashboard reads its rows from the Apps Script `getTrackerData()` (and
`getArchiveData()`) in **`Code.gs`**, which isn't in this repo. To populate the new
column, add two fields to that function's row-mapping object (cols V/W are 0-indexed
21/22).

⚠️ **Use the SAME row variable the adjacent `rsaTestResults` / `reportLink` lines already
use** — do NOT paste `row[...]` blindly. If the existing line reads
`rsaTestResults: String(r[20] || '').trim(),`, the loop variable is `r`, so add:

```js
techResults:     String(r[21] || '').trim(),   // V — Tech Results
techResultsLink: String(r[22] || '').trim(),   // W — Tech Results Link
```

If the existing line uses `rowData[20]`, use `rowData[21]`/`rowData[22]`; if `values[i][20]`,
use `values[i][21]`/`values[i][22]`. Referencing a variable name that doesn't exist in that
scope (e.g. `row`) throws `ReferenceError: row is not defined` on page load.

The Slack bot's own query/list endpoints (`GAS_BotEndpoints.gs`) already return these.

## Specifying the type on upload (Slack)

The destination is decided by what you say with the upload:
- say **tech** or **calibration** → Tech Results
- say **rsa**, **test**, or **w&m** → RSA Test Results
- no keyword → a spreadsheet checklist auto-files as Tech Results; anything else as a W&M form.

The explicit keyword always wins (e.g. upload a spreadsheet but say "rsa" → it's filed as a W&M test result).

## Tests
```bash
python3 tests/test_tech_offline.py   # offline checks (no secrets / network / real writes)
```
