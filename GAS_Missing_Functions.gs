// ═══════════════════════════════════════════════════════════════════════════════
// GAS_Missing_Functions.gs
//
// ADD THIS FILE to your Google Apps Script project alongside Code.gs.
// (In Apps Script editor: click + → Script → paste this whole file)
//
// These functions are called by Index.html but were absent from Code.gs,
// causing all modal saves, deletes, bulk edits, archive loads, and
// "Archive 20+ Day Carts" to silently fail.
//
// Functions in this file:
//   updateCartRow()       — modal Save Changes
//   setTechResultManual() — modal Tech Results field
//   deleteCartRow()       — modal Delete button
//   bulkUpdateCarts()     — bulk-status bar in Tracker tab
//   getArchiveData()      — Archive tab initial load
//   archiveOldCarts()     — "Archive 20+ Day Carts" button
// ═══════════════════════════════════════════════════════════════════════════════


/**
 * updateCartRow()
 * Called from the web app edit modal when the user clicks "Save Changes".
 *
 * Writes RSA Vendor, RSA Status, W&M Status, Overall Status, DRI, Notes to the
 * matched row, and stamps Last Updated (col R = 18) to now.
 *
 * Rule: if RSA Status is set to "Completed RSA", Overall Status is forced to
 * "Passed" regardless of what the dropdown held.
 *
 * @param {Object} params { cartId, sourceSheet, rsaVendor, rsaStatus, wmStatus,
 *                          overallStatus, dri, notes }
 * @returns {Object} { ok: true, cartId, row } or { error: '...' }
 */
function updateCartRow(params) {
  try {
    const ss        = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheetName = (params.sourceSheet === 'Archive') ? 'Archive' : 'RSA Tracker';
    const sheet     = ss.getSheetByName(sheetName);
    if (!sheet) return { error: sheetName + ' sheet not found.' };

    const cartId = String(params.cartId || '').trim();
    if (!cartId) return { error: 'Cart ID is required.' };

    // ── Find the row ──────────────────────────────────────────────────────────
    const maxScan = Math.max(sheet.getMaxRows() - 3, 1);
    const colA    = sheet.getRange(4, 1, maxScan, 1).getValues();
    let   rowNum  = -1;
    for (let i = 0; i < colA.length; i++) {
      if (String(colA[i][0] || '').trim() === cartId) { rowNum = 4 + i; break; }
    }
    if (rowNum === -1) return { error: 'Cart "' + cartId + '" not found in ' + sheetName + '.' };

    // ── Compute final Overall Status value ────────────────────────────────────
    // Completed RSA → always Passed (mirrors the frontend enforcement)
    const finalOverall = (params.rsaStatus === 'Completed RSA')
      ? 'Passed'
      : (params.overallStatus !== undefined ? params.overallStatus : null);

    // ── Write fields ──────────────────────────────────────────────────────────
    // Column references (1-indexed, matching Code.gs T object):
    //   G=7  RSA Vendor      J=10 RSA Status
    //   M=13 W&M Status      O=15 Overall/RTS    Q=17 DRI
    //   R=18 Last Updated    S=19 Notes
    const now = new Date();
    const writes = [
      { col:  7, val: params.rsaVendor    },   // G: RSA Vendor
      { col: 10, val: params.rsaStatus    },   // J: RSA Status
      { col: 13, val: params.wmStatus     },   // M: W&M Status
      { col: 15, val: finalOverall        },   // O: Overall Status
      { col: 17, val: params.dri          },   // Q: DRI
      { col: 18, val: now                 },   // R: Last Updated (always stamp)
      { col: 19, val: params.notes        },   // S: Notes
    ];

    writes.forEach(w => {
      if (w.val === undefined || w.val === null) return;   // skip unset fields
      sheet.getRange(rowNum, w.col).setValue(w.val);
    });

    SpreadsheetApp.flush();
    Logger.log('updateCartRow: updated ' + cartId + ' in ' + sheetName + ' (row ' + rowNum + ')');
    return { ok: true, cartId: cartId, row: rowNum };

  } catch (e) {
    return { error: 'updateCartRow: ' + e.message };
  }
}


