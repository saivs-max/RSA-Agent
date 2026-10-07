// ═══════════════════════════════════════════════════════════════════════════════
// RSA Agent — Automated Monitoring & Workflow Triggers
// File: GAS_Automation.gs
//
// Copy into your Apps Script project alongside Code.gs and GAS_BotEndpoints.gs.
//
// This file handles ALL time-based automation:
//   1. monitorTrunoTracker()       — Polls Truno sheet for scheduled/completed visits
//                                    → Updates RSA Tracker status + ETS date
//                                    → Triggers Coffee Break reminder to ETS team
//   2. checkCompletedVisits()      — Finds completed RSA visits
//                                    → Notifies CSM team to schedule W&M
//   3. ingestInspectionFiles()     — Reads inspection Excel/CSV files from Drive folder
//                                    → Populates dashboard + adds tech upload links
//   4. masterDailyRun()            — Umbrella trigger that calls all of the above
//                                    + existing checkAndSendAlerts()
//
// HOW TO INSTALL TRIGGERS (run once each):
//   Apps Script editor → Run: createAutomationTriggers()
// ═══════════════════════════════════════════════════════════════════════════════


// ── Configuration ─────────────────────────────────────────────────────────────

// Google Drive folder containing completed inspection files uploaded by techs
// https://drive.google.com/drive/folders/1nXWekoXow3-az02QHfVSx7PJQP4uA4vx
const INSPECTION_FOLDER_ID = '1nXWekoXow3-az02QHfVSx7PJQP4uA4vx';

// ETS (End-of-Term Service) team Slack channel — receives Coffee Break reminders.
// NOTE: WM_SLACK_CHANNEL_ID is declared once in Code.gs — do NOT redeclare it here (two
// top-level `const`s of the same name across files throw "Identifier already declared").
// To route these posts to the ops channel, set WM_SLACK_CHANNEL_ID = 'C0ARJQWG7RP' in Code.gs.
const ETS_SLACK_CHANNEL_ID = WM_SLACK_CHANNEL_ID;

// CSM team email for post-visit W&M completion notifications
// Already defined in Code.gs as: const CSM_EMAIL = 'caper-customer-success@instacart.com';

// Truno tracker column indices (must match TRUNO_COL in GAS_BotEndpoints.gs).
// These are used to READ back from the live Truno "Sheet1" during monitoring.
const T_STORE       = 1;   // A — IC Store Name
const T_TRUNO_STORE = 2;   // B — Truno Store Name (e.g. ISC0050-WKF-0113)
const T_CART        = 3;   // C — Cart (number; may be a list like "1, 7")
const T_TRUNO_CALL  = 4;   // D — Truno Call #
const T_ADDRESS     = 5;   // E — Address
const T_VISIT_DATE  = 6;   // F — Scheduled Date
const T_ARRIVAL     = 7;   // G — Arrival Time of Tech
const T_TEST_RESULT = 8;   // H — Test Results
const T_NOTES       = 9;   // I — Notes
const T_FORMS_SENT  = 10;  // J — Forms sent to team?
const T_STATUS      = 11;  // K — Status (raw words; normalized via _canonTrunoStatus)
const T_MANAGER     = 12;  // L — Manager
const T_WM          = 13;  // M — W&M (market code, e.g. EWR / LAX)
const T_LAST_COL    = 13;  // total columns to read

// Dashboard sheet name (for tech upload column)
const DASHBOARD_SHEET_NAME = 'W&M Import';   // update if your dashboard tab is named differently

// Column in the RSA Tracker where tech upload links will be stored
// Currently placed in col S (Notes/Next Steps = col 19). We'll use a new col T = 20 for links.
const UPLOAD_LINK_COL = 20;  // T — "Tech Upload Link"

// Alert tag for ETS Coffee Break reminder (Slack user group)
const ETS_USERGROUP_HANDLE = '<!subteam^S05TS9VMTN0>';  // existing W&M group from Code.gs


// ═══════════════════════════════════════════════════════════════════════════════
// 1. MASTER DAILY RUN
//    Single function installed as a daily trigger (replaces checkAndSendAlerts
//    as the trigger target — it calls that too).
// ═══════════════════════════════════════════════════════════════════════════════

function masterDailyRun() {
  Logger.log('── masterDailyRun started ──');

  // Existing alert / archive / progression logic
  try { checkAndSendAlerts(); }    catch(e) { Logger.log('checkAndSendAlerts error: ' + e.message); }

  // New automation
  try { monitorTrunoTracker(); }   catch(e) { Logger.log('monitorTrunoTracker error: ' + e.message); }
  // checkCompletedVisits() DISABLED — W&M inspection is out of scope; no more W&M nudges.
  try { ingestInspectionFiles(); } catch(e) { Logger.log('ingestInspectionFiles error: ' + e.message); }

  Logger.log('── masterDailyRun complete ──');
}


// ═══════════════════════════════════════════════════════════════════════════════
// 2. TRUNO TRACKER MONITOR
//    Reads the Truno spreadsheet and syncs status back to the RSA Tracker.
//
//    Triggers on three transitions in the Truno "Status" column (col F):
//      "Pending"   → visit not yet confirmed by Truno (no action)
//      "Scheduled" → visit date confirmed → update RSA Tracker + set ETS + send Coffee Break reminder
//      "Completed" → visit done           → OWNED BY THE PYTHON BOT (form-driven); skipped here
//      "Cancelled" → visit cancelled      → reset RSA status back to "Pending RSA", alert DRI
// ═══════════════════════════════════════════════════════════════════════════════

