# RSA Agent — Setup Guide

A complete operations automation system for RSA W&M cart tracking. Ops managers submit carts via Slack; the agent handles the full workflow automatically.

---

## Architecture Overview

```
Ops Manager (Slack) — text message or file upload
       ↓
  RSA Agent (Python Claude agent)   ← rsa_agent.py
       ↓  Claude tool_use loop
       │  Claude reasons about what to do and calls tools in sequence.
       │  No hard-coded routing — Claude decides the workflow.
  Google Apps Script        ← Code.gs + GAS_BotEndpoints.gs + GAS_Automation.gs
       ↓
  ┌─────────────────────────────────────────────────────┐
  │  Step 1: RSA Tracker (Google Sheet)                 │
  │  Step 2: TRUNO RSA Tracker (separate Sheet)         │
  │  Step 3: Email Nicole (GmailApp)                    │
  └─────────────────────────────────────────────────────┘
       ↓  Time-based triggers
  Auto-monitoring (every 2–4 hrs):
  ├── monitorTrunoTracker  → syncs Truno status → RSA Tracker
  ├── ETS Coffee Break reminder → ETS Slack channel
  ├── checkCompletedVisits → CSM W&M notification
  └── ingestInspectionFiles → Drive folder → dashboard
```

---

## Part 1 — Google Apps Script Setup

### 1.1 Add the new files

In your existing Apps Script project (`script.google.com`):
1. Click **+** next to Files → **Script**
2. Create `GAS_BotEndpoints` — paste contents of `GAS_BotEndpoints.gs`
3. Create `GAS_Automation` — paste contents of `GAS_Automation.gs`

### 1.2 Configure constants

In `GAS_BotEndpoints.gs`, update:
```javascript
const AGENT_SECRET    = 'your-strong-secret-here';   // must match .env AGENT_SECRET
const NICOLE_SLACK_ID = 'U0XXXXXXXXX';               // Nicole's Slack user ID
const NICOLE_EMAIL    = 'nicole@truno.com';           // Nicole's real email
```

In `GAS_Automation.gs`, update:
```javascript
const INSPECTION_FOLDER_ID = '1nXWekoXow3-az02QHfVSx7PJQP4uA4vx'; // already set
const ETS_SLACK_CHANNEL_ID = 'YOUR_ETS_CHANNEL_ID';  // if different from W&M channel
```

Verify the Truno column layout in `GAS_BotEndpoints.gs` (`TRUNO_COL`) matches your actual Truno spreadsheet.

### 1.3 Re-deploy the Web App

1. **Deploy → Manage Deployments → Edit** (pencil icon on your existing deployment)
2. Change **Version** to **New version**
3. Ensure **Who has access** = `Anyone` (the bot authenticates via AGENT_SECRET)
4. Click **Deploy** → copy the new Web App URL

### 1.4 Install automation triggers

In the Apps Script editor:
1. Select function **`createAutomationTriggers`** from the dropdown
2. Click **Run** (authorize if prompted)

This installs:
- `masterDailyRun` — 9am daily (alerts + archive + Truno monitor + file ingestion)
- `monitorTrunoTracker` — every 2 hours
- `ingestInspectionFiles` — every 4 hours

### 1.5 Grant Drive access

The script needs access to read from the inspection folder. When `ingestInspectionFiles` runs for the first time, Apps Script will prompt for Drive authorization in the execution log.

---

## Part 2 — Slack App Setup

### 2.1 Create Slack App

1. Go to https://api.slack.com/apps → **Create New App → From scratch**
2. Name it `RSA Agent`, select your workspace

### 2.2 Enable Socket Mode

1. **Settings → Socket Mode** → toggle **Enable Socket Mode**
2. Create an App-Level Token with scope `connections:write` → copy the `xapp-` token

### 2.3 OAuth & Permissions