/**
 * setTechResultManual()
 * Writes (or clears) the manually-entered tech calibration result.
 * Stored in col V (1-indexed col 22 = "Tech Results").
 *
 * @param {Object} params { cartId, sourceSheet, techResults }
 * @returns {Object} { ok: true } or { error: '...' }
 */
function setTechResultManual(params) {
  try {
    const ss        = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheetName = (params.sourceSheet === 'Archive') ? 'Archive' : 'RSA Tracker';
    const sheet     = ss.getSheetByName(sheetName);
    if (!sheet) return { error: sheetName + ' sheet not found.' };

    const cartId = String(params.cartId || '').trim();
    if (!cartId) return { error: 'Cart ID is required.' };

    const maxScan = Math.max(sheet.getMaxRows() - 3, 1);
    const colA    = sheet.getRange(4, 1, maxScan, 1).getValues();
    let   rowNum  = -1;
    for (let i = 0; i < colA.length; i++) {
      if (String(colA[i][0] || '').trim() === cartId) { rowNum = 4 + i; break; }
    }
    if (rowNum === -1) return { error: 'Cart "' + cartId + '" not found.' };

    // Col V (22) = Tech Results (the field the modal edits)
    sheet.getRange(rowNum, 22).setValue(params.techResults || '');
    SpreadsheetApp.flush();
    Logger.log('setTechResultManual: wrote tech result for ' + cartId);
    return { ok: true };

  } catch (e) {
    return { error: 'setTechResultManual: ' + e.message };
  }
}


/**
 * deleteCartRow()
 * Permanently removes a single cart row from the RSA Tracker or Archive sheet.
 * Called from the modal Delete button (after the user confirms the browser dialog).
 *
 * @param {Object} params { cartId, sourceSheet }
 * @returns {Object} { ok: true, cartId } or { error: '...' }
 */
function deleteCartRow(params) {
  try {
    const ss        = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheetName = (params.sourceSheet === 'Archive') ? 'Archive' : 'RSA Tracker';
    const sheet     = ss.getSheetByName(sheetName);
    if (!sheet) return { error: sheetName + ' sheet not found.' };

    const cartId = String(params.cartId || '').trim();
    if (!cartId) return { error: 'Cart ID is required.' };

    const maxScan = Math.max(sheet.getMaxRows() - 3, 1);
    const colA    = sheet.getRange(4, 1, maxScan, 1).getValues();
    let   rowNum  = -1;
    for (let i = 0; i < colA.length; i++) {
      if (String(colA[i][0] || '').trim() === cartId) { rowNum = 4 + i; break; }
    }
    if (rowNum === -1) return { error: 'Cart "' + cartId + '" not found in ' + sheetName + '.' };

    sheet.deleteRow(rowNum);
    SpreadsheetApp.flush();
    Logger.log('deleteCartRow: deleted ' + cartId + ' from ' + sheetName + ' (was row ' + rowNum + ')');
    return { ok: true, cartId: cartId };

  } catch (e) {
    return { error: 'deleteCartRow: ' + e.message };
  }
}


/**
 * bulkUpdateCarts()
 * Applies one or more field updates to multiple carts simultaneously.
 * Called from the bulk-selection bar in the Tracker tab.
 *
 * Supported fields: rsaStatus (col J=10), overallStatus (col O=15), dri (col Q=17)
 * Always stamps Last Updated (col R=18) on every changed row.
 *
 * @param {Object} params {
 *   cartIds: string[],
 *   fields:  { rsaStatus?: string, overallStatus?: string, dri?: string }
 * }
 * @returns {Object} { ok: true, updated: N, missing: string[] } or { error: '...' }
 */