function monitorTrunoTracker() {
  const ss      = SpreadsheetApp.openById(TRUNO_SPREADSHEET_ID);
  const sheets  = ss.getSheets();
  const trunoSheet = sheets.find(s => s.getName().toLowerCase().includes('rsa')) || sheets[0];
  if (!trunoSheet) { Logger.log('monitorTrunoTracker: Truno sheet not found.'); return; }

  const rsaSS   = SpreadsheetApp.openById(SPREADSHEET_ID);
  const tracker = rsaSS.getSheetByName('RSA Tracker');
  if (!tracker)  { Logger.log('monitorTrunoTracker: RSA Tracker not found.'); return; }

  const maxScan  = Math.max(trunoSheet.getMaxRows() - 1, 1);
  const data     = trunoSheet.getRange(2, 1, maxScan, T_LAST_COL).getValues();

  // Load processed rows from script properties to avoid re-processing.
  const props     = PropertiesService.getScriptProperties();
  const processed  = JSON.parse(props.getProperty('trunoProcessed') || '{}');
  // First run after the column re-map: snapshot existing rows as "already
  // handled" so we don't re-fire emails/Slack for visits that are already
  // complete or scheduled. Genuine transitions are caught on later runs.
  const baseline   = props.getProperty('trunoBaselineV3') !== 'done';   // V3: re-snapshot once to stop a re-fire flood

  const now = new Date();

  for (let i = 0; i < data.length; i++) {
    const row        = data[i];
    const icStore    = String(row[T_STORE       - 1] || '').trim();
    const trunoStore = String(row[T_TRUNO_STORE - 1] || '').trim();
    const cartRaw    = row[T_CART - 1];
    const cart       = String(cartRaw || '').trim();
    const market     = String(row[T_WM          - 1] || '').trim();   // W&M code (display only)
    const visitDate  = row[T_VISIT_DATE - 1];
    const status     = _canonTrunoStatus(row[T_STATUS - 1]);          // scheduled|completed|cancelled|''

    // Skip blank rows and statuses we don't act on (pending forms, duplicate, …)
    if ((!icStore && !trunoStore) || !status) continue;

    // Act on each (row + status) at most once. Keyed on store + cart, since
    // Truno rows have no stable exact id.
    // Idempotency key: store|cart|status (NOT the visit date — keying on the date
    // invalidates the whole saved cache on deploy and re-fires every row). Re-schedules
    // are handled by clearing the 'scheduled' key on cancellation (below) instead.
    const rowSig = `${trunoStore}|${icStore}|${cart}|${status}`.replace(/[^a-zA-Z0-9_|]/g, '_');
    if (processed[rowSig]) continue;
    if (baseline) { processed[rowSig] = now.toISOString(); continue; }

    // 'Completed' is now owned by the Python bot (drive_monitor + test_forms): it
    // reads the actual Scale Test Form and writes Pass→Completed RSA (next step
    // Pending W&M) or Fail→Failed RSA with the report link, then tags the CSM.
    // Skip it here so GAS doesn't double-write the status, mislabel a failed
    // inspection as "Completed RSA", or post a duplicate CSM notification.
    if (status === 'completed') { processed[rowSig] = now.toISOString(); continue; }

    // Guard: a Cart cell that is actually a DATE (bad data / a shifted Truno row) can't
    // identify a cart — skip it so we never reset the wrong cart or spam the DRI. Real
    // cart cells are short tokens like "1, 7" or "3A".
    if (cartRaw instanceof Date ||
        /\b(19|20)\d{2}\b/.test(cart) ||
        /(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|mon|tue|wed|thu|fri|sat|sun)/i.test(cart)) {
      Logger.log(`monitorTrunoTracker: "${icStore || trunoStore}" Cart cell is not a cart ("${cart}") — skipped. Fix the Truno row.`);
      processed[rowSig] = now.toISOString();
      continue;
    }

    // Infer which RSA cart(s) this Truno row means; only act when confident.
    const m = _matchTrunoRow(tracker, icStore, trunoStore, cart);

    if (!m.matches.length && !m.ambiguous.length && !m.unmatched.length) {
      Logger.log(`monitorTrunoTracker: no RSA store match for "${icStore}" / "${trunoStore}" — will retry.`);
      continue;  // not marked processed → retried later in case the cart is added
    }

    m.matches.forEach(function(hit) {
      Logger.log(`monitorTrunoTracker: ${trunoStore} cart ${hit.cartNum} → RSA ${hit.cartId} (${status})`);
      if (status === 'scheduled')      _handleTrunoScheduled(tracker, hit.cartId, icStore, market, visitDate, now);
      else if (status === 'completed') _handleTrunoCompleted(tracker, hit.cartId, icStore, now);
      else if (status === 'cancelled') _handleTrunoCancelled(tracker, hit.cartId, icStore, now);
    });

    // Anything we couldn't pin down → ask a human instead of guessing (the matched carts
    // above are reported as already-handled so they don't read as "couldn't match").
    if (m.ambiguous.length || m.unmatched.length) _flagTrunoAmbiguous(icStore, trunoStore, cart, status, m);

    // On a cancellation, forget the prior 'scheduled' key for this store+cart so a later
    // re-schedule (to a new date) is treated as new and re-fires the ETS reminder.
    if (status === 'cancelled') {
      delete processed[`${trunoStore}|${icStore}|${cart}|scheduled`.replace(/[^a-zA-Z0-9_|]/g, '_')];
    }

    processed[rowSig] = now.toISOString();
  }

  if (baseline) props.setProperty('trunoBaselineV3', 'done');
  props.setProperty('trunoProcessed', JSON.stringify(processed));
  Logger.log('monitorTrunoTracker complete.');
}


// ═══════════════════════════════════════════════════════════════════════════════
// TRUNO STATUS NORMALIZATION
// The live Truno sheet uses free-form status words. Map them to the three
// transitions the monitor acts on; everything else (Pending Forms, Pending
// Customer, Duplicate, blank, …) returns '' and is ignored.
// ═══════════════════════════════════════════════════════════════════════════════
function _canonTrunoStatus(raw) {
  const s = String(raw || '').trim().toLowerCase();
  if (!s) return '';
  if (s.indexOf('cancel') !== -1)     return 'cancelled';  // Canceled / Cancelled
  if (s.indexOf('reschedule') !== -1) return 'cancelled';  // Reschedule / Pending Reschedule → reset + alert
  if (s.indexOf('closed') !== -1)     return 'cancelled';  // Closed (call superseded)
  if (s.indexOf('complete') !== -1)   return 'completed';  // Complete / Completed
  if (s.indexOf('schedul') !== -1)    return 'scheduled';  // Scheduled (reschedule handled above)
  return '';  // pending forms, pending customer, duplicate, totals, etc.
}


// ═══════════════════════════════════════════════════════════════════════════════
// TRUNO → RSA TRACKER MATCHING (inferential)
// Truno rows carry no exact RSA cart id, so we infer the match. RSA serials
// follow the scheme  P_..._<STORE>_M3_<CART>[letter]  (e.g. P_WF_SHOPRITE_113_M3_002
// = store 113, cart 2), so we match on the store number (taken from the serial's
// store segment AND the store text) and then on the cart number, breaking ties on
// the letter suffix. A match is returned ONLY when it is unambiguous; anything
// uncertain is reported in `ambiguous` so the caller can ask a human rather than
// guess. Assumes RSA Tracker col A = serial, col B = store (as the rest of this
// project already does). Helper names are _tm-prefixed to avoid collisions.
// ═══════════════════════════════════════════════════════════════════════════════
function _matchTrunoRow(tracker, icStore, trunoStore, cartCell) {
  const result = { matches: [], ambiguous: [], unmatched: [] };

  const scanStart = 4;
  const maxScan   = Math.max(tracker.getMaxRows() - scanStart + 1, 1);
  const rows      = tracker.getRange(scanStart, 1, maxScan, 2).getValues();  // A=serial, B=store

  // Store numbers from the Truno side (ISC0050-WKF-0113 → "113"; "Shoprite 116" → "116")
  const storeNums = _tmStoreNumbers(trunoStore).concat(_tmStoreNumbers(icStore));
  const icNorm    = _tmNorm(icStore);

  // Candidate RSA rows whose store matches by number (from the serial's store
  // segment AND the store text) or by name.
  const storeRows = [];
  for (let i = 0; i < rows.length; i++) {
    const cartId   = String(rows[i][0] || '').trim();
    const rsaStore = String(rows[i][1] || '');
    if (!cartId || !rsaStore.trim()) continue;

    let rsaNums = _tmStoreNumbers(rsaStore);
    const idStore = _tmStoreNumFromId(cartId);                 // store # embedded in the serial
    if (idStore) rsaNums = rsaNums.concat(idStore, idStore.replace(/^0+/, ''));

    const numHit  = storeNums.length && rsaNums.some(function(n){ return storeNums.indexOf(n) !== -1; });
    const nameHit = icNorm.length >= 4 && _tmNorm(rsaStore).indexOf(icNorm) !== -1;
    if (numHit || nameHit) storeRows.push({ cartId: cartId });
  }
  if (!storeRows.length) return result;  // store not found at all → caller retries later

  // Store-level ambiguity: if the Truno store maps to RSA rows from more than one
  // distinct store number, no per-cart match is trustworthy (a cart number can
  // exist in both stores). Flag the whole row instead of guessing. e.g. Truno
  // "Wakefern 82" matching both ShopRite-82 and WF-82/ShopRite-563.
  const storeKeys = {};
  storeRows.forEach(function(r){ storeKeys[_tmStoreNumFromId(r.cartId) || '?'] = true; });
  if (Object.keys(storeKeys).length > 1) {
    result.ambiguous.push({ cartNum: cartCell || '(all carts)', candidates: storeRows.map(function(r){ return r.cartId; }) });
    return result;
  }

  // Cart numbers from the Truno cart cell: "1, 7" → ["1","7"]; "3A" → ["3A"]
  const cartNums = String(cartCell || '').split(/[,/]+/).map(function(s){ return s.trim(); }).filter(Boolean);

  // No cart detail → safe only if the store maps to exactly one RSA cart
  if (!cartNums.length) {
    if (storeRows.length === 1) result.matches.push({ cartId: storeRows[0].cartId, cartNum: '' });
    else result.ambiguous.push({ cartNum: '(unspecified)', candidates: storeRows.map(function(r){ return r.cartId; }) });
    return result;
  }

  const matchedIds = {};
  cartNums.forEach(function(cn) {
    const t = _tmTrunoCart(cn);                                // {num, full}
    if (!t.num) { result.unmatched.push(cn); return; }         // not a cart number → can't tag
    // Match on the serial's cart number; if several share it, use the letter
    // suffix (e.g. "3" vs "3A") to break the tie.
    let hits = storeRows.filter(function(r){ return _tmSerialCart(r.cartId).num === t.num; });
    if (hits.length > 1) {
      const exact = hits.filter(function(r){ return _tmSerialCart(r.cartId).full === t.full; });
      if (exact.length === 1) hits = exact;
    }
    if (hits.length === 1) {
      result.matches.push({ cartId: hits[0].cartId, cartNum: cn });
      matchedIds[hits[0].cartId] = true;
    } else if (hits.length === 0) {
      result.unmatched.push(cn);                               // store has no cart with this number
    } else {                                                   // a genuine tie among real candidates
      result.ambiguous.push({ cartNum: cn, candidates: hits.map(function(r){ return r.cartId; }) });
    }
  });

  // Never offer a serial we already matched as a candidate for another cart; drop a tie
  // that empties out as a result (it's effectively covered).
  result.ambiguous = result.ambiguous
    .map(function(a){ return { cartNum: a.cartNum,
        candidates: a.candidates.filter(function(c){ return !matchedIds[c]; }) }; })
    .filter(function(a){ return a.candidates.length; });
  return result;
}

