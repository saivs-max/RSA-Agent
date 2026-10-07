// ═══════════════════════════════════════════════════════════════════════════════
// RSA Agent — Bot API Endpoints (add these functions to your Code.gs)
// File: GAS_BotEndpoints.gs
//
// HOW TO USE:
//   Copy all functions from this file into your existing Code.gs in Apps Script.
//   Then re-deploy your Web App (Deploy → Manage Deployments → Edit → New version).
//   The bot will POST to your Web App URL with { action, ... } payloads.
//
// CORS NOTE:
//   Since the Slack bot calls this from a server, set "Who has access" = "Anyone"
//   in your deployment settings (the bot authenticates via the AGENT_SECRET below).
// ═══════════════════════════════════════════════════════════════════════════════

// ── Shared secret — the Slack bot sends this in every request ─────────────────
// Set the same value in your Slack bot's .env as AGENT_SECRET
const AGENT_SECRET = 'rsa-agent-secret-change-me';   // ⚠️ change before deploying

// ── Truno / Nicole configuration ──────────────────────────────────────────────
// Nicole is the Truno contact who receives scheduling notifications.
// HOW TO GET HER SLACK ID: Open Slack → click Nicole's name → ••• → Copy member ID
const NICOLE_SLACK_ID    = 'U0XXXXXXXXX';    // ⚠️ replace with Nicole's real Slack user ID
const NICOLE_EMAIL       = 'nicole@truno.com'; // ⚠️ replace with Nicole's real email
const TRUNO_CONTACT_NAME = 'Nicole';

// ── Winter Scales configuration (NJ-only alternative to TRUNO) ────────────────
// For stores in NJ the agent asks TRUNO vs Winter Scales instead of defaulting to
// TRUNO. Winter Scales has no tracker sheet, so we email these contacts to request
// scheduling and ops follows up directly. Keep this list in sync with the bot's
// WINTERSCALES_EMAILS env var.
const WINTERSCALES_EMAILS       = [
  'service@winterscale.com',
  'john.winter@winterscale.com',
  'rich.ianniello@winterscale.com',
];
const WINTERSCALES_CONTACT_NAME = 'Winter Scales Team';

// Column layout for the Truno RSA tracker spreadsheet (live "Sheet1").
// Mirrors the sheet's header row, left to right (1-indexed).
// Truno owns most columns; the agent only writes a few when scheduling.
const TRUNO_COL = {
  store:       1,   // A — IC Store Name
  trunoStore:  2,   // B — Truno Store Name (e.g. ISC0050-WKF-0113)
  cart:        3,   // C — Cart (number; may be a list like "1, 7")
  trunoCall:   4,   // D — Truno Call #
  address:     5,   // E — Address
  visitDate:   6,   // F — Scheduled Date
  arrivalTime: 7,   // G — Arrival Time of Tech
  testResults: 8,   // H — Test Results
  notes:       9,   // I — Notes
  formsSent:  10,   // J — Forms sent to team?
  status:     11,   // K — Status
  manager:    12,   // L — Manager
  wm:         13,   // M — W&M (market code, e.g. EWR / LAX)
};

// ── W&M Compliance Guide (read-only reference) ────────────────────────────────
// Store-level RSA/W&M requirements. When a cart is onboarded the agent looks up
// the store here and surfaces the pre/post-launch directive to the ops manager.
const COMPLIANCE_SPREADSHEET_ID = '126U07Y-b8D9w5Nw6GKkyo-P-9Yh9lARJtWotzPrBBRc';
const COMPLIANCE_SHEET_NAME     = 'Store Requirements';
// Column indices in the Store Requirements tab (1-indexed; data starts row 3).
const CG = {
  retailer: 2, nickname: 3, city: 4, state: 5, county: 7,
  preReq:  10, preDir:  13, preNotify:  14,
  atReq:   15, atDir:   18, atNotify:   19,
  postReq: 20, postDir: 23, postNotify: 24,
  cadence: 25, agency:  28,
};

/**
 * _webAppUrl — link to the RSA webapp (the searchable cart UI) instead of the raw
 * spreadsheet. With a cartId, deep-links to `.../exec?cart=<id>` so the webapp opens
 * on the Tracker tab already filtered to that cart — that's where users go to find it.
 * Falls back to TRACKER_URL (the sheet) if the web-app URL can't be resolved, so a
 * link is always returned.
 */
function _webAppUrl(cartId) {
  var base = '';
  try { base = ScriptApp.getService().getUrl() || ''; } catch (err) { base = ''; }
  if (!base) return (typeof TRACKER_URL !== 'undefined' && TRACKER_URL) ? TRACKER_URL : '';
  if (cartId) {
    var sep = base.indexOf('?') >= 0 ? '&' : '?';
    return base + sep + 'cart=' + encodeURIComponent(String(cartId));
  }
  return base;
}

/**
 * doPost — Entry point for all Slack bot API calls.
 *
 * Supported actions:
 *   updateCart  — Update one or more fields on an existing cart
 *   addCart     — Add a brand-new cart to the tracker
 *   queryCart   — Fetch data for a specific cart by cartId or store fragment
 *   getSummary  — Return high-level counts for a quick dashboard view
 *   listCarts   — Return a filtered list (by state, status, dri, storeFragment)
 */
