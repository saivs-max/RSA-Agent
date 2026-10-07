# RSA Agent — Demo & Validation Checklist

A run-through of every behavior we built. **Writes use a throwaway demo store** so the real
478-row tracker stays clean; **reads run against real data** so they look real. Each item is:
**say this → expect this → ✅ verify**. DM the bot or @mention it in a channel.

> Demo store used throughout: **`DEMO MART, Newark NJ`** — carts **1** and **2**.

---

## ⚙️ Pre-demo setup (do this first — ~5 min)

- [ ] **Bot running from the project folder:** `python3 rsa_agent.py`
      (startup logs should show the model, `RSA Test Results column: {...}`, and the drive monitor starting.)
- [ ] **Point demo pings at yourself**, not live ops. In `.env`:
      `CSM_MENTION=<@your-slack-id>` and `RSA_MONITOR_CHANNEL=<a test channel id>`, then restart the bot.
- [ ] **Pause the Nicole email trigger** so a demo onboarding doesn't email Truno: in Apps Script →
      Triggers, disable **`emailNicoleForNewTrunoRows`** (re-enable after the demo). The bot will still
      *say* "Nicole emailed automatically" — that's the normal message; the email just won't fire while paused.
- [ ] **Decide how to show automated Drive ingestion** (the folder has ~45 real forms):
      - To demo cleanly with **one** form, pre-mark the existing backlog as processed so only a *new* file triggers a write:
        ```bash
        python3 -c "import drive_monitor as d,json; ids=[f['id'] for f in d._list_folder_files()]; json.dump(ids,open(d.PROCESSED_FILE,'w')); print('pre-seeded',len(ids),'files')"
        ```
      - Or skip pre-seeding and let the backlog process once — it writes **real, accurate** results for every Forms-sent=Yes store.

---

## Part 1 — Interactive Slack commands

### 1.1 Help / capabilities  *(read-only)*
- [ ] Say: **`hi`**  (or `what can you do?`)
- Expect: the "RSA Agent — what I can do" capability list.

### 1.2 Compliance lookup  *(read-only, real store)*
- [ ] Say: **`what's required for ShopRite, Newark NJ?`**
      *(use any store + state you know is in the Compliance Guide; WF / Sprouts / Kroger locations work too.)*
- Expect: 📜 **RSA / W&M requirements** — a **Pre-launch** directive and a **Post-launch** directive, plus any
      ⚠️ flags (e.g. "Confirm Locally", NY county exception). If it says *"matched by state only"*, that store
      isn't in the guide by name — still a good thing to show (it's the safe fallback).
- Talking point: *only* pre/post requirements surface, matched by store + address — no raw data dump.

### 1.3 Onboard carts — service trigger required  *(WRITE → demo store)*

**1.3a — let the bot ask (shows the picker):**
- [ ] Say: **`onboard carts 1 and 2 at DEMO MART, Newark NJ`**  *(no trigger)*
- Expect: the bot asks **"What's the service trigger?"** with **buttons** for the dashboard options
      (Launches, Cart replacement, Load cell recal, W&M Failed, …). **Nothing is added yet.**
- [ ] Click a trigger (e.g. **Cart replacement**).
- Expect: the message updates to 🛒 **2 carts** → `P_DEMO_MART_NEWARK_NJ_M3_001` / `_002` (added),
      trigger shown, **RSA status: Pending Truno Scheduled** (no date), 📋 submitted to TRUNO, 📧 Nicole emailed to schedule, + compliance directive.

**1.3b — or state the trigger inline (one shot):**
- [ ] Say: **`onboard carts 1, 2 at DEMO MART, Newark NJ — trigger Cart replacement`** → same result, no prompt.

- ✅ Verify: two **RSA Tracker** rows with **RSA Status = Pending Truno Scheduled**, the **trigger** filled in, and **no RSA Scheduled Date**;
      one **TRUNO → Sheet1** row with status **Pending**, blank date, and an `[RSA Agent · …]` trace in Notes.