/** Lowercase alphanumerics only, for fuzzy comparison. */
function _tmNorm(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

/**
 * Candidate store numbers from a store string. Drops the common "ISC0050"
 * Truno prefix, then returns each 2+ digit group both as-is and with leading
 * zeros stripped (so "0113" also matches "113").
 */
function _tmStoreNumbers(s) {
  const cleaned = String(s || '').toLowerCase().replace(/isc0*50/g, ' ');
  const groups  = cleaned.match(/\d{2,}/g) || [];
  const out = {};
  groups.forEach(function(g) {
    out[g] = true;
    out[g.replace(/^0+/, '')] = true;
  });
  return Object.keys(out).filter(Boolean);
}

/** Store number embedded in an RSA serial: ..._<store>_M3_... → "113". '' if absent. */
function _tmStoreNumFromId(cartId) {
  const m = String(cartId).match(/_0*(\d+)_M3_/i);
  return m ? m[1] : '';
}

/** Normalize a Truno cart token: "3A" → {num:"3", full:"3a"}; "016" → {num:"16", full:"16"}. */
function _tmTrunoCart(cn) {
  const full = String(cn).toLowerCase().replace(/[^a-z0-9]/g, '').replace(/^0+/, '');
  return { num: full.replace(/[a-z]+$/, ''), full: full };
}

/** Cart token from an RSA serial: ..._M3_<cart>[letter] → {num, full}. */
function _tmSerialCart(cartId) {
  const m = String(cartId).match(/_M3_0*(\d+[a-z]?)/i);
  if (!m) return { num: '', full: '' };
  const full = m[1].toLowerCase();
  return { num: full.replace(/[a-z]+$/, ''), full: full };
}


// ═══════════════════════════════════════════════════════════════════════════════
// AMBIGUOUS MATCH → ASK A HUMAN
// Posts a clarification request to the W&M channel instead of guessing which
// RSA cart a Truno row refers to.
// ═══════════════════════════════════════════════════════════════════════════════
function _flagTrunoAmbiguous(icStore, trunoStore, cartCell, status, m) {
  const matches   = (m && m.matches)   || [];
  const ambiguous = (m && m.ambiguous) || [];
  const unmatched = (m && m.unmatched) || [];

  const lines = [];
  // Show what WAS tagged first, so a just-scheduled cart isn't read as "couldn't match".
  if (matches.length) {
    const done = matches.map(function(h){ return '`' + h.cartId + '`'; }).join(', ');
    lines.push(`• :white_check_mark: cart *${matches.map(function(h){ return h.cartNum; }).join(', ')}* already *${status}* → ${done}`);
  }
  // A genuine tie — list only the real candidates (the matched serial above is excluded).
  ambiguous.forEach(function(a) {
    const cands = a.candidates.slice(0, 8).map(function(c){ return '`' + c + '`'; }).join(', ');
    lines.push(`• cart *${a.cartNum}* could be ${cands} — which one?`);
  });
  // No RSA cart has this number — don't pad it with unrelated store carts.
  if (unmatched.length) {
    lines.push(`• :warning: no RSA cart found for cart *${unmatched.join(', ')}* — please add/tag it in the webapp.`);
  }

  // Deep-link into the WEBAPP (never the raw sheet), scoped to a real cart when we have one.
  const repCart = (matches[0] && matches[0].cartId)
    || (ambiguous[0] && ambiguous[0].candidates[0]) || '';

  try {
    postToSlack({
      channel: WM_SLACK_CHANNEL_ID,
      text: `:grey_question: Truno update needs confirmation: ${icStore || trunoStore} cart "${cartCell}" → ${status}`,
      blocks: [
        { type: 'section', text: { type: 'mrkdwn', text:
            `:grey_question: *Truno match needs confirmation*\n` +
            `*${icStore || trunoStore}* (${trunoStore}) — Truno cart "${cartCell}" is now *${status}*:\n${lines.join('\n')}\n\n` +
            `Please tag the right cart's RSA Status in the webapp, or reply with the correct cart id.` } },
        { type: 'actions', elements: [{ type: 'button',
            text: { type: 'plain_text', text: 'Open in webapp →', emoji: true },
            url: _webAppUrl(repCart), style: 'primary' }] },
      ],
    });
  } catch (e) {
    Logger.log('_flagTrunoAmbiguous Slack error: ' + e.message);
  }
}


/**
 * Handles a Truno visit transitioning to "Scheduled".
 * - Sets RSA Tracker: RSA Status=Scheduled RSA, RSA Vendor=TRUNO, RSA Sched Date, ETS Date
 * - Posts Coffee Break reminder to ETS channel
 */
function _handleTrunoScheduled(tracker, cartId, store, state, visitDate, now) {
  const { row, rowData } = _findCartRow(tracker, cartId, store);
  if (!row) { Logger.log(`_handleTrunoScheduled: Cart ${cartId} not found in RSA Tracker.`); return; }

  // Compute ETS reminder = visitDate − 1 day (per existing ETS logic)
  let etsDate = null;
  if (visitDate instanceof Date && !isNaN(visitDate)) {
    etsDate = new Date(visitDate.getTime() - 86400000);
  }

  tracker.getRange(row, 7).setValue('TRUNO');              // G: RSA Vendor
  tracker.getRange(row, 8).setValue(visitDate || '');      // H: RSA Scheduled Date
  tracker.getRange(row, 10).setValue('Scheduled RSA');     // J: RSA Status
  if (etsDate) tracker.getRange(row, 9).setValue(etsDate); // I: ETS Reminder Date
  tracker.getRange(row, 18).setValue(now);                 // R: Last Updated
  SpreadsheetApp.flush();

  const visitStr = visitDate instanceof Date ? _fmtDate(visitDate) : String(visitDate || 'TBD');
  const etsStr   = etsDate ? _fmtDate(etsDate) : 'N/A';

  // Post Coffee Break reminder to ETS channel
  const blocks = [
    {
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: `:calendar: *ETS Coffee Break Reminder*\n` +
              `${ETS_USERGROUP_HANDLE}\n\n` +
              `Cart *\`${cartId}\`* at *${store}*${state ? ` (${state})` : ''} has a TRUNO RSA inspection on *${visitStr}*.\n\n` +
              `:white_check_mark: *Day before (${etsStr})*: Put cart *ON Coffee Break*\n` +
              `:no_entry_sign: *Inspection day (${visitStr})*: Turn *OFF Coffee Break* before technician arrives`,
      },
    },
    {
      type: 'actions',
      elements: [{
        type: 'button',
        text: { type: 'plain_text', text: 'View Tracker →', emoji: true },
        url: _webAppUrl(cartId),
        style: 'primary',
      }],
    },
  ];

  try {
    postToSlack({
      channel: ETS_SLACK_CHANNEL_ID,
      text:    `:calendar: ETS Reminder: Cart ${cartId} at ${store} — TRUNO inspection ${visitStr}. Put ON Coffee Break ${etsStr}.`,
      blocks,
    });
  } catch(e) {
    Logger.log('_handleTrunoScheduled Slack error: ' + e.message);
  }

  Logger.log(`_handleTrunoScheduled: ${cartId} → Scheduled RSA, ETS set to ${etsStr}`);
}


/**
 * DISABLED / no longer called as of the bot's form-driven results flow.
 * RSA completion is owned by the Python bot (drive_monitor.py + test_forms.py):
 * it reads the actual Scale Test Form and writes Pass→Completed RSA (next step
 * Pending W&M) or Fail→Failed RSA with the report link, then tags the CSM — which
 * a blind "status = Completed" handler can't do (it would mislabel failures).
 * Kept here only so GAS completion handling can be restored if the bot is retired:
 * delete the `if (status === 'completed') {...continue;}` early-skip in
 * monitorTrunoTracker and this function runs again (its dispatch line is intact).
 */