function doPost(e) {
  const corsHeaders = {
    'Access-Control-Allow-Origin':  '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Content-Type':                 'application/json',
  };

  try {
    if (!e || !e.postData || !e.postData.contents) {
      return _jsonResp({ error: 'No POST body received.' }, corsHeaders);
    }

    const body   = JSON.parse(e.postData.contents);
    const secret = body.secret || '';

    // Simple shared-secret auth — reject unrecognised callers
    if (secret !== AGENT_SECRET) {
      return _jsonResp({ error: 'Unauthorized.' }, corsHeaders);
    }

    const action = String(body.action || '').toLowerCase();

    switch (action) {
      case 'updatecart':        return _jsonResp(_botUpdateCart(body),        corsHeaders);
      case 'addcart':           return _jsonResp(_botAddCart(body),           corsHeaders);
      case 'querycart':         return _jsonResp(_botQueryCart(body),         corsHeaders);
      case 'getsummary':        return _jsonResp(_botGetSummary(body),        corsHeaders);
      case 'listcarts':         return _jsonResp(_botListCarts(body),         corsHeaders);
      case 'scheduletruno':     return _jsonResp(_botScheduleTruno(body),     corsHeaders);
      case 'schedulewinterscales': return _jsonResp(_botScheduleWinterScales(body), corsHeaders);
      case 'fullonboardcart':   return _jsonResp(_botFullOnboardCart(body),   corsHeaders);
      case 'compliancecheck':   return _jsonResp(_botComplianceCheck(body),   corsHeaders);
      default:
        return _jsonResp({ error: `Unknown action: ${action}` }, corsHeaders);
    }
  } catch (err) {
    return _jsonResp({ error: 'doPost error: ' + err.message }, corsHeaders);
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// ACTION: updateCart
// Body fields:
//   cartId        (required) — exact Cart ID from col A, or partial match
//   storeHint     (optional) — store name fragment to disambiguate
//   rsaStatus     (optional) — new value for col J
//   wmStatus      (optional) — new value for col M
//   overallStatus (optional) — new value for col O
//   rsaVendor     (optional) — new value for col G
//   rsaSchedDate  (optional) — new value for col H  (ISO date string or MM/DD/YYYY)
//   wmDate        (optional) — new value for col N
//   dri           (optional) — new value for col Q
//   notes         (optional) — new value for col S (appends to existing if appendNotes=true)
//   appendNotes   (optional) — if true, prepend today's date + note text instead of replacing
// ─────────────────────────────────────────────────────────────────────────────
function _botUpdateCart(body) {
  try {
    const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
    const tracker = ss.getSheetByName('RSA Tracker');
    if (!tracker) return { error: 'RSA Tracker sheet not found.' };

    const { row, rowData, rowIdx } = _findCartRow(tracker, body.cartId, body.storeHint);
    if (!row) return { error: `Cart not found: "${body.cartId}"${body.storeHint ? ` at "${body.storeHint}"` : ''}` };

    // Columns to update (1-indexed, matching the T map in processWMData)
    // A=1 B=2 C=3 D=4 E=5 F=6 G=7 H=8 I=9 J=10 K=11 L=12 M=13 N=14 O=15 P=16 Q=17 R=18 S=19
    const updates = [];

    if (body.rsaVendor     != null) updates.push({ col: 7,  val: body.rsaVendor });
    if (body.rsaSchedDate  != null) updates.push({ col: 8,  val: _parseDate(body.rsaSchedDate) });
    if (body.rsaStatus     != null) updates.push({ col: 10, val: body.rsaStatus });
    if (body.wmStatus      != null) updates.push({ col: 13, val: body.wmStatus  });
    if (body.wmDate        != null) updates.push({ col: 14, val: _parseDate(body.wmDate) });
    if (body.overallStatus != null) updates.push({ col: 15, val: body.overallStatus });
    if (body.dri           != null) updates.push({ col: 17, val: body.dri });

    // Notes: append or replace
    if (body.notes != null) {
      let noteVal = body.notes;
      if (body.appendNotes) {
        const existing = String(rowData[18] || '').trim();
        const dateStr  = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'MM/dd/yyyy');
        noteVal = existing
          ? `${existing}\n[${dateStr}] ${body.notes}`
          : `[${dateStr}] ${body.notes}`;
      }
      updates.push({ col: 19, val: noteVal });
    }

    if (updates.length === 0) return { error: 'No fields to update were provided.' };

    // Write each field
    updates.forEach(u => tracker.getRange(row, u.col).setValue(u.val));

    // Stamp Last Updated (col R=18) whenever RSA Status (J=10) or W&M Status (M=13) changes
    const statusChanged = updates.some(u => u.col === 10 || u.col === 13);
    if (statusChanged) tracker.getRange(row, 18).setValue(new Date());

    SpreadsheetApp.flush();

    return {
      ok:       true,
      cartId:   String(rowData[0]).trim(),
      store:    String(rowData[1]).trim(),
      updated:  updates.map(u => _colName(u.col)),
      trackerUrl: _webAppUrl(String(rowData[0]).trim()),
    };
  } catch (err) {
    return { error: '_botUpdateCart: ' + err.message };
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// ACTION: addCart
// Body fields: cartId, store, state, trigger, rsaRequired, rsaVendor,
//              rsaStatus, wmStatus, overallStatus, dri, notes
// ─────────────────────────────────────────────────────────────────────────────
function _botAddCart(body) {
  try {
    const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
    const tracker = ss.getSheetByName('RSA Tracker');
    if (!tracker) return { error: 'RSA Tracker sheet not found.' };

    if (!body.cartId || !body.store) return { error: 'cartId and store are required.' };

    // Check for duplicate
    const { row: existingRow } = _findCartRow(tracker, body.cartId, body.store);
    if (existingRow) return { error: `Cart "${body.cartId}" already exists in the tracker.` };

    // Find next empty row in col A
    const scanStart = 4;
    const maxScan   = Math.max(tracker.getMaxRows() - scanStart + 1, 1);
    const colAVals  = tracker.getRange(scanStart, 1, maxScan, 1).getValues();
    let lastDataIdx = -1;
    for (let i = 0; i < colAVals.length; i++) {
      if (String(colAVals[i][0]).trim() !== '') lastDataIdx = i;
    }
    const newRow = lastDataIdx === -1 ? scanStart : (scanStart + lastDataIdx + 1);

    // Apply formatting + dropdowns
    _applyRowValidations(tracker, newRow);

    const now     = new Date();
    const nowStr  = Utilities.formatDate(now, Session.getScriptTimeZone(), 'MM/dd/yyyy');
    const state   = String(body.state || 'NJ').toUpperCase();
    const trigger = String(body.trigger || '').trim();
    const rsaReq  = body.rsaRequired || calcRsaRequired_(state, trigger) || 'No';

    tracker.getRange(newRow,  1).setValue(body.cartId);
    tracker.getRange(newRow,  2).setValue(body.store);
    tracker.getRange(newRow,  3).setValue(state);
    tracker.getRange(newRow,  4).setValue(trigger);
    tracker.getRange(newRow,  5).setValue(nowStr);                // Service Date
    tracker.getRange(newRow,  6).setValue(rsaReq);
    tracker.getRange(newRow,  7).setValue(body.rsaVendor  || 'TBD');
    tracker.getRange(newRow, 10).setValue(body.rsaStatus  || 'Pending RSA');
    tracker.getRange(newRow, 12).setValue('Yes');                 // W&M Required
    tracker.getRange(newRow, 13).setValue(body.wmStatus   || 'Pending W&M');
    tracker.getRange(newRow, 15).setValue(body.overallStatus || 'Pending RSA');
    tracker.getRange(newRow, 17).setValue(body.dri        || 'HW Ops');
    tracker.getRange(newRow, 18).setValue(now);                   // Last Updated
    tracker.getRange(newRow, 19).setValue(body.notes      || 'Added via RSA Agent bot');

    // Days-in-Status formula (col P = 16)
    tracker.getRange(newRow, 16).setFormula(
      `=IF(AND(J${newRow}="",M${newRow}=""),"",` +
      `IF(R${newRow}<>"",TODAY()-INT(R${newRow}),` +
      `IF(E${newRow}<>"",TODAY()-INT(E${newRow}),"")))`
    );

    _ensureConditionalFormatting(tracker, newRow);
    SpreadsheetApp.flush();

    return {
      ok:         true,
      cartId:     body.cartId,
      store:      body.store,
      row:        newRow,
      trackerUrl: _webAppUrl(body.cartId),
    };
  } catch (err) {
    return { error: '_botAddCart: ' + err.message };
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// ACTION: queryCart
// Body fields: cartId (exact or partial), storeHint (optional)
// Returns: all fields for the matched cart
// ─────────────────────────────────────────────────────────────────────────────
function _botQueryCart(body) {
  try {
    const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
    const tracker = ss.getSheetByName('RSA Tracker');
    if (!tracker) return { error: 'RSA Tracker sheet not found.' };

    const { row, rowData } = _findCartRow(tracker, body.cartId, body.storeHint);
    if (!row) return { found: false, message: `No cart found matching "${body.cartId}".` };

    return {
      found:        true,
      cartId:       String(rowData[0]).trim(),
      store:        String(rowData[1]).trim(),
      state:        String(rowData[2]).trim(),
      trigger:      String(rowData[3]).trim(),
      rsaRequired:  String(rowData[5]).trim(),
      rsaVendor:    String(rowData[6]).trim(),
      rsaStatus:    String(rowData[9]).trim(),
      wmStatus:     String(rowData[12]).trim(),
      wmDate:       _fmtDate(rowData[13]),
      overallStatus:String(rowData[14]).trim(),
      days:         rowData[15] !== '' ? rowData[15] : null,
      dri:          String(rowData[16]).trim(),
      lastUpdated:  _fmtDate(rowData[17]),
      notes:        String(rowData[18]).trim(),
      reportLink:     String(rowData[19] || '').trim(),   // T — Tech/Report link
      rsaTestResults: String(rowData[20] || '').trim(),   // U — RSA Test Results
      techResults:     String(rowData[21] || '').trim(),  // V — Tech Results (calibration)
      techResultsLink: String(rowData[22] || '').trim(),  // W — Tech Results file link
      trackerUrl:   _webAppUrl(String(rowData[0]).trim()),
    };
  } catch (err) {
    return { error: '_botQueryCart: ' + err.message };
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// ACTION: getSummary
// Returns aggregate counts across the whole tracker
// ─────────────────────────────────────────────────────────────────────────────
function _botGetSummary(body) {
  try {
    const rows = _getAllTrackerRows();
    if (!rows) return { error: 'RSA Tracker sheet not found.' };

    const counts = {
      total:           rows.length,
      pendingRSA:      rows.filter(r => r[9]  === 'Pending RSA').length,
      scheduledRSA:    rows.filter(r => r[9]  === 'Scheduled RSA').length,
      completedRSA:    rows.filter(r => r[9]  === 'Completed RSA').length,
      failedRSA:       rows.filter(r => r[9]  === 'Failed RSA').length,
      pendingWM:       rows.filter(r => r[12] === 'Pending W&M').length,
      passedWM:        rows.filter(r => r[12] === 'Passed W&M' || r[12] === 'Completed W&M').length,
      failedWM:        rows.filter(r => r[12] === 'Failed W&M').length,
      overallPassed:   rows.filter(r => r[14] === 'Passed').length,
      needsAction:     rows.filter(r => r[14] === 'Redispatch Needed' || r[14] === 'Needs Verification' || r[9] === 'Failed RSA' || r[12] === 'Failed W&M').length,
      stale10plus:     rows.filter(r => typeof r[15] === 'number' && r[15] >= 10 && r[14] !== 'Passed').length,
    };

    // Break down by state
    const byState = {};
    rows.forEach(r => {
      const st = String(r[2]).trim();
      if (!st) return;
      byState[st] = (byState[st] || 0) + 1;
    });

    return { ok: true, counts, byState, trackerUrl: _webAppUrl() };
  } catch (err) {
    return { error: '_botGetSummary: ' + err.message };
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// ACTION: listCarts
// Body fields (all optional, combined as AND filters):
//   state         — e.g. 'NJ'
//   rsaStatus     — e.g. 'Pending RSA'
//   wmStatus      — e.g. 'Failed W&M'
//   overallStatus — e.g. 'Redispatch Needed'
//   dri           — e.g. 'Sai VS'
//   staleOnly     — if true, only return carts with days >= 10 and not Passed
//   storeFragment — partial store name match
//   limit         — max rows to return (default 20)
// ─────────────────────────────────────────────────────────────────────────────
function _botListCarts(body) {
  try {
    let rows = _getAllTrackerRows();
    if (!rows) return { error: 'RSA Tracker sheet not found.' };

    const sf = body.storeFragment ? String(body.storeFragment).toLowerCase() : '';

    rows = rows.filter(r => {
      if (body.state         && String(r[2])  !== body.state)         return false;
      if (body.rsaStatus     && String(r[9])  !== body.rsaStatus)     return false;
      if (body.wmStatus      && String(r[12]) !== body.wmStatus)      return false;
      if (body.overallStatus && String(r[14]) !== body.overallStatus) return false;
      if (body.dri           && !String(r[16]).toLowerCase().includes(body.dri.toLowerCase())) return false;
      if (sf && !String(r[1]).toLowerCase().includes(sf))             return false;
      if (body.staleOnly) {
        const d = r[15];
        if (typeof d !== 'number' || d < 10 || String(r[14]) === 'Passed') return false;
      }
      return true;
    });

    const limit = Math.min(parseInt(body.limit, 10) || 20, 50);
    const sliced = rows.slice(0, limit);

    return {
      ok:    true,
      total: rows.length,
      shown: sliced.length,
      carts: sliced.map(r => ({
        cartId:        String(r[0]).trim(),
        store:         String(r[1]).trim(),
        state:         String(r[2]).trim(),
        rsaStatus:     String(r[9]).trim(),
        wmStatus:      String(r[12]).trim(),
        overallStatus: String(r[14]).trim(),
        days:          r[15] !== '' ? r[15] : null,
        dri:           String(r[16]).trim(),
        lastUpdated:   _fmtDate(r[17]),
        reportLink:     String(r[19] || '').trim(),   // T — Tech/Report link
        rsaTestResults: String(r[20] || '').trim(),   // U — RSA Test Results
        techResults:     String(r[21] || '').trim(),  // V — Tech Results (calibration)
        techResultsLink: String(r[22] || '').trim(),  // W — Tech Results file link
      })),
      trackerUrl: _webAppUrl(),
    };
  } catch (err) {
    return { error: '_botListCarts: ' + err.message };
  }
}


// ═══════════════════════════════════════════════════════════════════════════════
// HELPER UTILITIES
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * Reads all cart rows (rows 4+) from RSA Tracker where col A is not empty.
 * Returns an array of raw row arrays (0-indexed values), or null on error.
 */
function _getAllTrackerRows() {
  try {
    const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
    const tracker = ss.getSheetByName('RSA Tracker');
    if (!tracker) return null;

    const scanStart = 4;
    const maxScan   = Math.max(tracker.getMaxRows() - scanStart + 1, 1);
    const colA      = tracker.getRange(scanStart, 1, maxScan, 1).getValues();
    let lastIdx     = -1;
    for (let i = 0; i < colA.length; i++) {
      if (String(colA[i][0]).trim() !== '') lastIdx = i;
    }
    if (lastIdx < 0) return [];

    // Read through col U (21): adds T=Tech/Report Link (idx 19) and U=RSA Test Results (idx 20).
    return tracker.getRange(scanStart, 1, lastIdx + 1, 21).getValues()
      .filter(r => String(r[0]).trim() !== '');
  } catch (err) {
    Logger.log('_getAllTrackerRows error: ' + err.message);
    return null;
  }
}

/**
 * Finds a cart row by cartId (partial match allowed) + optional store hint.
 * Returns { row (1-indexed), rowData (array), rowIdx (0-indexed) } or { row: null }.
 */
function _findCartRow(tracker, cartId, storeHint) {
  if (!cartId) return { row: null };

  const scanStart = 4;
  const maxScan   = Math.max(tracker.getMaxRows() - scanStart + 1, 1);
  const colA      = tracker.getRange(scanStart, 1, maxScan, 1).getValues();
  let lastIdx     = -1;
  for (let i = 0; i < colA.length; i++) {
    if (String(colA[i][0]).trim() !== '') lastIdx = i;
  }
  if (lastIdx < 0) return { row: null };

  const data     = tracker.getRange(scanStart, 1, lastIdx + 1, 21).getValues();   // incl. T(20)/U(21)
  const queryId  = String(cartId).toLowerCase().trim();
  const queryStr = storeHint ? String(storeHint).toLowerCase().trim() : '';

  let bestIdx = -1;

  for (let i = 0; i < data.length; i++) {
    const rowId    = String(data[i][0]).toLowerCase().trim();
    const rowStore = String(data[i][1]).toLowerCase().trim();
    if (!rowId) continue;

    const idMatch = rowId === queryId || rowId.includes(queryId) || queryId.includes(rowId);
    if (!idMatch) continue;

    if (!queryStr) { bestIdx = i; break; }  // no store hint — take first match

    const storeMatch = rowStore.includes(queryStr) || queryStr.includes(rowStore.substring(0, 4));
    if (storeMatch) { bestIdx = i; break; }
    if (bestIdx === -1) bestIdx = i;          // fallback: first ID match even without store
  }

  if (bestIdx === -1) return { row: null };
  return {
    row:     scanStart + bestIdx,
    rowData: data[bestIdx],
    rowIdx:  bestIdx,
  };
}

/** Parse date strings to Date objects; pass-through if already a Date */
function _parseDate(val) {
  if (!val) return '';
  if (val instanceof Date) return val;
  const s = String(val).trim();
  const d = new Date(s);
  return isNaN(d.getTime()) ? s : d;
}

/** Format Date objects to MM/DD/YYYY strings; pass-through strings */
function _fmtDate(val) {
  if (!val) return '';
  if (val instanceof Date && !isNaN(val)) {
    return Utilities.formatDate(val, Session.getScriptTimeZone(), 'MM/dd/yyyy');
  }
  return String(val);
}

/** Human-readable column name for update log */
function _colName(col) {
  const map = {
    7:'RSA Vendor', 8:'RSA Scheduled Date', 10:'RSA Status',
    13:'W&M Status', 14:'W&M Date', 15:'Overall Status',
    17:'DRI', 18:'Last Updated', 19:'Notes',
  };
  return map[col] || `col${col}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// ACTION: scheduleTruno
// Adds a new row to the Truno RSA tracker spreadsheet to schedule an inspection
// visit, then sends an email to Nicole (and optionally posts to her Slack DM).
//
// Body fields:
//   cartId         (required) — cart ID to schedule
//   store          (required) — store / location name
//   state          (optional) — state code
//   visitDate      (optional) — requested visit date (ISO or MM/DD/YYYY); if omitted the row
//                   is created 'Pending' with a BLANK date for Truno to schedule (no placeholder)
//   notes          (optional) — additional instructions for Nicole
//   submittedBy    (optional) — name of the person who submitted (for traceability)
//   sendEmail      (optional) — boolean, default true
//   emailCc        (optional) — comma-separated CC addresses
// ─────────────────────────────────────────────────────────────────────────────
function _botScheduleTruno(body) {
  try {
    if (!body.cartId || !body.store) return { error: 'cartId and store are required.' };
    // Block: a Truno row needs the store address (the Caper join key + what Nicole
    // needs to dispatch). The bot resolves it before calling; refuse if it's missing.
    if (!String(body.address || '').trim()) {
      return { error: 'address is required — resolve the store address before scheduling with Truno.',
               needAddress: true, store: body.store };
    }

    const ss     = SpreadsheetApp.openById(TRUNO_SPREADSHEET_ID);
    const sheets = ss.getSheets();
    // Target the first sheet (or a sheet named "RSA Visits" if it exists)
    const trunoSheet = sheets.find(s => s.getName().toLowerCase().includes('rsa')) || sheets[0];
    if (!trunoSheet) return { error: 'Could not find a suitable sheet in the Truno tracker.' };

    // Visit date: ONLY what the caller provides — never fabricate one. With no date the
    // row is created 'Pending' for Nicole/Truno to schedule (matches the Slack agent).
    const visitDate = body.visitDate ? _parseDate(body.visitDate) : '';
    const hasDate   = !!visitDate;

    const now       = new Date();
    const nowStr    = Utilities.formatDate(now, Session.getScriptTimeZone(), 'MM/dd/yyyy HH:mm');
    const visitStr  = hasDate ? _fmtDate(visitDate) : '';
    const submitter = String(body.submittedBy || 'RSA Agent').trim();
    const noteText  = String(body.notes || '').trim();

    // Find next empty row in the Truno sheet (scan col A)
    const maxScan  = Math.max(trunoSheet.getMaxRows(), 2);
    const colAVals = trunoSheet.getRange(2, 1, maxScan - 1, 1).getValues();
    let lastIdx    = -1;
    for (let i = 0; i < colAVals.length; i++) {
      if (String(colAVals[i][0]).trim() !== '') lastIdx = i;
    }
    const newRow = lastIdx === -1 ? 2 : (2 + lastIdx + 1);

    // Write row using the live Truno column layout. We now fill the Address (col E)
    // so the row is complete and joins cleanly to Caper; the remaining Truno-owned
    // fields (Truno Store Name, Call #, Arrival, Test Results, Forms, W&M) are left
    // blank for Truno/ops to fill. The sheet has no submitter or timestamp column, so
    // who/when traceability is folded into Notes.
    const trace   = `[RSA Agent · ${submitter} · ${nowStr}]`;
    const noteOut = noteText ? `${noteText}  ${trace}` : trace;

    const width   = 13;                                // full Truno row width
    const rowVals = new Array(width).fill('');
    rowVals[TRUNO_COL.store     - 1] = body.store;     // A — IC Store Name
    rowVals[TRUNO_COL.cart      - 1] = body.cartId;    // C — Cart
    rowVals[TRUNO_COL.address   - 1] = body.address;   // E — Address (Caper join key)
    rowVals[TRUNO_COL.visitDate - 1] = visitDate;      // F — Scheduled Date (blank until Truno sets it)
    rowVals[TRUNO_COL.notes     - 1] = noteOut;        // I — Notes (+ trace)
    rowVals[TRUNO_COL.status    - 1] = hasDate ? 'Scheduled' : 'Pending';  // K — Status

    trunoSheet.getRange(newRow, 1, 1, width).setValues([rowVals]);
    SpreadsheetApp.flush();

    // Also update the RSA Tracker: vendor = TRUNO; write a scheduled date + 'Scheduled RSA'
    // ONLY when a real date was provided — otherwise leave the date blank and keep the cart
    // 'Pending Truno Scheduled' (no fabricated date).
    try {
      const rsaSS      = SpreadsheetApp.openById(SPREADSHEET_ID);
      const tracker    = rsaSS.getSheetByName('RSA Tracker');
      if (tracker) {
        const { row } = _findCartRow(tracker, body.cartId, body.store);
        if (row) {
          tracker.getRange(row, 7).setValue('TRUNO');                       // G: RSA Vendor
          if (hasDate) {
            tracker.getRange(row, 8).setValue(visitDate);                   // H: RSA Scheduled Date
            tracker.getRange(row, 10).setValue('Scheduled RSA');            // J: RSA Status
          } else {
            tracker.getRange(row, 10).setValue('Pending Truno Scheduled');  // J: RSA Status
          }
          tracker.getRange(row, 18).setValue(now);                          // R: Last Updated
        }
      }
    } catch(e) {
      Logger.log('_botScheduleTruno: RSA Tracker update skipped — ' + e.message);
    }

    // ── Send email to Nicole ───────────────────────────────────────────────────
    if (body.sendEmail !== false) {
      const emailSubject = `RSA Inspection Request — ${body.cartId} at ${body.store}`
                         + (hasDate ? ` · ${visitStr}` : '');
      const htmlBody = _buildTrunoEmail({
        cartId:      body.cartId,
        store:       body.store,
        state:       body.state || '',
        address:     body.address || '',
        visitDate:   visitStr || 'TBD — Truno to schedule',
        notes:       noteText,
        submittedBy: submitter,
        submittedAt: nowStr,
      });

      const mailOpts = {
        htmlBody: htmlBody,
        name:     'RSA Operations Agent',
        noReply:  true,
      };
      if (body.emailCc) mailOpts.cc = body.emailCc;

      try {
        GmailApp.sendEmail(NICOLE_EMAIL, emailSubject, '', mailOpts);
        Logger.log('_botScheduleTruno: Email sent to ' + NICOLE_EMAIL);
      } catch(e) {
        Logger.log('_botScheduleTruno: Email failed — ' + e.message);
      }
    }

    const trunoUrl = `https://docs.google.com/spreadsheets/d/${TRUNO_SPREADSHEET_ID}/edit`;
    return {
      ok:        true,
      cartId:    body.cartId,
      store:     body.store,
      address:   body.address,
      visitDate: visitStr || null,
      status:    hasDate ? 'Scheduled' : 'Pending',
      trunoRow:  newRow,
      emailSent: body.sendEmail !== false,
      emailTo:   NICOLE_EMAIL,
      trunoUrl:  trunoUrl,
    };
  } catch(err) {
    return { error: '_botScheduleTruno: ' + err.message };
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// ACTION: scheduleWinterScales
// NJ-only alternative to TRUNO. There is NO Winter Scales tracker sheet, so this
// emails the Winter Scales contacts to request scheduling and (best-effort) marks
// the RSA Tracker row as vendor = Winter Scales / status Pending RSA. Ops follows
// up with Winter Scales directly — the cart is not auto-scheduled.
//
// Body fields:
//   cartId       (required) — cart ID/serial, or a list of cart numbers (e.g. "1, 7")
//   store        (required) — store / location name
//   state        (optional) — state code (expected NJ)
//   trigger      (optional) — service trigger
//   notes        (optional) — extra context for the email
//   submittedBy  (optional) — submitter name (traceability)
//   emailTo      (optional) — override recipients (array or comma-separated string)
//   sendEmail    (optional) — boolean, default true
// ─────────────────────────────────────────────────────────────────────────────
function _botScheduleWinterScales(body) {
  try {
    if (!body.cartId || !body.store) return { error: 'cartId and store are required.' };
    // Block: the email IS the scheduling request, so it must carry the store address.
    if (!String(body.address || '').trim()) {
      return { error: 'address is required — resolve the store address before emailing Winter Scales.',
               needAddress: true, store: body.store };
    }

    const now       = new Date();
    const nowStr    = Utilities.formatDate(now, Session.getScriptTimeZone(), 'MM/dd/yyyy HH:mm');
    const submitter = String(body.submittedBy || 'RSA Agent').trim();
    const noteText  = String(body.notes || '').trim();

    // Resolve recipients: explicit override (array or comma string), else the default list.
    let recipients = body.emailTo || WINTERSCALES_EMAILS;
    if (typeof recipients === 'string') {
      recipients = recipients.split(',').map(function (s) { return s.trim(); });
    }
    recipients = (recipients || []).filter(String);
    if (!recipients.length) return { error: 'No Winter Scales recipients configured.' };

    // Best-effort RSA Tracker update (the bot already writes these via the Sheets
    // API; this is a safety net and a no-op when cartId is a list of numbers).
    try {
      const rsaSS   = SpreadsheetApp.openById(SPREADSHEET_ID);
      const tracker = rsaSS.getSheetByName('RSA Tracker');
      if (tracker) {
        const found = _findCartRow(tracker, body.cartId, body.store);
        if (found && found.row) {
          tracker.getRange(found.row, 7).setValue('Winter Scales');  // G — RSA Vendor
          tracker.getRange(found.row, 10).setValue('Pending RSA');    // J — RSA Status
          tracker.getRange(found.row, 18).setValue(now);              // R — Last Updated
          const trace = `[RSA Agent · Winter Scales · ${submitter} · ${nowStr}]`;
          const cur   = String(tracker.getRange(found.row, 19).getValue() || '');  // S — Notes
          tracker.getRange(found.row, 19).setValue(cur ? `${cur}\n${trace}` : trace);
        }
      }
    } catch (e) {
      Logger.log('_botScheduleWinterScales: RSA Tracker update skipped — ' + e.message);
    }

    // ── Email the Winter Scales contacts ───────────────────────────────────────
    let emailSent = false;
    if (body.sendEmail !== false) {
      const storeLabel = body.state ? `${body.store} (${body.state})` : body.store;
      const subject    = `RSA Inspection Request (Winter Scales) — ${body.cartId} at ${storeLabel}`;
      const htmlBody   = _buildWinterScalesEmail({
        cartId:      body.cartId,
        store:       body.store,
        state:       body.state || '',
        address:     body.address || '',
        trigger:     body.trigger || '',
        notes:       noteText,
        submittedBy: submitter,
        submittedAt: nowStr,
      });
      try {
        GmailApp.sendEmail(recipients.join(','), subject, '', {
          htmlBody: htmlBody, name: 'RSA Operations Agent', noReply: true,
        });
        emailSent = true;
        Logger.log('_botScheduleWinterScales: Email sent to ' + recipients.join(', '));
      } catch (e) {
        Logger.log('_botScheduleWinterScales: Email failed — ' + e.message);
        return { error: 'Winter Scales email failed: ' + e.message, emailTo: recipients };
      }
    }

    return {
      ok:         true,
      vendor:     'Winter Scales',
      cartId:     body.cartId,
      store:      body.store,
      address:    body.address,
      emailSent:  emailSent,
      emailTo:    recipients,
      trackerUrl: _webAppUrl(body.cartId),
    };
  } catch (err) {
    return { error: '_botScheduleWinterScales: ' + err.message };
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// Webapp entry point (google.script.run) for the NJ "Winter Scales" path.
// The Index.html add-cart form calls this AFTER a cart is added with vendor =
// Winter Scales, to email the Winter Scales contacts. No shared secret is needed
// here — unlike doPost, this runs inside the authenticated, same-project web app.
// Payload: { cartId, store, state, trigger, notes, submittedBy, emailTo? }
// ─────────────────────────────────────────────────────────────────────────────
function webappScheduleWinterScales(payload) {
  return _botScheduleWinterScales(payload || {});
}


// ─────────────────────────────────────────────────────────────────────────────
// ACTION: fullOnboardCart
// The complete sequential workflow triggered when an ops manager submits a
// new cart via Slack:
//   Step 1 — Add (or update) cart in the RSA Tracker
//   Step 2 — Schedule inspection in the Truno RSA tracker + tag Nicole
//   Step 3 — Send traceability email to Truno (Nicole)
//
// Returns a structured result with per-step outcomes so the bot can report
// exactly what happened at each step even if a later step fails.
// ─────────────────────────────────────────────────────────────────────────────
function _botFullOnboardCart(body) {
  const result = {
    steps: {
      step1_rsaTracker: null,
      step2_trunoSchedule: null,
    },
    cartId:    body.cartId,
    store:     body.store,
    trackerUrl: _webAppUrl(body.cartId),
  };

  // ── STEP 1: Add (or update) the cart in the RSA Tracker ──────────────────
  try {
    const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
    const tracker = ss.getSheetByName('RSA Tracker');
    const { row }  = tracker ? _findCartRow(tracker, body.cartId, body.store) : { row: null };

    if (row) {
      // Cart exists — update relevant fields only
      const updateResult = _botUpdateCart({
        cartId:        body.cartId,
        storeHint:     body.store,
        rsaStatus:     body.rsaStatus     || null,
        wmStatus:      body.wmStatus      || null,
        overallStatus: body.overallStatus || null,
        rsaVendor:     body.rsaVendor     || 'TRUNO',
        dri:           body.dri           || null,
        notes:         body.notes         || null,
        appendNotes:   body.appendNotes   !== false,
      });
      result.steps.step1_rsaTracker = { action: 'updated', ...updateResult };
    } else {
      // New cart — add it
      const addResult = _botAddCart({
        cartId:        body.cartId,
        store:         body.store,
        state:         body.state     || 'NJ',
        trigger:       body.trigger   || '',
        rsaStatus:     body.rsaStatus || 'Pending RSA',
        rsaVendor:     body.rsaVendor || 'TRUNO',   // respect the caller's vendor (don't force TRUNO for NJ)
        wmStatus:      body.wmStatus  || 'Pending W&M',
        overallStatus: body.overallStatus || 'Pending RSA',
        dri:           body.dri       || 'HW Ops',
        notes:         body.notes     || 'Added via RSA Agent bot',
      });
      result.steps.step1_rsaTracker = { action: 'added', ...addResult };
    }
  } catch(e) {
    result.steps.step1_rsaTracker = { error: 'Step 1 failed: ' + e.message };
  }

  // ── STEP 2: Schedule in Truno tracker + email Nicole ─────────────────────
  try {
    const trunoResult = _botScheduleTruno({
      cartId:      body.cartId,
      store:       body.store,
      state:       body.state     || '',
      visitDate:   body.visitDate || null,   // null = leave blank (Pending; Truno schedules)
      notes:       body.notes     || '',
      submittedBy: body.submittedBy || 'RSA Agent',
      sendEmail:   true,
      emailCc:     body.emailCc   || '',
    });
    result.steps.step2_trunoSchedule = trunoResult;
    result.trunoUrl  = trunoResult.trunoUrl;
    result.visitDate = trunoResult.visitDate;
    result.emailTo   = trunoResult.emailTo;
  } catch(e) {
    result.steps.step2_trunoSchedule = { error: 'Step 2 failed: ' + e.message };
  }

  // ── Compliance lookup — surface RSA/W&M requirements for this store ───────
  try {
    result.compliance = _lookupCompliance(body.store, body.state || '', body.trigger || '');
  } catch(e) {
    result.compliance = { error: 'compliance lookup failed: ' + e.message };
  }

  result.ok = !result.steps.step1_rsaTracker?.error && !result.steps.step2_trunoSchedule?.error;
  return result;
}


// ═══════════════════════════════════════════════════════════════════════════════
// COMPLIANCE LOOKUP — W&M / RSA requirements from the W&M Compliance Guide
// Matches a cart's store to the guide (by state, refined by store nickname/city)
// and returns the pre-launch + post-launch directives so the bot can tell the ops
// manager exactly what to do next. Requirements are state-driven in the guide, so
// the state match is authoritative; the store match adds county-level precision.
// ═══════════════════════════════════════════════════════════════════════════════
function _lookupCompliance(store, state, trigger) {
  const out = { matched: false, state: String(state || '').toUpperCase().trim(), store: store || '' };
  try {
    const ss = SpreadsheetApp.openById(COMPLIANCE_SPREADSHEET_ID);
    const sh = ss.getSheetByName(COMPLIANCE_SHEET_NAME) || ss.getSheets()[0];
    if (!sh) { out.error = 'compliance sheet not found'; return out; }
    const last = sh.getLastRow();
    if (last < 3) return out;
    const data = sh.getRange(3, 1, last - 2, CG.agency).getValues();

    const st = out.state;
    const storeNorm = String(store || '').toLowerCase().replace(/[^a-z0-9]/g, '');

    // Rows in the same state (requirements are state-uniform in the guide)
    const inState = [];
    for (let i = 0; i < data.length; i++) {
      if (String(data[i][CG.state - 1] || '').toUpperCase().trim() === st) inState.push(data[i]);
    }
    if (!inState.length) { out.note = 'state not in compliance guide'; return out; }

    // Refine to a specific store by nickname/city token (county-level precision)
    let row = null;
    for (let i = 0; i < inState.length; i++) {
      const nick = String(inState[i][CG.nickname - 1] || '').toLowerCase().replace(/[^a-z0-9]/g, '');
      const city = String(inState[i][CG.city - 1] || '').toLowerCase().replace(/[^a-z0-9]/g, '');
      if ((nick.length >= 4 && storeNorm.indexOf(nick) !== -1) ||
          (city.length >= 4 && storeNorm.indexOf(city) !== -1)) { row = inState[i]; break; }
    }
    const storeMatched = !!row;
    if (!row) row = inState[0];   // state-level default

    const gate   = _complianceGate(trigger);
    const req    = gate === 'AT-LAUNCH' ? row[CG.atReq - 1]    : row[CG.postReq - 1];
    const dir    = gate === 'AT-LAUNCH' ? row[CG.atDir - 1]    : row[CG.postDir - 1];
    const notify = gate === 'AT-LAUNCH' ? row[CG.atNotify - 1] : row[CG.postNotify - 1];

    out.matched      = true;
    out.storeMatched = storeMatched;
    out.retailer     = String(row[CG.retailer - 1] || '');
    out.nickname     = String(row[CG.nickname - 1] || '');
    out.county       = String(row[CG.county - 1] || '');
    out.gate         = gate;
    out.path         = String(req || '').trim();
    out.directive    = String(dir || '').trim();
    out.notify       = String(notify || '').trim();
    out.preDirective = String(row[CG.preDir - 1] || '').trim();
    out.postDirective= String(row[CG.postDir - 1] || '').trim();
    out.agency       = String(row[CG.agency - 1] || '').trim();
    out.cadence      = String(row[CG.cadence - 1] || '').trim();

    const flags = [];
    if (/confirm locally/i.test(out.path)) flags.push('Path is "Confirm Locally" — verify RSA vs county path before scheduling.');
    if (st === 'NY') flags.push('NY county exception: Dutchess / Orange / Westchester may allow conditional deployment with advance W&M approval.');
    if (!storeMatched) flags.push('Matched by state only (no exact store match) — confirm county-level rules.');
    out.flags = flags;

    out.slackText = _complianceSlackText(out);
    return out;
  } catch(e) {
    out.error = '_lookupCompliance: ' + e.message;
    return out;
  }
}

// Which gate applies, from the RSA Tracker service trigger.
function _complianceGate(trigger) {
  const t = String(trigger || '').toLowerCase();
  if (/launch|first deploy|new cart|full cart swap|cart replacement/.test(t)) return 'AT-LAUNCH';
  return 'POST-LAUNCH';   // top unit swap, recal, scale drift, jetson, hw ops, W&M failed, etc.
}

// Ready-to-post Slack block summarising the store's requirements + what to do next.
function _complianceSlackText(c) {
  if (!c.matched) {
    return `:scroll: *W&M / RSA requirements — ${c.store} (${c.state || '?'})*\n` +
           `:warning: No compliance row found. Confirm requirements manually in the W&M Compliance Guide.`;
  }
  const L = [];
  L.push(`:scroll: *W&M / RSA requirements — ${c.store} (${c.state})*`);
  L.push(`*Path:* ${c.path || 'N/A'}    ·    *Gate:* ${c.gate}`);
  L.push(`:point_right: *Do next:* ${c.directive || 'N/A'}`);
  if (c.notify) L.push(`*Notify:* ${c.notify}`);
  if (c.agency) L.push(`*Authority:* ${c.agency}${c.cadence ? '   ·   Cadence: ' + c.cadence : ''}`);
  L.push(`• _Pre-launch:_ ${c.preDirective || 'N/A'}`);
  L.push(`• _Post-launch:_ ${c.postDirective || 'N/A'}`);
  (c.flags || []).forEach(function(f){ L.push(`:warning: ${f}`); });
  return L.join('\n');
}

// doPost action: standalone compliance lookup by store/state.
function _botComplianceCheck(body) {
  if (!body.store && !body.state) return { error: 'store or state is required.' };
  return _lookupCompliance(body.store || '', body.state || '', body.trigger || '');
}


// ─────────────────────────────────────────────────────────────────────────────
// EMAIL BUILDER — Truno scheduling notification
// ─────────────────────────────────────────────────────────────────────────────
// Render a cart label as the SHORT cart number(s). Truno/Winter Scales think in
// cart numbers, not our internal serials: "P_WF_561_M3_0116" → "116"; a list like
// "1, 7" (or already-short input) passes through unchanged.
function _shortCart(v) {
  var s = String(v == null ? '' : v).trim();
  if (!s) return s;
  return s.split(/\s*[,/]\s*/).filter(String).map(function (p) {
    var m = p.match(/_M3_0*(\d+)([A-Za-z]?)$/i);   // serial → trailing cart number
    if (m) return m[1] + (m[2] || '').toUpperCase();
    var d = p.match(/^0*(\d+)([A-Za-z]?)$/);        // already a bare number (drop pad zeros)
    if (d) return d[1] + (d[2] || '').toUpperCase();
    return p;                                       // unknown shape → leave as-is
  }).join(', ');
}


function _buildTrunoEmail({ cartId, store, state, address, visitDate, notes, submittedBy, submittedAt, followUp }) {
  const storeLabel = state ? `${store} (${state})` : store;
  const cartLabel  = _shortCart(cartId);
  const badgeMsg = followUp ? '🔔 Follow-up — still awaiting a scheduling date'
                            : '📅 New RSA Inspection Scheduled';
  const introMsg = followUp
    ? 'We\'re following up on the RSA inspection request below — we haven\'t seen a scheduling update in a few days. '
      + 'Could you confirm the expected visit date, or let us know if anything is holding it up?'
    : 'A new RSA inspection has been requested for the following cart. Please confirm availability and coordinate scheduling.';
  const addrRow = address
    ? `<tr style="background:#fff">
        <td colspan="2" style="padding:12px 18px;border-top:1px solid #e5e7eb">
          <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.7px;margin-bottom:4px">Store Address</div>
          <div style="font-size:14px;color:#1a2e4a">${address}</div>
        </td></tr>`
    : '';
  const notesBlock = notes
    ? `<p style="font-size:14px;color:#4b5563;margin:0 0 16px;line-height:1.6"><strong>Additional notes:</strong> ${notes}</p>`
    : '';

  return `<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:32px 16px">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.10);max-width:580px">

  <!-- Header -->
  <tr><td style="background:#1a2e4a;padding:22px 32px">
    <div style="color:#ffffff;font-size:17px;font-weight:bold">RSA Inspection Request</div>
    <div style="color:#7faabf;font-size:12px;margin-top:3px">Instacart Hardware Ops · RSA Agent</div>
  </td></tr>

  <!-- Status badge -->
  <tr><td style="background:#2e5fa318;border-left:4px solid #2e5fa3;padding:11px 32px">
    <span style="color:#2e5fa3;font-size:13px;font-weight:bold">${badgeMsg}</span>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:28px 32px 20px">
    <p style="font-size:15px;color:#1a2e4a;margin:0 0 8px;line-height:1.5">
      Hi ${TRUNO_CONTACT_NAME},
    </p>
    <p style="font-size:14px;color:#4b5563;margin:0 0 20px;line-height:1.6">
      ${introMsg}
    </p>

    <!-- Cart detail card -->
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;margin-bottom:20px">
      <tr style="background:#f9fafb">
        <td style="padding:12px 18px;border-right:1px solid #e5e7eb;width:50%;vertical-align:top">
          <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.7px;margin-bottom:4px">Cart #</div>
          <div style="font-size:15px;font-weight:bold;color:#1a2e4a;font-family:monospace">${cartLabel}</div>
        </td>
        <td style="padding:12px 18px;width:50%;vertical-align:top">
          <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.7px;margin-bottom:4px">Store</div>
          <div style="font-size:15px;font-weight:bold;color:#1a2e4a">${storeLabel}</div>
        </td>
      </tr>
      ${addrRow}
      <tr style="background:#fff">
        <td style="padding:12px 18px;border-top:1px solid #e5e7eb;border-right:1px solid #e5e7eb">
          <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.7px;margin-bottom:4px">Requested Visit Date</div>
          <div style="font-size:16px;font-weight:bold;color:#276221">${visitDate}</div>
        </td>
        <td style="padding:12px 18px;border-top:1px solid #e5e7eb">
          <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.7px;margin-bottom:4px">Submitted By</div>
          <div style="font-size:14px;color:#1a2e4a">${submittedBy}</div>
        </td>
      </tr>
    </table>

    ${notesBlock}

    <p style="font-size:13px;color:#6b7280;margin:0 0 24px;line-height:1.5">
      ⚠️ <strong>Before inspection:</strong> Please put the cart <strong>ON Coffee Break</strong> the day before.<br>
      🚫 <strong>On inspection day:</strong> Turn <strong>OFF Coffee Break</strong> before the technician starts.
    </p>

    <!-- CTA button -->
    <table cellpadding="0" cellspacing="0">
      <tr><td style="background:#1a2e4a;border-radius:7px">
        <a href="${`https://docs.google.com/spreadsheets/d/${TRUNO_SPREADSHEET_ID}/edit`}"
           style="display:inline-block;padding:12px 26px;color:#ffffff;font-size:14px;font-weight:bold;text-decoration:none">
          View Truno RSA Tracker →
        </a>
      </td></tr>
    </table>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f9fafb;border-top:1px solid #e5e7eb;padding:14px 32px">
    <p style="font-size:11px;color:#9ca3af;margin:0;line-height:1.5">
      Submitted by RSA Operations Agent · ${submittedAt}<br>
      This is an automated message from the Instacart Hardware Ops RSA system.
      Reply to this email or reach us on Slack to confirm or change the visit date.
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>`;
}


function _buildWinterScalesEmail({ cartId, store, state, address, trigger, notes, submittedBy, submittedAt }) {
  const storeLabel = state ? `${store} (${state})` : store;
  const cartLabel  = _shortCart(cartId);
  const addrRow = address
    ? `<tr style="background:#fff">
        <td colspan="2" style="padding:12px 18px;border-top:1px solid #e5e7eb">
          <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.7px;margin-bottom:4px">Store Address</div>
          <div style="font-size:14px;color:#1a2e4a">${address}</div>
        </td></tr>`
    : '';
  const trigBlock = trigger
    ? `<tr style="background:#fff"><td colspan="2" style="padding:12px 18px;border-top:1px solid #e5e7eb">
         <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.7px;margin-bottom:4px">Service Trigger</div>
         <div style="font-size:14px;color:#1a2e4a">${trigger}</div></td></tr>`
    : '';
  const notesBlock = notes
    ? `<p style="font-size:14px;color:#4b5563;margin:0 0 16px;line-height:1.6"><strong>Additional notes:</strong> ${notes}</p>`
    : '';

  return `<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:32px 16px">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.10);max-width:580px">

  <!-- Header -->
  <tr><td style="background:#1a2e4a;padding:22px 32px">
    <div style="color:#ffffff;font-size:17px;font-weight:bold">RSA Inspection Request</div>
    <div style="color:#7faabf;font-size:12px;margin-top:3px">Instacart Hardware Ops · RSA Agent</div>
  </td></tr>

  <!-- Status badge -->
  <tr><td style="background:#2e5fa318;border-left:4px solid #2e5fa3;padding:11px 32px">
    <span style="color:#2e5fa3;font-size:13px;font-weight:bold">⚖️ RSA Scheduling Request — Winter Scales</span>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:28px 32px 20px">
    <p style="font-size:15px;color:#1a2e4a;margin:0 0 8px;line-height:1.5">Hi ${WINTERSCALES_CONTACT_NAME},</p>
    <p style="font-size:14px;color:#4b5563;margin:0 0 20px;line-height:1.6">
      We'd like to schedule an RSA (weights &amp; measures) inspection for the cart(s) below. Please reply with your earliest availability.
    </p>

    <!-- Cart detail card -->
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;margin-bottom:20px">
      <tr style="background:#f9fafb">
        <td style="padding:12px 18px;border-right:1px solid #e5e7eb;width:50%;vertical-align:top">
          <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.7px;margin-bottom:4px">Cart(s) #</div>
          <div style="font-size:15px;font-weight:bold;color:#1a2e4a;font-family:monospace">${cartLabel}</div>
        </td>
        <td style="padding:12px 18px;width:50%;vertical-align:top">
          <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.7px;margin-bottom:4px">Store</div>
          <div style="font-size:15px;font-weight:bold;color:#1a2e4a">${storeLabel}</div>
        </td>
      </tr>
      ${addrRow}
      ${trigBlock}
    </table>

    ${notesBlock}

    <p style="font-size:13px;color:#6b7280;margin:0 0 24px;line-height:1.5">
      ⚠️ <strong>Before inspection:</strong> Please put the cart <strong>ON Coffee Break</strong> the day before.<br>
      🚫 <strong>On inspection day:</strong> Turn <strong>OFF Coffee Break</strong> before the technician starts.
    </p>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f9fafb;border-top:1px solid #e5e7eb;padding:14px 32px">
    <p style="font-size:11px;color:#9ca3af;margin:0;line-height:1.5">
      Submitted by ${submittedBy} via RSA Operations Agent · ${submittedAt}<br>
      This is an automated message from the Instacart Hardware Ops RSA system. Reply to this email to coordinate scheduling.
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>`;
}


/** Wrap a plain object as a JSON ContentService response */
function _jsonResp(obj, headers) {
  const out = ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
  return out;
}