function bulkUpdateCarts(params) {
  try {
    const ss    = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName('RSA Tracker');
    if (!sheet) return { error: 'RSA Tracker sheet not found.' };

    const cartIds = params.cartIds || [];
    const fields  = params.fields  || {};
    if (!cartIds.length) return { ok: true, updated: 0, missing: [] };

    // Build cartId → rowNum map
    const maxScan = Math.max(sheet.getMaxRows() - 3, 1);
    const colA    = sheet.getRange(4, 1, maxScan, 1).getValues();
    const rowMap  = {};
    for (let i = 0; i < colA.length; i++) {
      const id = String(colA[i][0] || '').trim();
      if (id) rowMap[id] = 4 + i;
    }

    // Column map for writable fields
    const FIELD_COL = { rsaStatus: 10, overallStatus: 15, dri: 17 };

    const now     = new Date();
    const missing = [];
    let   updated = 0;

    cartIds.forEach(function(cartId) {
      const rowNum = rowMap[cartId];
      if (!rowNum) { missing.push(cartId); return; }

      Object.keys(fields).forEach(function(field) {
        const col = FIELD_COL[field];
        if (col) sheet.getRange(rowNum, col).setValue(fields[field]);
      });
      sheet.getRange(rowNum, 18).setValue(now);   // R: Last Updated
      updated++;
    });

    SpreadsheetApp.flush();
    Logger.log('bulkUpdateCarts: updated=' + updated + ', missing=' + missing.length);
    return { ok: true, updated: updated, missing: missing };

  } catch (e) {
    return { error: 'bulkUpdateCarts: ' + e.message };
  }
}


/**
 * getArchiveData()
 * Returns all rows from the Archive sheet in the same shape as getTrackerData(),
 * so the Archive tab can use the same rendering code as the Tracker tab.
 *
 * @returns {Object} { rows: [...], refreshedAt: string } or { error: '...' }
 */
function getArchiveData() {
  try {
    const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
    const archive = ss.getSheetByName('Archive');
    if (!archive) {
      // Return empty rather than error — Archive tab should just show "nothing here yet"
      return { rows: [], refreshedAt: Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'MMM d, yyyy h:mm a') };
    }

    const tz      = Session.getScriptTimeZone();
    const now     = new Date();
    const maxScan = Math.max(archive.getMaxRows() - 3, 1);
    const ncol    = Math.min(23, archive.getLastColumn());

    const raw  = archive.getRange(4, 1, maxScan, ncol).getValues();
    const disp = archive.getRange(4, 1, maxScan, ncol).getDisplayValues();

    function fmtDate(v) {
      if (!v || v === '') return '';
      if (v instanceof Date && !isNaN(v)) return Utilities.formatDate(v, tz, 'MM/dd/yyyy');
      return String(v);
    }

    const rows = [];
    for (var i = 0; i < raw.length; i++) {
      const cartId = String(raw[i][0] || '').trim();
      if (!cartId) continue;
      rows.push({
        sheetRow:          i + 4,
        cartId:            cartId,
        store:             String(raw[i][1]  || '').trim(),
        state:             String(raw[i][2]  || '').trim(),
        trigger:           String(raw[i][3]  || '').trim(),
        serviceDate:       fmtDate(raw[i][4]),
        rsaRequired:       String(raw[i][5]  || '').trim(),
        rsaVendor:         String(raw[i][6]  || '').trim(),
        rsaSchedDate:      fmtDate(raw[i][7]),
        etsDate:           fmtDate(raw[i][8]),
        rsaStatus:         String(raw[i][9]  || '').trim(),
        rsaCompletionDate: fmtDate(raw[i][10]),
        wmRequired:        String(raw[i][11] || '').trim(),
        wmStatus:          String(raw[i][12] || '').trim(),
        wmDate:            fmtDate(raw[i][13]),
        overallStatus:     String(raw[i][14] || '').trim(),
        days:              disp[i][15] !== '' ? disp[i][15] : '',
        dri:               String(raw[i][16] || '').trim(),
        lastUpdated:       fmtDate(raw[i][17]),
        notes:             String(raw[i][18] || '').trim(),
        reportLink:        ncol > 19 ? String(raw[i][19] || '').trim() : '',
        rsaTestResults:    ncol > 20 ? String(raw[i][20] || '').trim() : '',
        techResults:       ncol > 21 ? String(raw[i][21] || '').trim() : '',
        techResultsLink:   ncol > 22 ? String(raw[i][22] || '').trim() : '',
      });
    }

    rows.sort(function(a, b) { return b.sheetRow - a.sheetRow; });
    return {
      rows:        rows,
      refreshedAt: Utilities.formatDate(now, tz, 'MMM d, yyyy h:mm a'),
    };

  } catch (e) {
    return { error: 'getArchiveData: ' + e.message };
  }
}