function _handleTrunoCompleted(tracker, cartId, store, now) {
  const { row, rowData } = _findCartRow(tracker, cartId, store);
  if (!row) { Logger.log(`_handleTrunoCompleted: Cart ${cartId} not found in RSA Tracker.`); return; }

  tracker.getRange(row, 10).setValue('Completed RSA');    // J: RSA Status
  tracker.getRange(row, 11).setValue(now);                // K: RSA Completion Date
  tracker.getRange(row, 15).setValue('Pending W&M');      // O: Overall Status
  tracker.getRange(row, 18).setValue(now);                // R: Last Updated
  SpreadsheetApp.flush();

  const state = rowData ? String(rowData[2]).trim() : '';

  // Slack notification to W&M / CSM channel
  const blocks = [
    {
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: `:white_check_mark: *RSA Inspection Completed*\n` +
              `Cart *\`${cartId}\`* at *${store}*${state ? ` (${state})` : ''} has had its TRUNO RSA inspection completed.\n\n` +
              `:next_track_button: *Next step*: Schedule and complete W&M inspection\n` +
              `:pushpin: RSA Status updated to *Completed RSA* | Overall: *Pending W&M*`,
      },
    },
    {
      type: 'actions',
      elements: [{
        type: 'button',
        text: { type: 'plain_text', text: 'Update W&M in Tracker →', emoji: true },
        url: _webAppUrl(cartId),
        style: 'primary',
      }],
    },
  ];

  try {
    postToSlack({
      channel: CSM_SLACK_CHANNEL,
      text:    `✅ RSA Complete: Cart ${cartId} at ${store} — now Pending W&M. Please schedule W&M inspection.`,
      blocks,
    });
  } catch(e) {
    Logger.log('_handleTrunoCompleted Slack error: ' + e.message);
  }

  // Also send email to CSM
  try {
    const subject = `RSA Inspection Complete — W&M Scheduling Required: ${cartId} at ${store}`;
    const html = _buildCompletionEmail({ cartId, store, state, now });
    GmailApp.sendEmail(CSM_EMAIL, subject, '', { htmlBody: html, name: 'RSA Operations Agent' });
  } catch(e) {
    Logger.log('_handleTrunoCompleted email error: ' + e.message);
  }

  Logger.log(`_handleTrunoCompleted: ${cartId} → Completed RSA, CSM notified.`);
}


/**
 * Handles a Truno visit being "Cancelled".
 * - Resets RSA Status to "Pending RSA", clears scheduled date
 * - Alerts DRI in the W&M channel
 */
function _handleTrunoCancelled(tracker, cartId, store, now) {
  const { row, rowData } = _findCartRow(tracker, cartId, store);
  if (!row) { Logger.log(`_handleTrunoCancelled: Cart ${cartId} not found in RSA Tracker.`); return; }

  const dri = rowData ? String(rowData[16]).trim() : 'HW Ops';
  const driLower = dri.toLowerCase();
  const matched = Object.keys(SLACK_USER_IDS).find(n => driLower.includes(n));
  const driSlackId = matched ? SLACK_USER_IDS[matched] : DEFAULT_SLACK_USER_ID;

  tracker.getRange(row, 8).setValue('');               // H: Clear RSA Scheduled Date
  tracker.getRange(row, 10).setValue('Pending RSA');   // J: Reset RSA Status
  tracker.getRange(row, 18).setValue(now);             // R: Last Updated
  SpreadsheetApp.flush();

  try {
    postToSlack({
      channel: WM_SLACK_CHANNEL_ID,
      text: `:x: TRUNO visit CANCELLED for cart ${cartId} at ${store}. RSA reset to Pending RSA. <@${driSlackId}> please reschedule.`,
      blocks: [{
        type: 'section',
        text: {
          type: 'mrkdwn',
          text: `:x: *TRUNO Visit Cancelled*\n<@${driSlackId}> Cart *\`${cartId}\`* at *${store}* — Truno cancelled the scheduled RSA visit.\nRSA Status has been reset to *Pending RSA*. Please reschedule with Truno.`,
        },
      }, {
        type: 'actions',
        elements: [{ type: 'button', text: { type: 'plain_text', text: 'Open cart in RSA Tracker →', emoji: true },
                     url: _webAppUrl(cartId), style: 'primary' }],
      }],
    });
  } catch(e) {
    Logger.log('_handleTrunoCancelled Slack error: ' + e.message);
  }

  Logger.log(`_handleTrunoCancelled: ${cartId} reset to Pending RSA, DRI alerted.`);
}


// ═══════════════════════════════════════════════════════════════════════════════
// 3. CHECK COMPLETED VISITS
//    Daily scan for carts that have RSA Status = "Completed RSA" but
//    W&M Status is still Pending — sends CSM reminder if they've been pending
//    W&M for more than 3 days.
// ═══════════════════════════════════════════════════════════════════════════════

function checkCompletedVisits() {
  // DISABLED: W&M inspection scheduling is out of scope, so the "W&M Scheduling Needed"
  // nudge is retired. Left in place (unreferenced) for history; return immediately.
  Logger.log('checkCompletedVisits skipped — W&M out of scope.');
  return;
  /* eslint-disable no-unreachable */
  const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
  const tracker = ss.getSheetByName('RSA Tracker');
  if (!tracker) return;

  const rows = _getAllTrackerRows();
  if (!rows) return;

  const now     = new Date();
  const todayMs = now.getTime();
  const THREE_DAYS_MS = 3 * 86400000;

  const props  = PropertiesService.getScriptProperties();
  const log    = JSON.parse(props.getProperty('csmAlerts') || '{}');

  const toAlert = rows.filter(r => {
    const rsaStatus = String(r[9]).trim();
    const wmStatus  = String(r[12]).trim();
    const lastUpd   = r[17];

    if (rsaStatus !== 'Completed RSA') return false;
    // Only nudge when W&M is still genuinely pending/unstarted. Excludes Passed/Completed
    // AND Failed W&M (was: only Passed/Completed → failed carts got re-nudged forever) and
    // Scheduled W&M (already booked).
    if (wmStatus !== '' && wmStatus !== 'Pending W&M') return false;
    if (!lastUpd || !(lastUpd instanceof Date)) return false;

    const daysSince = (todayMs - lastUpd.getTime()) / 86400000;
    return daysSince >= 3;
  });

  // Batch by store
  const byStore = {};
  toAlert.forEach(r => {
    const store  = String(r[1]).trim();
    const key    = `csm_${String(r[0]).trim()}`.replace(/[^a-zA-Z0-9_]/g, '_');
    if (log[key] && (todayMs - new Date(log[key]).getTime()) / 86400000 < 3) return;
    if (!byStore[store]) byStore[store] = [];
    // days = real days pending (from Last Updated, col R) — NOT col P (r[15]), which is an
    // unrelated value and rendered as 'NaNd'. lastUpd is a Date here (filtered above).
    const daysPending = Math.floor((todayMs - r[17].getTime()) / 86400000);
    byStore[store].push({ cartId: String(r[0]).trim(), days: daysPending, alertKey: key });
  });

  for (const [store, carts] of Object.entries(byStore)) {
    const cartLines = carts.map(c =>
      `• \`${c.cartId}\` — RSA done, W&M pending${c.days ? ` (${Math.floor(c.days)}d)` : ''}`
    ).join('\n');

    const blocks = [{
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: `:pushpin: *W&M Scheduling Needed*\n\n` +
              `*${store}* — ${carts.length} cart${carts.length > 1 ? 's' : ''} have completed RSA but still need W&M:\n\n` +
              `${cartLines}\n\n` +
              `:calendar: Please schedule and complete the Weights & Measures inspection.`,
      },
    }, {
      type: 'actions',
      elements: [{ type: 'button', text: { type: 'plain_text', text: 'Update Tracker →' }, url: _webAppUrl(), style: 'primary' }],
    }];

    try {
      postToSlack({ channel: CSM_SLACK_CHANNEL, text: `W&M needed: ${carts.length} cart(s) at ${store} — RSA complete`, blocks });
      carts.forEach(c => { log[c.alertKey] = now.toISOString(); });
    } catch(e) {
      Logger.log('checkCompletedVisits Slack error: ' + e.message);
    }

    // Email CSM
    try {
      GmailApp.sendEmail(
        CSM_EMAIL,
        `W&M Scheduling Required — ${carts.length} cart(s) at ${store}`,
        '',
        {
          htmlBody: _buildWMNeededEmail(store, carts),
          name: 'RSA Operations Agent',
        }
      );
    } catch(e) {
      Logger.log('checkCompletedVisits email error: ' + e.message);
    }
  }

  props.setProperty('csmAlerts', JSON.stringify(log));
  Logger.log('checkCompletedVisits complete. Notified ' + Object.keys(byStore).length + ' store(s).');
}


