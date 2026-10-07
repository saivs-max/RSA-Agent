# RSA Agent on Superblocks — Setup & Migration Guide

## Overview
This guide covers migrating the RSA Agent from fly.io to Instacart's Superblocks instance. Superblocks provides a governed platform for building and deploying internal tools with built-in access control, audit logs, and secret management.

## Prerequisites

### Access & Accounts
- ✓ Instacart Superblocks instance (via Okta)
- ✓ GitHub account with access to https://github.com/saivs-max/RSA-Agent.git
- ✓ Anthropic Console (for API key verification)
- ✓ Slack App (RSA Bot) — already configured

### Credentials & Secrets (from .env)
These must be added to Superblocks **Secrets** manager:

| Secret | Type | Source | Status |
|--------|------|--------|--------|
| `SLACK_BOT_TOKEN` | Secret | Slack App → OAuth & Permissions | ✓ Ready |
| `SLACK_APP_TOKEN` | Secret | Slack App → Basic Info → App-Level Tokens | ✓ Ready |
| `SLACK_SIGNING_SECRET` | Secret | Slack App → Basic Info | ✓ Ready |
| `ANTHROPIC_API_KEY` | Secret | Anthropic Console | ⚠️ ROTATE BEFORE MIGRATION |
| `GAS_WEB_APP_URL` | Secret | Google Apps Script deployment | ✓ Ready |
| `AGENT_SECRET` | Secret | Shared with GAS_BotEndpoints.gs | ✓ Ready |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Secret | sa-key.json (upload as JSON) | ✓ Ready |

---

## Part 1: Create Superblocks Project

### 1.1 Create New Application
1. Click **+ New application** in Superblocks
2. **Name:** `RSA Agent`
3. **Description:** "RSA Operations Agent — Slack bot for cart tracking & W&M automation"
4. **Type:** Backend app (Python)
5. Click **Create**

### 1.2 Configure Git Integration
1. Go to **Settings** → **Git**
2. Click **Connect GitHub**
3. Select **Repository:** `saivs-max/RSA-Agent`
4. **Branch:** `main` (or `master`)
5. **Deploy on push:** Enable (optional, for auto-deploy)
6. Click **Save**

---

## Part 2: Add Secrets to Superblocks

### 2.1 Navigate to Secrets Manager
1. In your RSA Agent project, click **Settings** → **Secrets**
2. Click **+ Add Secret**

### 2.2 Add Each Secret

#### Slack Bot Token
- **Name:** `SLACK_BOT_TOKEN`
- **Value:** xoxb-... (from Slack App)
- **Type:** Secret (encrypted)

#### Slack App Token
- **Name:** `SLACK_APP_TOKEN`
- **Value:** xapp-... (from Slack App)
- **Type:** Secret (encrypted)

#### Slack Signing Secret
- **Name:** `SLACK_SIGNING_SECRET`
- **Value:** (from Slack App)
- **Type:** Secret (encrypted)

#### Anthropic API Key
- **Name:** `ANTHROPIC_API_KEY`
- **Value:** sk-ant-... (from Anthropic Console)
- **Type:** Secret (encrypted)
- ⚠️ **IMPORTANT:** Rotate this key first (the old key in .env.example is exposed)

#### Google Apps Script URL
- **Name:** `GAS_WEB_APP_URL`
- **Value:** https://script.google.com/a/macros/instacart.com/s/.../exec
- **Type:** Secret (encrypted)

#### Agent Secret
- **Name:** `AGENT_SECRET`
- **Value:** d738a518... (from .env)
- **Type:** Secret (encrypted)
- **Note:** Must match AGENT_SECRET in GAS_BotEndpoints.gs

#### Google Service Account (JSON)
- **Name:** `GOOGLE_SERVICE_ACCOUNT_JSON`
- **Value:** Paste entire sa-key.json contents (as JSON object)
- **Type:** Secret (encrypted)

#### Configuration Variables (Non-Secret)
Add these as **Variables** (not Secrets):

```
AGENT_MODEL=claude-sonnet-4-6
RSA_FORMS_FOLDER_ID=1nXWekoXow3-az02QHfVSx7PJQP4uA4vx
TECH_RESULTS_FOLDER_ID=1g7myq_dnWCJqOqYgVzAapks4aJ7UBrHX
RSA_MONITOR_CHANNEL=C0ARJQWG7RP
RSA_TECH_POLL_SECONDS=300
RSA_GAP_REPORT_SECONDS=86400
WINTERSCALES_EMAILS=service@winterscale.com, john.winter@winterscale.com, rich.ianniello@winterscale.com
```

