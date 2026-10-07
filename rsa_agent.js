// ═══════════════════════════════════════════════════════════════════════════════
// RSA Agent — Slack Bot
// File: rsa_agent.js
//
// Ops managers send a single Slack message to submit a cart. The agent executes
// a full sequential workflow automatically:
//
//   Step 1 — Parse intent with Claude (NLP)
//   Step 2 — Add / update cart in RSA Tracker (GAS)
//   Step 3 — Schedule TRUNO inspection + tag Nicole (GAS)
//   Step 4 — Send traceability email to Nicole (GAS)
//   Step 5 — Post confirmation to Slack with links + next steps
//
// Additionally handles:
//   • Status queries           ("What's the status of cart X?")
//   • Filtered list views      ("Show all pending RSA in NJ")
//   • Tracker summary          ("Give me a dashboard summary")
//   • Manual status updates    ("Mark cart X as Completed RSA")
//
// SETUP: see README_SETUP.md
// ═══════════════════════════════════════════════════════════════════════════════

'use strict';

require('dotenv').config();

const { App, LogLevel } = require('@slack/bolt');
const Anthropic         = require('@anthropic-ai/sdk');
const fetch             = (...args) => import('node-fetch').then(m => m.default(...args));

// ── Config ────────────────────────────────────────────────────────────────────
const {
  SLACK_BOT_TOKEN,
  SLACK_APP_TOKEN,
  SLACK_SIGNING_SECRET,
  ANTHROPIC_API_KEY,
  GAS_WEB_APP_URL,
  AGENT_SECRET = 'rsa-agent-secret-change-me',
  NICOLE_SLACK_ID = '',       // Nicole's Slack user ID for @mention in confirmations
} = process.env;

for (const [k, v] of Object.entries({ SLACK_BOT_TOKEN, SLACK_APP_TOKEN, ANTHROPIC_API_KEY, GAS_WEB_APP_URL })) {
  if (!v) { console.error(`❌ Missing env var: ${k}. Check your .env file.`); process.exit(1); }
}

const anthropic = new Anthropic({ apiKey: ANTHROPIC_API_KEY });

const app = new App({
  token:         SLACK_BOT_TOKEN,
  appToken:      SLACK_APP_TOKEN,
  signingSecret: SLACK_SIGNING_SECRET,
  socketMode:    true,
  logLevel:      LogLevel.WARN,
});


// ═══════════════════════════════════════════════════════════════════════════════
// CLAUDE INTENT PARSER
// ═══════════════════════════════════════════════════════════════════════════════

const SYSTEM_PROMPT = `You are the RSA Operations assistant for Instacart Hardware Ops.
You help ops managers interact with the RSA cart tracker and automate workflows.

ACTIONS available:
  • onboardCart  — Full workflow: add to RSA Tracker + schedule TRUNO + email Nicole
                   Use when an ops manager submits a NEW cart that needs inspection scheduled.
                   Key signal: "new cart", "add cart", "launch", "schedule inspection", "need TRUNO visit"
  • updateCart   — Update specific fields on an existing cart (status changes, notes, DRI)
                   Use when the cart already exists and they're updating its progress.
  • queryCart    — Look up a specific cart's current status
  • getSummary   — Overall dashboard counts and breakdown by state
  • listCarts    — Filtered list (by state, status, DRI, store, stale carts)
  • unknown      — Request needs clarification

RSA Tracker field reference:
  cartId, store, state (2-letter), trigger (Launches/Cart replacement/Load cell recal/W&M Failed/W&M Inspection/Scale Drift/Bent Frame/Top unit replacement)
  rsaStatus (Pending RSA/Scheduled RSA/Completed RSA/Failed RSA)
  rsaVendor (Winter Scales/TRUNO/DUMAC/TBD)
  rsaSchedDate (MM/DD/YYYY)
  wmStatus (Pending W&M/Scheduled W&M/Passed W&M/Failed W&M/Completed W&M)
  wmDate (MM/DD/YYYY)
  overallStatus (Pending RSA/Pending W&M/Passed/Failed/Redispatch Needed/Needs Verification)
  dri (Sai VS/Vishnu Kariyattu/Maitland Kelly/Andrew Cha/Reshmi Chowdhury/HW Ops/Ops Manager/CSM/Winter Scales/TRUNO/DUMAC/TBD)
  notes, visitDate (for TRUNO scheduling), submittedBy (name of the submitter)

Return ONLY valid JSON matching this schema (no markdown, no explanation):
{
  "action": "onboardCart"|"updateCart"|"queryCart"|"getSummary"|"listCarts"|"unknown",
  "cartId": string|null,
  "store": string|null,
  "storeHint": string|null,
  "state": string|null,
  "trigger": string|null,
  "rsaStatus": string|null,
  "rsaVendor": string|null,
  "rsaSchedDate": string|null,
  "wmStatus": string|null,
  "wmDate": string|null,
  "overallStatus": string|null,
  "dri": string|null,
  "notes": string|null,
  "appendNotes": boolean,
  "visitDate": string|null,
  "submittedBy": string|null,
  "staleOnly": boolean,
  "storeFragment": string|null,
  "limit": number|null,
  "clarification": string|null
}

Today's date: ${new Date().toLocaleDateString('en-US')}`;