// ═══════════════════════════════════════════════════════════════════════════════
// 4. INSPECTION FILE INGESTION
//    Reads Excel/CSV inspection files uploaded to the Google Drive folder,
//    parses results, and:
//      a) Adds/updates rows in the RSA Tracker
//      b) Adds a "Tech Upload Link" column (col T) with a link to the source file
//      c) Archives a copy of the data as a timestamped sheet tab
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * Main entry point — scans the inspection folder for new files and processes each.
 * Files are tracked by Drive file ID in script properties so they're only processed once.
 */
function ingestInspectionFiles() {
  const folder = DriveApp.getFolderById(INSPECTION_FOLDER_ID);
  const files  = folder.getFiles();

  const props      = PropertiesService.getScriptProperties();
  const ingestedRaw = props.getProperty('ingestedFiles') || '{}';
  const ingested   = JSON.parse(ingestedRaw);

  let count = 0;

  while (files.hasNext()) {
    const file     = files.next();
    const fileId   = file.getId();
    const fileName = file.getName();
    const mimeType = file.getMimeType();

    // Only process Excel and CSV files we haven't seen yet
    if (ingested[fileId]) continue;
    const isExcel = mimeType === MimeType.MICROSOFT_EXCEL ||
                    mimeType === MimeType.MICROSOFT_EXCEL_LEGACY ||
                    fileName.endsWith('.xlsx') || fileName.endsWith('.xls');
    const isCsv   = mimeType === MimeType.CSV || fileName.endsWith('.csv');

    if (!isExcel && !isCsv) continue;

    Logger.log(`ingestInspectionFiles: Processing "${fileName}" (${fileId})`);

    try {
      const rows = isCsv
        ? _parseCSVFile(file)
        : _parseExcelInspectionFile(file, fileName);

      if (rows && rows.length > 0) {
        _importInspectionRows(rows, fileName, fileId, file.getUrl());
        count++;
      }

      ingested[fileId] = new Date().toISOString();
    } catch(e) {
      Logger.log(`ingestInspectionFiles: Error on "${fileName}" — ${e.message}`);
    }
  }

  props.setProperty('ingestedFiles', JSON.stringify(ingested));
  Logger.log(`ingestInspectionFiles complete: ${count} new file(s) processed.`);
}


/**
 * Parses a CSV file from Drive into an array of row objects.
 * Expects headers in row 1. Returns [{ cartId, store, state, testDate, wmResult, notes, ... }]
 */
function _parseCSVFile(file) {
  const csv  = file.getBlob().getDataAsString('UTF-8');
  const rows = Utilities.parseCsv(csv);
  if (rows.length < 2) return [];

  const headers = rows[0].map(h => h.trim().toLowerCase());
  return rows.slice(1).map(row => {
    const obj = {};
    headers.forEach((h, i) => { obj[h] = String(row[i] || '').trim(); });
    return _normaliseInspectionRow(obj);
  }).filter(r => r && r.cartId);
}


/**
 * For Excel files — uses the Google Drive conversion trick:
 * Export the xlsx as CSV (first sheet), then parse.
 */
