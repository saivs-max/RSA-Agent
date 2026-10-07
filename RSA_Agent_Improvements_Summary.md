# RSA Agent — Improvements Summary

*Prepared for the TL & team · July 1, 2026*

## TL;DR

Over this round of work we hardened the RSA Agent across three fronts: **store mapping/naming**, **Truno auto-scheduling accuracy**, and **reliability**, and we continued migrating automation off Google Apps Script into the Python bot. We also retired W&M from the workflow and cleaned up the stale-RSA backlog. Everything is covered by **417 passing automated tests**.

Headlines:
- Store lookups are now validated against the authoritative **Deployed Stores** directory (148 live stores) and mapped to a full address that flows into the Truno tracker automatically.
- Stores are now identified by **banner + store number** (e.g. `ShopRite 153`), the way ops actually refers to them.
- Fixed the Truno matching bug that made correctly-scheduled carts look "unmatched," and pointed all links at the **webapp** instead of the raw sheet.
- The AI-gateway calls no longer hang or spam stack traces when the network/VPN drops.
- **W&M is fully out of scope** — reminders off, hidden from all views, and a passed RSA now closes a cart as *Passed*.
- Scheduling reminders and backlog updates now come **from the RSA Agent**, not the external Google-docs automation.

---

## 1. Store mapping is authoritative and self-serve-safe

The Deployed Stores export is now the primary source of truth for the store directory (148 stores), replacing the old, drift-prone Caper number/name matching (kept only as a fallback).

- When onboarding via Slack, the bot **only accepts a real deployed store**, identified by its store name, nickname, external store ID, or retailer. Typos and unknown stores are rejected with suggestions; a retailer (e.g. "shoprite") or a duplicate name prompts the user to pick the exact store.
- Once a store is identified, its **full address (Street, City, State ZIP)** is written to the Truno tracker automatically when service is requested — no more manually chasing addresses, and the Truno↔store join is exact.
- Dropping a newer `Deployed Stores *.csv` into the project folder refreshes the directory with no code changes.

## 2. Store naming: banner + store number

Ops refers to Wakefern stores as "ShopRite 153," but the data kept that number in a different field than the store's name. We added a computed **`banner_alias`** = `<Banner> <External Store ID>` and made it the preferred name.

- `ShopRite 153`, `Kroger 250`, `Weis 226`, `Sprouts 503`, `Price Chopper 200`, etc. — **132 of 148 stores** now carry this name; the 16 exceptions (Kroger + a few with compound/slug IDs) safely fall back to their location name.
- A store is now findable **every way it might be typed**: banner + number (`ShopRite 153`), external ID (`153`), location nickname (`Northvale`), internal id (`wakefern-23`), or full store id.
- The banner name is carried into the Truno tracker alongside the address, so downstream records read consistently.

## 3. Truno auto-scheduling: correct matches, clearer messages

Fixed a real matching bug surfaced by the Weis 226 case, where a cart that *was* scheduled showed up as "couldn't match."

- A cart the bot already matched is **never re-offered as a candidate** for another cart on the same row.
- A Truno cart number with no corresponding RSA cart is reported plainly as **"no RSA cart found"** instead of being padded with unrelated serials.
- Matched carts are shown as **already handled**, so they don't read as failures.
- All confirmation links now deep-link into the **webapp**, not the raw spreadsheet.
- Fixed in the Python monitor (the go-forward system) and mirrored into the legacy Apps Script.

## 4. Reliability: the AI gateway no longer hangs or floods logs

A gateway connection failure (traced to the VPN dropping — the gateway is an internal-only endpoint) previously hung the bot on the SDK's 10-minute default and dumped a full stack trace.

- All three gateway clients (intent parsing, W&M form OCR, tech form OCR) now have an explicit **timeout + retry budget** (30s / 90s, 3 retries with backoff), so transient blips self-heal.
- Connection/timeout errors are caught distinctly: a concise log line and a plain **"couldn't reach the backend — try again"** message to the user, instead of a stack trace.

## 5. W&M retired; RSA completion simplified

W&M (weights & measures inspection) is no longer in scope.

- **W&M scheduling reminders are off**, and W&M is hidden from the bot's summaries, cart lookups, lists, and status roll-ups.
- A passed/completed RSA now closes the cart as **Overall = Passed** (previously "Pending W&M"), across the results pipeline and the reconcile flow.
- The W&M columns and historical data remain in the sheet — nothing was deleted — they're just no longer surfaced or acted on.

## 6. Reminders & backlog cleanup moved off Google Apps Script

- New **`rsa_reminders.py`** generates RSA-only scheduling reminders (stale > 13 days, grouped by store, "schedule with Winter Scales…") from the Python bot on a daily cadence — replacing the external "RSAAlert" script that produced the reminder flood.
- New **`rsa_bulk_complete.py`** reconciles the Winter Scales stale-RSA backlog: it marks the reminder carts **Completed RSA / Overall Passed**, but only those with **no update in more than 13 days** (so recently-touched carts are left alone). Dry-run by default; on the current backlog it updates 64 carts and correctly skips the one that was only 10 days stale.

---

## Quality & testing

- **417 automated offline tests passing** across the five suites (dispatch/onboarding, reconcile, Truno monitor, store map, tech pipeline). Every change above shipped with targeted tests — including the exact Weis-226 scenario, the gateway connection-error path, the banner/internal-id matching, the 13-day bulk gate, and the RSA-only reminder output.
- Tests run with no secrets, no network, and no writes to any live sheet, so they're safe to run in CI or locally.

## What this means operationally — action items

1. **Turn off the external "RSAAlert" Apps Script trigger** — the reminder flood comes from there; the Python agent now owns RSA reminders.
2. **Disable the RSA-Tracker Apps Script automation trigger** (`masterDailyRun`) now that Truno monitoring + reminders run in Python, to avoid double writes/notifications.
3. **Run the backlog cleanup**: `python3 rsa_bulk_complete.py` (preview) → `--apply` to mark the 64 stale carts Completed RSA / Passed.
4. **VPN**: the AI gateway is internal-only — the bot needs the corporate VPN/network to parse messages and read forms.

## Open items / recommendations

- **Kroger naming**: Kroger's external IDs are compound and its Truno numbering is inconsistent, so those stores stay on their location name for now. If ops keys Kroger by internal store # ("Kroger 11"), we can switch it — a small, isolated change.
- **Reminder timing**: the daily RSA reminder can be pinned to a specific time (e.g. 9:00 AM) to match when RSAAlert used to fire.
- **Continue the GAS → Python migration** per the migration plan (CSM nudges, daily report) so all automation lives in one always-on process.