async function parseIntent(userMessage, userName) {
  const response = await anthropic.messages.create({
    model:      'claude-haiku-4-5-20251001',
    max_tokens: 600,
    system:     SYSTEM_PROMPT,
    messages:   [{
      role:    'user',
      content: `Submitted by: ${userName || 'ops manager'}\nMessage: ${userMessage}`,
    }],
  });

  const text  = (response.content[0]?.text || '{}').trim();
  const clean = text.replace(/^```json\s*/i, '').replace(/```$/i, '').trim();
  return JSON.parse(clean);
}


// ═══════════════════════════════════════════════════════════════════════════════
// GAS API CALLER
// ═══════════════════════════════════════════════════════════════════════════════

async function callGAS(payload) {
  const res = await fetch(GAS_WEB_APP_URL, {
    method:   'POST',
    headers:  { 'Content-Type': 'application/json' },
    body:     JSON.stringify({ ...payload, secret: AGENT_SECRET }),
    redirect: 'follow',
  });
  if (!res.ok) throw new Error(`GAS HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}


// ═══════════════════════════════════════════════════════════════════════════════
// WORKFLOW: FULL ONBOARD (new cart → RSA Tracker → TRUNO → Email Nicole)
// ═══════════════════════════════════════════════════════════════════════════════

async function runOnboardWorkflow(intent, userName) {
  const result = await callGAS({
    action:        'fullOnboardCart',
    cartId:        intent.cartId,
    store:         intent.store,
    state:         intent.state,
    trigger:       intent.trigger,
    rsaStatus:     intent.rsaStatus,
    wmStatus:      intent.wmStatus,
    overallStatus: intent.overallStatus,
    rsaVendor:     intent.rsaVendor || 'TRUNO',
    dri:           intent.dri,
    notes:         intent.notes,
    visitDate:     intent.visitDate,
    submittedBy:   userName || intent.submittedBy || 'Ops Manager',
    appendNotes:   true,
  });
  return result;
}

function formatOnboardResult(result, intent) {
  if (!result) return '❌ No response from server.';

  const step1 = result.steps?.step1_rsaTracker;
  const step2 = result.steps?.step2_trunoSchedule;

  const s1Icon  = step1?.error ? '❌' : step1?.action === 'added' ? '➕' : '✏️';
  const s1Label = step1?.error ? `Failed — ${step1.error}`
                : step1?.action === 'added' ? 'Cart added to RSA Tracker'
                : 'Cart updated in RSA Tracker';

  const s2Icon  = step2?.error ? '❌' : '📅';
  const s2Label = step2?.error
    ? `Truno scheduling failed — ${step2.error}`
    : `TRUNO inspection scheduled for *${step2.visitDate}*${NICOLE_SLACK_ID ? ` (cc: <@${NICOLE_SLACK_ID}>)` : ''}`;

  const emailLine = step2?.emailSent
    ? `📧 Email sent to Nicole at \`${step2.emailTo}\``
    : step2?.error ? '' : '📧 Email to Nicole queued';

  const blocks = [
    {
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: [
          `*🛒 Cart Onboarded: \`${result.cartId}\`*  at _${result.store}_`,
          '',
          `${s1Icon} *Step 1 — RSA Tracker:* ${s1Label}`,
          `${s2Icon} *Step 2 — TRUNO Schedule:* ${s2Label}`,
          emailLine,
        ].filter(Boolean).join('\n'),
      },
    },
    {
      type: 'actions',
      elements: [
        { type: 'button', text: { type: 'plain_text', text: '📊 RSA Tracker' }, url: result.trackerUrl, style: 'primary' },
        ...(step2?.trunoUrl ? [{ type: 'button', text: { type: 'plain_text', text: '📋 TRUNO Tracker' }, url: step2.trunoUrl }] : []),
      ],
    },
  ];

  return { text: `Cart ${result.cartId} onboarded at ${result.store}`, blocks };
}