/**
 * archiveOldCarts()
 * Frontend alias for the "Archive 20+ Day Carts" button in the Archive tab.
 * Delegates to the existing archiveCompletedCarts() in Code.gs and reshapes
 * the return value into { count, cartIds } as expected by the button handler.
 *
 * archiveCompletedCarts() already handles all the logic:
 *   • W&M passed + RSA completed + Overall = Passed + DRI = CSM + 15+ days old
 *   → copies to Archive sheet, deletes from RSA Tracker
 *
 * @returns {Object} { count: N, cartIds: string[] } or { error: '...' }
 */
function archiveOldCarts() {
  try {
    // Collect cart IDs before archiving so we can report them back
    const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
    const tracker = ss.getSheetByName('RSA Tracker');
    if (!tracker) return { error: 'RSA Tracker sheet not found.' };

    const ARCHIVE_THRESHOLD_DAYS = 15;
    const now      = new Date();
    const todayMid = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const PASS_KW  = ['pass', 'recalib', 'calib', 'complet', 'ok', 'clear'];

    const maxScan  = Math.max(tracker.getMaxRows() - 3, 1);
    const colAVals = tracker.getRange(4, 1, maxScan, 1).getValues();
    let   lastIdx  = -1;
    for (var i = 0; i < colAVals.length; i++) {
      if (String(colAVals[i][0]).trim() !== '') lastIdx = i;
    }
    if (lastIdx < 0) return { count: 0, cartIds: [] };

    const data = tracker.getRange(4, 1, lastIdx + 1, 19).getValues();
    const eligibleIds = [];

    for (var j = 0; j < data.length; j++) {
      const cartId      = String(data[j][0] || '').trim();
      if (!cartId) continue;
      const wmStatus    = String(data[j][12] || '').toLowerCase();
      const rsaStatus   = String(data[j][9]  || '').toLowerCase();
      const rts         = String(data[j][14] || '').toLowerCase();
      const dri         = String(data[j][16] || '').toLowerCase();
      const lastUpdated = data[j][17];

      const wmPassed  = PASS_KW.some(function(k) { return wmStatus.includes(k); });
      const rsaPassed = rsaStatus.includes('complet') || rsaStatus.includes('pass');
      const rtsPassed = rts === 'passed';
      const isCsm     = dri === 'csm';
      if (!wmPassed || !rsaPassed || !rtsPassed || !isCsm) continue;

      if (!lastUpdated) continue;
      const lastMid = new Date(
        new Date(lastUpdated).getFullYear(),
        new Date(lastUpdated).getMonth(),
        new Date(lastUpdated).getDate()
      );
      const daysSince = Math.floor((todayMid - lastMid) / (1000 * 60 * 60 * 24));
      if (daysSince < ARCHIVE_THRESHOLD_DAYS) continue;

      eligibleIds.push(cartId);
    }

    if (eligibleIds.length === 0) return { count: 0, cartIds: [] };

    // Delegate to existing archiveCompletedCarts() which does the actual move
    const moved = archiveCompletedCarts();
    return { count: moved, cartIds: eligibleIds };

  } catch (e) {
    return { error: 'archiveOldCarts: ' + e.message };
  }
}