function _parseExcelInspectionFile(file, fileName) {
  // Export the first sheet of the xlsx as CSV via Drive export URL
  const exportUrl = `https://docs.google.com/spreadsheets/d/${file.getId()}/export?format=csv`;
  let csv;
  try {
    const resp = UrlFetchApp.fetch(exportUrl, {
      headers:  { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
      muteHttpExceptions: true,
    });
    if (resp.getResponseCode() !== 200) throw new Error('HTTP ' + resp.getResponseCode());
    csv = resp.getContentText();
  } catch(e) {
    // Fallback: open as a Sheets-converted doc if possible
    Logger.log(`_parseExcelInspectionFile export failed: ${e.message}. Trying direct blob.`);
    csv = file.getBlob().getDataAsString('UTF-8');
  }

  const rows = Utilities.parseCsv(csv);
  if (rows.length < 2) return [];

  const headers = rows[0].map(h => h.trim().toLowerCase());
  return rows.slice(1).map(row => {
    const obj = {};
    headers.forEach((h, i) => { obj[h] = String(row[i] || '').trim(); });
    return _normaliseInspectionRow(obj);
  }).filter(r => r && r.cartId);
}


/**
 * Normalises a raw parsed row object into the standard inspection schema,
 * regardless of column naming variations across tech uploads.
 */
function _normaliseInspectionRow(obj) {
  // Try multiple common header names for each field
  const get = (...keys) => {
    for (const k of keys) {
      if (obj[k] != null && obj[k] !== '') return obj[k];
    }
    return '';
  };

  const cartId = get('cart id', 'cart_id', 'serial', 'cart no', 'cartid', 'cart number');
  if (!cartId) return null;

  return {
    cartId:   cartId,
    store:    get('store', 'retailer', 'location', 'store name'),
    state:    get('state', 'st').toUpperCase(),
    testDate: get('test date', 'date', 'inspection date', 'visit date'),
    wmResult: get('result', 'wm result', 'outcome', 'pass/fail', 'status'),
    trigger:  get('trigger', 'service trigger', 'type', 'reason'),
    notes:    get('notes', 'note', 'comments', 'remarks'),
  };
}


/**
 * Writes the normalised inspection rows to the RSA Tracker and archives
 * the file data as a new sheet tab. Also stamps col T with a link to the file.
 */
function _importInspectionRows(rows, fileName, fileId, fileUrl) {
  const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
  const tracker = ss.getSheetByName('RSA Tracker');
  if (!tracker) return;

  const now    = new Date();
  const nowStr = Utilities.formatDate(now, Session.getScriptTimeZone(), 'MM/dd/yyyy HH:mm');

  let updated = 0;
  let added   = 0;

  for (const row of rows) {
    if (!row.cartId) continue;

    const { row: sheetRow, rowData } = _findCartRow(tracker, row.cartId, row.store);
    const rawResult  = String(row.wmResult || '').trim().toLowerCase();
    // Classify pass / fail / AMBIGUOUS. Ambiguous results ('pending', 'partial',
    // 'postponed', …) must neither auto-pass (the old bare-'p' prefix marked them
    // 'Passed W&M') NOR be mislabeled 'Failed' — leave the W&M status unchanged instead.
    const AMBIG_KW   = ['pend', 'partial', 'problem', 'postpon', 'progress', 'review',
                        'tbd', 'incomplete', 'unknown', 'n/a', 'na'];
    const PASS_KW    = ['p', 'pass', 'recalib', 'calib', 'complet', 'ok', 'clear'];   // 'p' = single-letter Pass
    const FAIL_KW    = ['f', 'fail', 'no', 'redo', 'reject', 'unsafe'];
    const ambiguous  = !!rawResult && AMBIG_KW.some(k => rawResult.startsWith(k));
    const wmPassed   = !!rawResult && !ambiguous && PASS_KW.some(k => rawResult.startsWith(k));
    const wmFailed   = !!rawResult && !ambiguous && !wmPassed && FAIL_KW.some(k => rawResult.startsWith(k));
    const wmStatus   = wmPassed ? 'Passed W&M' : (wmFailed ? 'Failed W&M' : '');   // '' = no change
    const testDate   = row.testDate || nowStr;

    // Ensure col T (Upload Link) exists — extend tracker if needed
    _ensureUploadLinkColumn(tracker);

    if (sheetRow) {
      // Update existing row
      if (wmStatus) tracker.getRange(sheetRow, 13).setValue(wmStatus);   // M: W&M Status
      if (testDate) tracker.getRange(sheetRow, 14).setValue(testDate);   // N: W&M Date
      if (row.notes) {
        const existing = String(tracker.getRange(sheetRow, 19).getValue() || '').trim();
        const newNote  = existing ? `${existing}\n[${nowStr}] ${row.notes}` : `[${nowStr}] ${row.notes}`;
        tracker.getRange(sheetRow, 19).setValue(newNote);
      }
      // Stamp tech upload link in col T
      tracker.getRange(sheetRow, UPLOAD_LINK_COL).setFormula(
        `=HYPERLINK("${fileUrl}","${fileName}")`
      );
      updated++;
    } else if (row.cartId && row.store) {
      // Auto-add new row
      const addResult = _botAddCart({
        cartId:   row.cartId,
        store:    row.store,
        state:    row.state || 'NJ',
        trigger:  row.trigger || '',
        wmStatus: wmStatus   || 'Pending W&M',
        dri:      'HW Ops',
        notes:    `[${nowStr}] Ingested from ${fileName}${row.notes ? ' — ' + row.notes : ''}`,
      });
      if (addResult.row) {
        tracker.getRange(addResult.row, UPLOAD_LINK_COL).setFormula(
          `=HYPERLINK("${fileUrl}","${fileName}")`
        );
      }
      added++;
    }
  }

  SpreadsheetApp.flush();

  // Archive the file data as a new tab
  _archiveInspectionTab(ss, fileName, nowStr, rows, fileUrl);

  // Post to Slack
  try {
    postToSlack({
      channel: WM_SLACK_CHANNEL_ID,
      text: `📂 Inspection file ingested: "${fileName}" — ${updated} updated, ${added} added.`,
      blocks: [{
        type: 'section',
        text: {
          type: 'mrkdwn',
          text: `:file_folder: *Inspection File Ingested*\n` +
                `File: *${fileName}*\n` +
                `Updated: *${updated}* carts  |  Added: *${added}* new carts\n` +
                `<${fileUrl}|📎 View source file>  ·  <${_webAppUrl()}|📊 View Tracker>`,
        },
      }],
    });
  } catch(e) {
    Logger.log('_importInspectionRows Slack error: ' + e.message);
  }

  Logger.log(`_importInspectionRows: "${fileName}" — updated=${updated}, added=${added}`);
}


/**
 * Ensures column T (UPLOAD_LINK_COL) has a header in row 3 (or the header row).
 * Only runs if the header cell is blank.
 */
function _ensureUploadLinkColumn(tracker) {
  try {
    const headerCell = tracker.getRange(3, UPLOAD_LINK_COL);
    if (!String(headerCell.getValue()).trim()) {
      headerCell.setValue('Tech Upload');
      headerCell.setBackground('#2e5fa3').setFontColor('#ffffff').setFontWeight('bold');
    }
  } catch(e) {}
}


/**
 * Creates a timestamped archive tab for the ingested inspection file data.
 */
function _archiveInspectionTab(ss, fileName, nowStr, rows, fileUrl) {
  let tabName;
  try {
    const parts = nowStr.split(' ');
    const dp    = parts[0].split('/');
    tabName = `Ingest ${dp[2]}-${dp[0]}-${dp[1]} ${parts[1] || ''}`.trim();
  } catch(e) { tabName = `Ingest ${nowStr}`; }

  // Deduplicate tab name
  let finalName = tabName.substring(0, 95);
  let c = 2;
  while (ss.getSheetByName(finalName)) { finalName = `${tabName.substring(0, 92)} (${c++})`; }

  const tab = ss.insertSheet(finalName);
  tab.getRange('A1').setValue('Inspection File Ingest');
  tab.getRange('B1').setValue(fileName);
  tab.getRange('A2').setValue('Source');
  tab.getRange('B2').setFormula(`=HYPERLINK("${fileUrl}","${fileName}")`);
  tab.getRange('A3').setValue('Imported');
  tab.getRange('B3').setValue(nowStr);
  tab.getRange('A1:C3').setBackground('#1a2e4a').setFontColor('#ffffff').setFontWeight('bold');

  const headers = ['Cart ID', 'Store', 'State', 'Test Date', 'W&M Result', 'Trigger', 'Notes'];
  const hRange  = tab.getRange(5, 1, 1, headers.length);
  hRange.setValues([headers]);
  hRange.setBackground('#2e4a6e').setFontColor('#ffffff').setFontWeight('bold');

  if (rows.length > 0) {
    tab.getRange(6, 1, rows.length, headers.length).setValues(
      rows.map(r => [r.cartId, r.store, r.state, r.testDate, r.wmResult, r.trigger, r.notes])
    );
  }
  tab.setFrozenRows(5);
}


// ═══════════════════════════════════════════════════════════════════════════════
// EMAIL BUILDERS
// ═══════════════════════════════════════════════════════════════════════════════

function _buildCompletionEmail({ cartId, store, state, now }) {
  const dateStr = Utilities.formatDate(now, Session.getScriptTimeZone(), 'MMM dd, yyyy');
  return `<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 16px">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.10)">
  <tr><td style="background:#1a2e4a;padding:20px 28px">
    <div style="color:#fff;font-size:16px;font-weight:bold">RSA Inspection Complete — W&amp;M Required</div>
    <div style="color:#7faabf;font-size:11px">Instacart Hardware Ops · RSA Agent</div>
  </td></tr>
  <tr><td style="background:#c6efce18;border-left:4px solid #276221;padding:10px 28px">
    <span style="color:#276221;font-size:13px;font-weight:bold">✅ RSA Inspection Completed</span>
  </td></tr>
  <tr><td style="padding:24px 28px">
    <p style="font-size:14px;color:#1a2e4a;margin:0 0 12px">Hi CSM Team,</p>
    <p style="font-size:14px;color:#4b5563;margin:0 0 20px;line-height:1.6">
      The TRUNO RSA inspection for the following cart has been completed.
      Please schedule and complete the Weights &amp; Measures (W&amp;M) inspection to finish the Return to Service process.
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;margin-bottom:20px">
      <tr style="background:#f9fafb">
        <td style="padding:12px 16px;border-right:1px solid #e5e7eb;width:50%">
          <div style="font-size:10px;color:#9ca3af;text-transform:uppercase">Cart ID</div>
          <div style="font-size:14px;font-weight:bold;color:#1a2e4a;font-family:monospace">${cartId}</div>
        </td>
        <td style="padding:12px 16px;width:50%">
          <div style="font-size:10px;color:#9ca3af;text-transform:uppercase">Store</div>
          <div style="font-size:14px;font-weight:bold;color:#1a2e4a">${store}${state ? ` (${state})` : ''}</div>
        </td>
      </tr>
      <tr>
        <td colspan="2" style="padding:12px 16px;border-top:1px solid #e5e7eb">
          <div style="font-size:10px;color:#9ca3af;text-transform:uppercase">RSA Completed On</div>
          <div style="font-size:14px;color:#1a2e4a">${dateStr}</div>
        </td>
      </tr>
    </table>
    <table cellpadding="0" cellspacing="0">
      <tr><td style="background:#1a2e4a;border-radius:7px">
        <a href="${_webAppUrl(cartId)}" style="display:inline-block;padding:11px 22px;color:#fff;font-size:13px;font-weight:bold;text-decoration:none">View &amp; Update Tracker →</a>
      </td></tr>
    </table>
  </td></tr>
  <tr><td style="background:#f9fafb;border-top:1px solid #e5e7eb;padding:12px 28px">
    <p style="font-size:11px;color:#9ca3af;margin:0">Automated message from RSA Operations Agent · Instacart Hardware Ops</p>
  </td></tr>
</table>
</td></tr></table>
</body></html>`;
}


function _buildWMNeededEmail(store, carts) {
  const rows = carts.map(c =>
    `<tr><td style="padding:9px 14px;border-bottom:1px solid #e5e7eb;font-family:monospace;font-size:13px;color:#1a2e4a">${c.cartId}</td>
     <td style="padding:9px 14px;border-bottom:1px solid #e5e7eb;color:#4b5563">${c.days ? Math.floor(c.days) + 'd in status' : '—'}</td></tr>`
  ).join('');

  return `<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 16px">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.10)">
  <tr><td style="background:#1a2e4a;padding:20px 28px">
    <div style="color:#fff;font-size:16px;font-weight:bold">W&amp;M Scheduling Required</div>
    <div style="color:#7faabf;font-size:11px">Instacart Hardware Ops · RSA Agent</div>
  </td></tr>
  <tr><td style="background:#dce9fb18;border-left:4px solid #2e5fa3;padding:10px 28px">
    <span style="color:#2e5fa3;font-size:13px;font-weight:bold">📅 ${carts.length} Cart(s) Need W&amp;M at ${store}</span>
  </td></tr>
  <tr><td style="padding:24px 28px">
    <p style="font-size:14px;color:#4b5563;margin:0 0 16px;line-height:1.6">
      The following carts at <strong>${store}</strong> have completed RSA inspection but still require W&amp;M scheduling:
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;margin-bottom:20px">
      <tr style="background:#f9fafb">
        <th style="padding:9px 14px;text-align:left;font-size:12px;color:#6b7280;border-bottom:1px solid #e5e7eb">Cart ID</th>
        <th style="padding:9px 14px;text-align:left;font-size:12px;color:#6b7280;border-bottom:1px solid #e5e7eb">Status</th>
      </tr>
      ${rows}
    </table>
    <table cellpadding="0" cellspacing="0">
      <tr><td style="background:#1a2e4a;border-radius:7px">
        <a href="${_webAppUrl()}" style="display:inline-block;padding:11px 22px;color:#fff;font-size:13px;font-weight:bold;text-decoration:none">Schedule W&amp;M in Tracker →</a>
      </td></tr>
    </table>
  </td></tr>
</table>
</td></tr></table>
</body></html>`;
}


// ═══════════════════════════════════════════════════════════════════════════════
// TRIGGER INSTALLER
// Run this ONCE to register all automation triggers.
// Apps Script editor → select createAutomationTriggers → Run
// ═══════════════════════════════════════════════════════════════════════════════


// ═══════════════════════════════════════════════════════════════════════════════
// DASHBOARD: delete an incorrectly-added record
// Called from Index.html via google.script.run.deleteCartRow({cartId, sourceSheet}).
// Hard-deletes the row from the RSA Tracker (or Archive). Returns {ok,...} or {error}.
// ═══════════════════════════════════════════════════════════════════════════════
function deleteCartRow(params) {
  try {
    params = params || {};
    var cartId = String(params.cartId || '').trim();
    if (!cartId) return { error: 'No cartId provided — nothing to delete.' };

    var ss    = SpreadsheetApp.openById(SPREADSHEET_ID);
    var sheet = _resolveDashboardSheet_(ss, params.sourceSheet);
    if (!sheet) return { error: 'Could not find the "' + (params.sourceSheet || 'RSA Tracker') + '" sheet.' };

    // Find the row by an exact Cart ID match in column A.
    var data  = sheet.getDataRange().getValues();
    var match = -1;
    for (var i = 0; i < data.length; i++) {
      if (String(data[i][0] || '').trim().toLowerCase() === cartId.toLowerCase()) { match = i + 1; break; }
    }
    if (match === -1) return { error: 'Cart "' + cartId + '" was not found in ' + sheet.getName() + '. It may have already been removed — refresh.' };

    var store = String(data[match - 1][1] || '').trim();
    sheet.deleteRow(match);

    // Best-effort audit trail (visible in Apps Script → Executions).
    try {
      var who = (Session.getActiveUser() && Session.getActiveUser().getEmail()) || 'unknown user';
      Logger.log('RSA delete: cart ' + cartId + ' (' + store + ') removed from ' + sheet.getName() + ' by ' + who);
    } catch (e) {}

    return { ok: true, cartId: cartId, store: store, sheet: sheet.getName(), deletedRow: match };
  } catch (err) {
    return { error: 'deleteCartRow: ' + err.message };
  }
}

// Resolve the dashboard's source-sheet label to a real sheet. "RSA Tracker" maps
// directly; "Archive" tolerates a differently-named archive tab (any sheet whose name
// contains "archive"). Falls back to the RSA Tracker.
function _resolveDashboardSheet_(ss, sourceSheet) {
  var want   = String(sourceSheet || 'RSA Tracker').trim();
  var direct = ss.getSheetByName(want);
  if (direct) return direct;
  if (want.toLowerCase() === 'archive') {
    var sheets = ss.getSheets();
    for (var i = 0; i < sheets.length; i++) {
      if (sheets[i].getName().toLowerCase().indexOf('archive') !== -1) return sheets[i];
    }
  }
  return ss.getSheetByName('RSA Tracker');
}


// ═══════════════════════════════════════════════════════════════════════════════
// DASHBOARD: let Ops add/correct a Tech Results value (col V) by hand.
// Called from Index.html via google.script.run.setTechResultManual({cartId, sourceSheet,
// techResults}). Writes (or clears, if blank) the readings into the "Tech Results"
// column, resolved by header name so it works regardless of exact column position.
// Also clears the Tech Results Link cell when the value is cleared (removes junk links).
// ═══════════════════════════════════════════════════════════════════════════════
function setTechResultManual(params) {
  try {
    params = params || {};
    var cartId = String(params.cartId || '').trim();
    if (!cartId) return { error: 'No cartId provided.' };

    var ss    = SpreadsheetApp.openById(SPREADSHEET_ID);
    var sheet = _resolveDashboardSheet_(ss, params.sourceSheet);
    if (!sheet) return { error: 'Could not find the "' + (params.sourceSheet || 'RSA Tracker') + '" sheet.' };

    var col = _headerCol_(sheet, 'tech results');
    if (!col) return { error: 'No "Tech Results" column found on ' + sheet.getName() + '.' };

    var data = sheet.getDataRange().getValues();
    var match = -1;
    for (var i = 0; i < data.length; i++) {
      if (String(data[i][0] || '').trim().toLowerCase() === cartId.toLowerCase()) { match = i + 1; break; }
    }
    if (match === -1) return { error: 'Cart "' + cartId + '" not found in ' + sheet.getName() + '.' };

    var val = String(params.techResults == null ? '' : params.techResults).trim();
    sheet.getRange(match, col).setValue(val);

    // If Ops clears the reading, drop the (possibly junk) link too.
    if (!val) {
      var linkCol = _headerCol_(sheet, 'tech results link');
      if (linkCol) sheet.getRange(match, linkCol).setValue('');
    }
    return { ok: true, cartId: cartId, techResults: val, col: col };
  } catch (err) {
    return { error: 'setTechResultManual: ' + err.message };
  }
}

// Find a column by its header text (case-insensitive), scanning the first 3 rows.
function _headerCol_(sheet, headerLower) {
  var lastCol = sheet.getLastColumn();
  if (lastCol < 1) return 0;
  var rows = Math.min(3, sheet.getLastRow() || 1);
  var top  = sheet.getRange(1, 1, rows, lastCol).getValues();
  for (var r = 0; r < top.length; r++) {
    for (var c = 0; c < top[r].length; c++) {
      if (String(top[r][c]).trim().toLowerCase() === headerLower) return c + 1;
    }
  }
  return 0;
}


// ═══════════════════════════════════════════════════════════════════════════════
// TRUNO SYNC ALIASES
// runDailyTrunoSync is installed as a time-based trigger in the GAS dashboard.
// It calls syncTrunoScheduledDates(), which delegates to monitorTrunoTracker()
// (the canonical Truno→RSA sync).  Both names are kept so the trigger works
// without requiring a dashboard change.
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * Alias called by the runDailyTrunoSync time-based trigger.
 * Syncs Truno scheduled/completed/cancelled rows back to RSA Tracker.
 */
function syncTrunoScheduledDates() {
  monitorTrunoTracker();
}

/**
 * Entry-point for the manually-created "runDailyTrunoSync" GAS trigger.
 * Kept here so the function is defined; delegates to syncTrunoScheduledDates().
 */
function runDailyTrunoSync() {
  syncTrunoScheduledDates();
}


function createAutomationTriggers() {
  // Remove existing triggers to prevent duplicates
  const MANAGED_FUNCTIONS = ['checkAndSendAlerts', 'masterDailyRun', 'monitorTrunoTracker',
    'ingestInspectionFiles', 'emailNicoleForNewTrunoRows', 'runDailyTrunoSync'];
  ScriptApp.getProjectTriggers()
    .filter(t => MANAGED_FUNCTIONS.includes(t.getHandlerFunction()))
    .forEach(t => ScriptApp.deleteTrigger(t));

  // masterDailyRun at 9am — replaces the old checkAndSendAlerts trigger
  ScriptApp.newTrigger('masterDailyRun')
    .timeBased().everyDays(1).atHour(9).create();

  // monitorTrunoTracker every 2 hours (catches same-day updates)
  ScriptApp.newTrigger('monitorTrunoTracker')
    .timeBased().everyHours(2).create();

  // ingestInspectionFiles every 4 hours (new tech uploads processed quickly)
  ScriptApp.newTrigger('ingestInspectionFiles')
    .timeBased().everyHours(4).create();

  // emailNicoleForNewTrunoRows every 15 min — emails Truno for bot-created visits
  ScriptApp.newTrigger('emailNicoleForNewTrunoRows')
    .timeBased().everyMinutes(15).create();

  _ensureDRIDropdown();

  Logger.log([
    '✅ Automation triggers installed:',
    '  masterDailyRun     → daily at 9am',
    '  monitorTrunoTracker → every 2 hours',
    '  ingestInspectionFiles → every 4 hours',
    '  emailNicoleForNewTrunoRows → every 15 minutes',
  ].join('\n'));
}


/**
 * Removes all managed automation triggers (for pausing automation).
 */
function removeAutomationTriggers() {
  const MANAGED = ['checkAndSendAlerts', 'masterDailyRun', 'monitorTrunoTracker',
    'ingestInspectionFiles', 'emailNicoleForNewTrunoRows', 'runDailyTrunoSync'];
  ScriptApp.getProjectTriggers()
    .filter(t => MANAGED.includes(t.getHandlerFunction()))
    .forEach(t => ScriptApp.deleteTrigger(t));
  Logger.log('All automation triggers removed. Run createAutomationTriggers() to re-enable.');
}


// How long a Truno row may sit with NO progress before we send ONE follow-up nudge.
const TRUNO_FOLLOWUP_DAYS = 3;

/**
 * Pure decision for the Nicole-email trigger (no I/O — unit-testable).
 * Inputs: the stored record for a cart (object | legacy ISO string | undefined),
 * the row's current progress signature `sig` (status + whether a date is set),
 * `now` (Date), the follow-up window in days, and whether this is the first
 * (baseline) run. Returns { action, rec } where action is:
 *   'initial'  — send the first request email (a newly-seen cart)
 *   'followup' — ONE nudge, sent only after `followUpDays` with NO progress
 *   'baseline' — first run only: record pre-existing rows as inert (never email)
 *   'skip'     — already handled, progressed, or still inside the quiet window
 * Once we email or detect progress, rec.done is set so the cart is never emailed again.
 */
function _trunoEmailPlan(rec, sig, now, followUpDays, baseline) {
  if (typeof rec === 'string') rec = { sentAt: rec, sig: '', fu: true, done: false };  // legacy → no surprise nudge
  if (!rec) {
    if (baseline) return { action: 'baseline', rec: { sentAt: now.toISOString(), sig: sig, done: true } };
    return { action: 'initial', rec: { sentAt: now.toISOString(), sig: sig, fu: false, done: false } };
  }
  if (rec.done) return { action: 'skip', rec: rec };
  if (rec.sig && sig && sig !== rec.sig) {            // status/date changed → progress; stop emailing
    rec.done = true;
    return { action: 'skip', rec: rec };
  }
  const days = (now.getTime() - new Date(rec.sentAt).getTime()) / 86400000;
  if (!rec.fu && days >= followUpDays) {              // no progress for N days → one nudge, then done
    rec.fu = true; rec.fuAt = now.toISOString(); rec.done = true;
    return { action: 'followup', rec: rec };
  }
  return { action: 'skip', rec: rec };                // quiet window — nothing to do
}


// ═══════════════════════════════════════════════════════════════════════════════
// EMAIL NICOLE FOR BOT-CREATED TRUNO ROWS
// The Slack agent writes Truno rows directly via the Sheets API but can't send
// email (it runs as a service account). This trigger runs as the script owner —
// so GmailApp works — and emails Nicole for new agent-created visits.
//
// Cadence per cart (keyed on store|cart): ONE request email, then silence for
// TRUNO_FOLLOWUP_DAYS; if the row still shows no progress (no status change and no
// visit date), send exactly ONE follow-up nudge, then stop. Any progress → stop.
// Idempotent (Script Properties); baselines pre-existing rows; skips TEST carts.
// ═══════════════════════════════════════════════════════════════════════════════
function emailNicoleForNewTrunoRows() {
  const ss = SpreadsheetApp.openById(TRUNO_SPREADSHEET_ID);
  const sheets = ss.getSheets();
  const sh = sheets.find(s => s.getName().toLowerCase().includes('rsa')) || sheets[0];
  if (!sh) { Logger.log('emailNicole: Truno sheet not found.'); return; }

  const last = sh.getLastRow();
  if (last < 2) return;
  const data = sh.getRange(2, 1, last - 1, T_LAST_COL).getValues();

  const props = PropertiesService.getScriptProperties();
  const processed = JSON.parse(props.getProperty('trunoEmailed') || '{}');
  // First run: snapshot existing agent rows as already-emailed so we don't email
  // for rows that predate this trigger. Rows created after run #1 get emailed.
  const baseline = props.getProperty('trunoEmailBaseline') !== 'done';

  const now = new Date();
  let sent = 0;

  for (let i = 0; i < data.length; i++) {
    const row    = data[i];
    const store  = String(row[T_STORE      - 1] || '').trim();
    const cart   = String(row[T_CART       - 1] || '').trim();
    const address= String(row[T_ADDRESS    - 1] || '').trim();
    const visit  = row[T_VISIT_DATE - 1];
    const notes  = String(row[T_NOTES      - 1] || '');
    const status = String(row[T_STATUS     - 1] || '').trim();

    if (!/RSA Agent/i.test(notes))      continue;   // only agent-created rows
    if (/complet|cancel/i.test(status)) continue;   // not for finished / cancelled visits
    if (/test/i.test(cart))             continue;   // never email Nicole for test carts

    // Key on store+cart (NOT the date) so the request is tracked across re-schedules.
    const key = (`${store}|${cart}`).replace(/[^a-zA-Z0-9_|]/g, '_');

    // Progress signature: status + whether a real visit date is set. ANY change means
    // Nicole acted, so we stop emailing. No change for TRUNO_FOLLOWUP_DAYS → one nudge.
    const hasDate = (visit instanceof Date && !isNaN(visit));
    const sig     = String(status).toLowerCase() + '|' + (hasDate ? '1' : '0');

    const plan = _trunoEmailPlan(processed[key], sig, now, TRUNO_FOLLOWUP_DAYS, baseline);
    if (plan.action !== 'initial' && plan.action !== 'followup') {
      processed[key] = plan.rec;            // baseline / skip — record and move on (no email)
      continue;
    }

    try {
      const isFU       = plan.action === 'followup';
      const visitStr   = hasDate ? _fmtDate(visit) : 'TBD';
      const m          = notes.match(/RSA Agent\s*·\s*([^·\]]+)/i);
      const submitter  = m ? m[1].trim() : 'RSA Agent';
      const cleanNotes = notes.replace(/\[RSA Agent[^\]]*\]/i, '').trim();
      const subject    = (isFU ? 'Follow-up: ' : '')
                       + `RSA Inspection Request — ${cart} at ${store}`
                       + (hasDate ? ` · ${visitStr}` : '');
      const html = _buildTrunoEmail({
        cartId: cart, store: store, state: '', address: address, visitDate: visitStr,
        notes: cleanNotes, submittedBy: submitter, submittedAt: _fmtDate(now), followUp: isFU,
      });
      GmailApp.sendEmail(NICOLE_EMAIL, subject, '', { htmlBody: html, name: 'RSA Operations Agent', noReply: true });
      processed[key] = plan.rec;            // commit ONLY after a successful send (else retry next run)
      sent++;
      Logger.log('emailNicole: ' + plan.action + ' for ' + key);
    } catch (e) {
      Logger.log('emailNicole error for ' + key + ': ' + e.message);
    }
  }

  if (baseline) props.setProperty('trunoEmailBaseline', 'done');
  props.setProperty('trunoEmailed', JSON.stringify(processed));
  Logger.log('emailNicoleForNewTrunoRows complete — sent ' + sent);
}