Add these **Bot Token Scopes**:
- `chat:write` — post messages
- `reactions:write` — add emoji reactions
- `users:read` — get user names for traceability
- `app_mentions:read` — respond to @mentions
- `im:read`, `im:write` — direct messages

Install to workspace → copy the `xoxb-` Bot Token.

### 2.4 Event Subscriptions

Under **Event Subscriptions → Subscribe to bot events**, add:
- `app_mention`
- `message.im`

### 2.5 Add Bot to the W&M Channel

In Slack: `/invite @RSA Agent` in your `#wm-alerts` channel (or whatever channel ops managers use).

---

## Part 3 — Python Claude Agent Setup

```bash
cd "RSA Agent"
cp .env.example .env
# Fill in all values in .env

pip install -r requirements.txt
python rsa_agent.py
```

For production (keep running after terminal closes):
```bash
pip install supervisor
# or simply use screen / tmux:
screen -S rsa-agent
python rsa_agent.py
# Ctrl+A, D to detach
```

---

## How It Works — Workflow Reference

### New Cart Submission (Full Workflow)

Ops manager types in Slack (DM or @mention):
```
New cart P_WF_SHOPRITE_72_M3_042 at ShopRite Forest Hill NJ, trigger Launches
```

Agent executes automatically:
1. ✅ Adds cart to RSA Tracker (Google Sheet) with RSA status = Pending RSA
2. 📅 Creates row in TRUNO RSA Tracker (status Pending, store address filled in; date left blank for Truno to schedule)
3. 📧 Emails Nicole at TRUNO with inspection details (cart #, store, address)
4. Posts Slack confirmation with links to both trackers

### Status Queries
```
What's the status of P_WF_SHOPRITE_72_M3_001?
Show all pending RSA in NJ
Show stale carts for Sai
Give me a summary
```

### Status Updates
```
Cart NJ-001 passed W&M today
Update cart 42 at ShopRite — RSA status Scheduled RSA, visit date 6/20
Mark P_WF_001 as Completed RSA
```

### Automated Monitoring (runs in background)

| Trigger | Action |
|---------|--------|
| Truno status → "Scheduled" | Updates RSA Tracker, sets ETS date, posts Coffee Break reminder |
| Truno status → "Completed" | Updates RSA Tracker to Completed RSA, emails + Slack-notifies CSM team |
| Truno status → "Cancelled" | Resets to Pending RSA, alerts DRI |
| New file in Drive folder | Parses inspection data, updates tracker, adds tech upload link (col T) |
| RSA Completed > 3 days, W&M still Pending | Reminds CSM team to schedule W&M |

---

## File Summary

| File | Purpose |
|------|---------|
| `Code.gs` | Original webapp (unchanged) |
| `GAS_BotEndpoints.gs` | Add to GAS: doPost API, Truno scheduling, full onboard workflow |
| `GAS_Automation.gs` | Add to GAS: monitoring triggers, file ingestion, auto-notifications |
| `rsa_agent.py` | Python Claude agent (tool_use loop + Slack listener) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment variable template |

---

## Troubleshooting

**"Unauthorized" from GAS**: Check that `AGENT_SECRET` in `.env` matches `AGENT_SECRET` in `GAS_BotEndpoints.gs`, and that you redeployed after changing it.

**Bot not responding in channel**: Make sure you ran `/invite @RSA Agent` in the channel. Socket Mode bots don't need a public URL.

**Truno tracker not updating**: Check `TRUNO_COL` constants in `GAS_BotEndpoints.gs` match your Truno sheet's actual column order.

**File ingestion not picking up files**: Confirm the Drive folder ID in `GAS_Automation.gs` matches the folder URL (`1nXWekoXow3-az02QHfVSx7PJQP4uA4vx`). The script only processes `.xlsx`, `.xls`, and `.csv` files.

**Email not sending**: Apps Script must have Gmail authorization. Run `ingestInspectionFiles()` manually once from the editor and accept the permission prompt.