---

## Part 3: Configure Python Runtime

### 3.1 Set Up Environment
1. Click **Settings** → **Python Environment**
2. **Python Version:** 3.10+
3. **Requirements File:** Enable, point to `requirements.txt` from GitHub

### 3.2 Verify Dependencies
Superblocks will auto-install from requirements.txt:
```
openai>=1.30.0
gspread>=6.0.0
google-auth>=2.27.0
google-api-python-client>=2.100.0
pymupdf>=1.24.0
slack-bolt>=1.18.0
slack-sdk>=3.27.0
python-dotenv>=1.0.0
requests>=2.31.0
openpyxl>=3.1.0
Pillow>=10.0.0
```

---

## Part 4: Deploy & Test

### 4.1 Create Deployment
1. Click **Deploy** → **Create new deployment**
2. **Deployment name:** `production`
3. **Source:** GitHub `main` branch
4. **Environment:** Production
5. Click **Deploy**

### 4.2 Run Tests
In Superblocks, test key functionality:

**Test 1: Slack Connection**
```python
# Test Socket Mode connection
import slack_bolt
# Should connect without errors
```

**Test 2: Claude Integration**
```python
# Test API call to Claude
from anthropic import Anthropic
client = Anthropic(api_key="${ANTHROPIC_API_KEY}")
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=100,
    messages=[{"role": "user", "content": "Say hello"}]
)
print(response.content[0].text)
```

**Test 3: Google Sheets**
```python
# Test gspread + service account auth
import gspread
import json
sa_json = json.loads("${GOOGLE_SERVICE_ACCOUNT_JSON}")
gc = gspread.service_account_from_dict(sa_json)
# Try to open a known sheet
```

### 4.3 Monitor Logs
1. Click **Logs** to view real-time output
2. Check for errors or warnings
3. Verify Slack bot is connected

---

## Part 5: Set Up Permissions & Handoff

### 5.1 Add Maitland Kelly as Owner
1. Go to **Settings** → **Team & Access**
2. Click **+ Add member**
3. Search: `maitland.kelly@instacart.com`
4. **Role:** Admin (can manage project and deploy)
5. Click **Save**

### 5.2 Document for Handoff
Create a handoff document for Maitland with:
- Superblocks project URL
- How to update secrets (Settings → Secrets)
- How to deploy (Deploy → Create deployment)
- Emergency contacts (support, Anthropic API)
- GitHub repo for code

---

## Part 6: Verify VPN Issue Resolution

The main goal of migration is to resolve VPN issues. To verify:

1. ✓ Bot connects to Slack from Superblocks (no VPN required)
2. ✓ All API calls (Claude, Google, Slack) succeed
3. ✓ No "VPN required" or "IP blocked" errors
4. ✓ Logs show clean operation for 24+ hours

---

## Troubleshooting

### "Secret not found" error
- Check secret name matches exactly (case-sensitive)
- Verify secret is added in project, not account-level

### "GitHub branch not found"
- Ensure code is pushed to main/master branch
- Check repository name in Settings → Git

### Slack disconnects frequently
- Check SLACK_BOT_TOKEN and SLACK_APP_TOKEN are correct
- Verify Socket Mode is enabled in Slack App
- Check Superblocks logs for auth errors

### Google Sheets read fails
- Verify service account has access (check folder permissions)
- Check GOOGLE_SERVICE_ACCOUNT_JSON is valid JSON
- Ensure Google Drive API is enabled in GCP project

### Claude API calls fail
- Verify ANTHROPIC_API_KEY is fresh (rotate if needed)
- Check API key has correct permissions
- Monitor Anthropic Console for usage/quota issues

---

## Rollback Plan

If issues arise:
1. Keep fly.io deployment running as fallback
2. Redirect Slack bot commands to fly.io temporarily
3. Debug Superblocks setup
4. Redeploy to Superblocks once issues resolved

---

## Next Steps

1. **User (Sai):** Push code to GitHub
2. **User (Sai):** Rotate Anthropic API key
3. **Maitland:** Access Superblocks and verify setup
4. **Maitland:** Monitor logs for 24 hours
5. **Team:** Decommission fly.io once confident in Superblocks