// ═══════════════════════════════════════════════════════════════════════════════
// OTHER RESPONSE FORMATTERS
// ═══════════════════════════════════════════════════════════════════════════════

function formatUpdateResult(result) {
  if (result.error) return { text: `❌ ${result.error}` };
  return {
    text: `✅ Updated \`${result.cartId}\` — ${result.updated?.join(', ')}`,
    blocks: [{
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: `✅ *Updated \`${result.cartId}\`* at _${result.store}_\nFields: ${result.updated?.join(', ')}`,
      },
    }, {
      type: 'actions',
      elements: [{ type: 'button', text: { type: 'plain_text', text: '📊 View Tracker' }, url: result.trackerUrl, style: 'primary' }],
    }],
  };
}

function formatQueryResult(result) {
  if (result.error || !result.found) {
    return { text: result.error || result.message || 'Cart not found.' };
  }
  const icon = (s) => {
    if (!s) return '—';
    const l = s.toLowerCase();
    if (l.includes('pass') || l.includes('complet')) return '✅';
    if (l.includes('fail') || l.includes('redispatch')) return '❌';
    if (l.includes('sched')) return '📅';
    return '⏳';
  };
  return {
    text: `Cart ${result.cartId}: ${result.rsaStatus} / ${result.wmStatus}`,
    blocks: [{
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: [
          `*🛒 \`${result.cartId}\`*  |  _${result.store}_  |  ${result.state || '—'}`,
          `${icon(result.rsaStatus)} RSA: *${result.rsaStatus || '—'}*  ·  Vendor: ${result.rsaVendor || '—'}`,
          `${icon(result.wmStatus)} W&M: *${result.wmStatus || '—'}*  ·  Date: ${result.wmDate || '—'}`,
          `${icon(result.overallStatus)} Overall: *${result.overallStatus || '—'}*  ·  Days: ${result.days != null ? result.days + 'd' : '—'}`,
          `DRI: ${result.dri || '—'}  ·  Updated: ${result.lastUpdated || '—'}`,
          result.notes ? `📝 ${result.notes}` : null,
        ].filter(Boolean).join('\n'),
      },
    }, {
      type: 'actions',
      elements: [{ type: 'button', text: { type: 'plain_text', text: '📊 View Tracker' }, url: result.trackerUrl, style: 'primary' }],
    }],
  };
}

function formatSummaryResult(result) {
  if (result.error) return { text: `❌ ${result.error}` };
  const c = result.counts;
  const topStates = Object.entries(result.byState)
    .sort((a, b) => b[1] - a[1]).slice(0, 6)
    .map(([s, n]) => `${s}: ${n}`).join('  ·  ');

  return {
    text: `RSA Tracker: ${c.total} carts, ${c.pendingRSA} pending RSA, ${c.needsAction} need action`,
    blocks: [{
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: [
          '*📊 RSA Tracker Summary*',
          `Total carts: *${c.total}*  ·  🔴 Needs Action: *${c.needsAction}*  ·  ⏰ Stale 10+ days: *${c.stale10plus}*`,
          '',
          `*RSA*: ⏳ Pending ${c.pendingRSA}  📅 Scheduled ${c.scheduledRSA}  ✅ Completed ${c.completedRSA}  ❌ Failed ${c.failedRSA}`,
          `*W&M*: ⏳ Pending ${c.pendingWM}  ✅ Passed ${c.passedWM}  ❌ Failed ${c.failedWM}`,
          `*Overall Passed*: ${c.overallPassed}`,
          '',
          `*Top States*: ${topStates || '—'}`,
        ].join('\n'),
      },
    }, {
      type: 'actions',
      elements: [{ type: 'button', text: { type: 'plain_text', text: '📊 Open Tracker' }, url: result.trackerUrl, style: 'primary' }],
    }],
  };
}

function formatListResult(result) {
  if (result.error) return { text: `❌ ${result.error}` };
  if (result.total === 0) return { text: '🔍 No carts match the given filters.' };

  const header = result.total > result.shown
    ? `*${result.total} carts* (showing first ${result.shown}):`
    : `*${result.total} cart${result.total !== 1 ? 's' : ''}*:`;

  const lines = result.carts.map(c => {
    const days = c.days != null ? ` · ${c.days}d` : '';
    return `• \`${c.cartId}\` — ${c.store || '?'} (${c.state || '?'}) · *${c.rsaStatus || '—'}* · *${c.wmStatus || '—'}*${days}`;
  });

  return {
    text: `${result.total} carts found`,
    blocks: [{
      type: 'section',
      text: { type: 'mrkdwn', text: `${header}\n${lines.join('\n')}` },
    }, {
      type: 'actions',
      elements: [{ type: 'button', text: { type: 'plain_text', text: '📊 View Tracker' }, url: result.trackerUrl, style: 'primary' }],
    }],
  };
}