- Talking points: cart **numbers** only → serials generated; the **trigger is mandatory** (the bot won't add without it);
      **no date is invented** — it stays *Pending Truno Scheduled* until Nicole schedules.

### 1.3c Nicole schedules → tracker auto-updates  *(the handoff)*
- [ ] In the **TRUNO** sheet, set that DEMO MART row's **Status → Scheduled** and add a **date** in the visit column (simulating Nicole).
- [ ] Trigger the monitor now (Apps Script → run **`monitorTrunoTracker`**), or wait for its 2-hour run.
- Expect: the RSA Tracker rows flip **Pending Truno Scheduled → Scheduled RSA** with **that date** + vendor TRUNO, and an ETS Coffee-Break reminder posts.
- ✅ Verify: `status of cart 1 at DEMO MART` → **Scheduled RSA** with the date.
- Talking point: the scheduled date is **never guessed** — it comes straight from Truno, written back automatically.

### 1.4 Query a cart  *(read-only)*
- [ ] Say: **`status of cart 1 at DEMO MART`**
- Expect: a status card — RSA / W&M / Overall status, DRI, last updated.

### 1.5 Update status + notes  *(WRITE → demo cart)*
- [ ] Say: **`cart 1 at DEMO MART — set W&M to Scheduled W&M, note: W&M booked for Tue`**
- Expect: ✏️ *updated* with the changed fields listed.
- ✅ Verify: the field changed, the note is **appended** (old notes kept), Last Updated = today.

### 1.4 Query a cart  *(read-only)*
- [ ] Say: **`status of cart 1 at DEMO MART`**
- Expect: a status card — RSA / W&M / Overall status, DRI, last updated.

### 1.5 Update status + notes  *(WRITE → demo cart)*
- [ ] Say: **`cart 1 at DEMO MART — set RSA to Scheduled RSA, vendor TRUNO, note: tech booked for Tue`**
- Expect: ✏️ *updated* with the changed fields listed.
- ✅ Verify: RSA Status = **Scheduled RSA**, the note is **appended** (old notes kept), Last Updated = today.

### 1.6 Filtered list  *(read-only)*
- [ ] Say one of: **`show pending RSA carts in NJ`** · **`stale carts`** · **`carts for <a DRI name>`**
- Expect: a filtered list with a total count and per-cart status lines.

### 1.7 Summary dashboard  *(read-only)*
- [ ] Say: **`give me a summary`**
- Expect: overall counts (pending/scheduled/completed RSA, pending/passed W&M, needs-action, stale) + breakdown by state.

---

## Part 2 — Form parsing (the differentiator)

### 2.1 Preview a real form — *read-only, zero writes* (good warm-up)
- [ ] Run: `python3 tests/check_live.py parse`
- Expect: the newest form's extracted JSON — store, service date, and each cart's **Pass/Fail**. Confirms the
      vision model reads these mixed scans/PDFs correctly *before* anything is written.

### 2.2 Upload a form in Slack  *(WRITE → real cart, accurate result)*
- [ ] Download a form from the **Test results** folder (e.g. `Shoprite 0153 - 26.06.05.pdf`), then drag it into
      the bot DM with a note like *"results attached"*.
- Expect: bot parses it → *"wrote N/N cart result(s)"*; a **Pass** sets **Completed RSA** + next step **Pending W&M**
      and **tags the CSM**; a **Fail** sets **Failed RSA**.
- ✅ Verify: that store's cart(s) show the new RSA status, **col U (RSA Test Results)** has the Pass/Fail text, and
      **col T** has the report link.
- Note: this writes a real (accurate) result. If you'd rather not, demo with 2.1 + 2.3 instead.

### 2.3 Automated Drive ingestion  *(WRITE → real carts)*
- [ ] (Backlog pre-seeded in setup.) Drop a new form into the **Test results** folder — or re-add one renamed.
- [ ] Trigger the scan now instead of waiting for the 5-min poll:
      ```bash
      python3 -c "import drive_monitor as d; d.process_confirmed_forms(notify=print)"
      ```
- Expect: a line per store — *":page_facing_up: <file> — <store>: wrote N/N cart result(s)"*, Passed carts → *Pending W&M*,
      CSM tagged — and the same posted to your monitor channel.
- ✅ Verify: the matching cart(s) updated in the tracker.
- Talking point: only fires for stores marked **Forms sent = Yes** in the Truno tracker, matched by store number, and **only new files** (idempotent — re-running writes nothing).

---

## Part 3 — End-to-end story (the demo spine)

Walk one store start to finish — plain English the whole way:

1. **Guardrails** → `what's required for ShopRite, Newark NJ?` → pre/post requirements appear.
2. **Onboard** → `onboard carts 1, 2 at DEMO MART, Newark NJ` → bot asks the **service trigger** → click one → added as **Pending Truno Scheduled**, submitted to TRUNO, Nicole emailed.
3. **Check** → `status of cart 1 at DEMO MART` → *Pending Truno Scheduled* (no date yet).
4. **Nicole schedules** → set the Truno row to **Scheduled** + a date → run `monitorTrunoTracker` → tracker flips to **Scheduled RSA** with that date.
5. **Inspection result** → `cart 1 at DEMO MART passed its RSA inspection today` → **Completed RSA**, next step **Pending W&M** *(or upload that store's real form as in 2.2)*.
6. **Confirm** → `status of cart 1 at DEMO MART` → **Completed RSA / Pending W&M**, CSM tagged.

> One line for the team: *"A request in plain English becomes a tracked cart, a Truno-scheduled inspection, a parsed
> Pass/Fail from the field, and a clean W&M hand-off — and the bot only ever writes dates and results it actually has."*

---

## 🧹 Cleanup (after the demo)

- [ ] **Delete the demo rows.** In **RSA Tracker** delete the rows whose Cart ID starts `P_DEMO_MART`; in **TRUNO →
      Sheet1** delete the `DEMO MART` row. (Manual is safest.) Optional one-liner:
      ```bash
      python3 -c "import rsa_sheets as s; w=s._ws(s.RSA_SPREADSHEET_ID,s.RSA_SHEET); v=w.get_all_values(); [w.delete_rows(i+1) for i in range(len(v)-1,-1,-1) if v[i] and str(v[i][0]).startswith('P_DEMO_MART')]; print('cleaned')"
      ```
- [ ] **Re-enable** the `emailNicoleForNewTrunoRows` Apps Script trigger.
- [ ] If you onboarded the demo before pausing the email trigger, delete the `DEMO MART` Truno row before the next 15-min run.

---

## 📋 Sample-data quick reference

| Use | Data |
|---|---|
| Demo store (writes) | **DEMO MART, Newark NJ** — carts 1, 2 |
| Real stores with forms waiting | Shoprite 153 / 151 / 80 / 116 / 463, Sprouts 558 / 561, Schnucks 129, McKeevers 350 |
| Forms-sent = Yes (auto-ingest) | Sprouts 505 / 507 / 509 / 558 / 561 / 029, GFH2 Santa Monica, WF 77 Roxborough |
| Compliance lookup | any store + state in the Compliance Guide (ShopRite NJ is a safe bet) |

## What each item proves
Onboarding (cart-number → serial, **mandatory trigger prompt**, **Pending Truno Scheduled** with **no invented date**,
TRUNO request + compliance), the **Truno-scheduled → Scheduled RSA + real date** handoff, updates (status + note-append),
query / list / summary (reads), compliance (pre/post only, store/address match), form parsing
(vision → Pass/Fail → Completed/Failed RSA + col U + link + CSM tag) via **both** Slack upload and the
**automated, idempotent** Drive monitor gated on Forms-sent = Yes.