// ═══════════════════════════════════════════════════════════════════════════════
// MAIN MESSAGE HANDLER
// ═══════════════════════════════════════════════════════════════════════════════

async function handleMessage({ text, say, client, channel, ts, userId }) {
  const cleanText = text.replace(/<@[A-Z0-9]+>/g, '').trim();
  if (!cleanText) {
    await say({ text: helpText(), thread_ts: ts });
    return;
  }

  // Get user's real name for traceability
  let userName = 'Ops Manager';
  try {
    const userInfo = await client.users.info({ user: userId });
    userName = userInfo.user?.real_name || userInfo.user?.name || 'Ops Manager';
  } catch(e) {}

  // Show working indicator
  await client.reactions.add({ channel, timestamp: ts, name: 'hourglass_flowing_sand' }).catch(() => {});

  try {
    const intent = await parseIntent(cleanText, userName);
    let reply;

    if (intent.action === 'onboardCart') {
      // ── Full sequential workflow ──────────────────────────────────────────
      // Post a "working" message first since this takes a few seconds
      const thinkingMsg = await say({
        text: '⚙️ Processing… adding to tracker, scheduling with TRUNO, and emailing Nicole.',
        thread_ts: ts,
      });

      const result = await runOnboardWorkflow(intent, userName);
      reply = formatOnboardResult(result, intent);

      // Delete the thinking message if possible
      try { await client.chat.delete({ channel, ts: thinkingMsg.ts }); } catch(e) {}

    } else if (intent.action === 'updateCart') {
      const result = await callGAS({ action: 'updateCart', ...intent });
      reply = formatUpdateResult(result);

    } else if (intent.action === 'queryCart') {
      const result = await callGAS({ action: 'queryCart', ...intent });
      reply = formatQueryResult(result);

    } else if (intent.action === 'getSummary') {
      const result = await callGAS({ action: 'getSummary' });
      reply = formatSummaryResult(result);

    } else if (intent.action === 'listCarts') {
      const result = await callGAS({ action: 'listCarts', ...intent });
      reply = formatListResult(result);

    } else {
      reply = {
        text: `🤔 ${intent.clarification || 'Not sure what you need. See options below.'}\n\n${helpText()}`,
      };
    }

    await client.reactions.remove({ channel, timestamp: ts, name: 'hourglass_flowing_sand' }).catch(() => {});

    if (typeof reply === 'string') {
      await say({ text: reply, thread_ts: ts });
    } else {
      await say({ ...reply, thread_ts: ts });
    }

  } catch(err) {
    console.error('Handler error:', err);
    await client.reactions.remove({ channel, timestamp: ts, name: 'hourglass_flowing_sand' }).catch(() => {});
    await say({ text: `❌ Error: ${err.message}`, thread_ts: ts });
  }
}

function helpText() {
  return [
    '*RSA Agent — what I can do:*',
    '• *New cart (full workflow):* "New cart P_WF_001, ShopRite Forest Hill NJ, trigger Launches"',
    '• *Update status:* "Cart NJ-001 passed W&M today"',
    '• *Update status:* "Set cart 42 at ShopRite RSA to Scheduled RSA"',
    '• *Query cart:* "Status of P_WF_SHOPRITE_72_M3_001?"',
    '• *Filter list:* "Show pending RSA carts in NJ" or "Show stale carts assigned to Sai"',
    '• *Summary:* "Give me a summary" or "dashboard"',
  ].join('\n');
}


// ── Event listeners ───────────────────────────────────────────────────────────

app.event('app_mention', async ({ event, say, client }) => {
  await handleMessage({
    text:    event.text,
    say,
    client,
    channel: event.channel,
    ts:      event.ts,
    userId:  event.user,
  });
});

app.message(async ({ message, say, client }) => {
  if (message.channel_type !== 'im' || message.bot_id || message.subtype) return;
  await handleMessage({
    text:    message.text || '',
    say,
    client,
    channel: message.channel,
    ts:      message.ts,
    userId:  message.user,
  });
});


// ── Start ─────────────────────────────────────────────────────────────────────

(async () => {
  await app.start();
  console.log('✅ RSA Agent is running (Socket Mode)');
  console.log(`   GAS endpoint: ${GAS_WEB_APP_URL}`);
})();
