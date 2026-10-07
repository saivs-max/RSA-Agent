// ═══════════════════════════════════════════════════════════════════════════════
// getTrackerData() — REPLACE your existing one in Code.gs with this.
//
// Find the current `function getTrackerData() { … }` in the "TRACKER VIEW" section
// of Code.gs, select it through its closing brace, and paste this over it.
// Do NOT keep this in a separate file alongside the old one — two functions with the
// same name conflict and the dashboard returns null ("Loading tracker…" forever).
//
// FIXES vs your current version:
//   1. Tech Results bug — was raw[21]/raw[22] (row 21's whole array → the
//      "P_WF_SHOPRITE_136_M3_005,WF…" junk on every row). Now raw[i][21]/raw[i][22].
//   2. Reads A..W (up to 23 cols) so V/W are actually read (capped at the sheet's
//      real last column so it never throws out-of-bounds).
//   3. COMPLETED carts now SHOW if finished within COMPLETED_WINDOW_DAYS (30) —
//      older completed carts stay hidden. Active carts keep your 20-day stale rule.
// ═══════════════════════════════════════════════════════════════════════════════
function getTrackerData() {
  try {
    const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
    const tracker = ss.getSheetByName('RSA Tracker');
    if (!tracker) return { error: 'RSA Tracker sheet not found.' };

    const COMPLETED_WINDOW_DAYS = 30;   // show Completed-RSA carts finished within this many days
    const tz      = Session.getScriptTimeZone();
    const now     = new Date();
    const maxScan = Math.max(tracker.getMaxRows() - 3, 1);
    const ncol    = Math.min(23, tracker.getLastColumn());           // A..W if present
    const raw     = tracker.getRange(4, 1, maxScan, ncol).getValues();
    const disp    = tracker.getRange(4, 1, maxScan, ncol).getDisplayValues();

    // Cart IDs already in Archive are never shown in the Tracker view.
    const archivedIds = new Set();
    try {
      const archive = ss.getSheetByName('Archive');
      if (archive) {
        const archMax = Math.max(archive.getMaxRows() - 3, 1);
        archive.getRange(4, 1, archMax, 1).getValues().forEach(r => {
          const id = String(r[0] || '').trim();
          if (id) archivedIds.add(id);
        });
      }
    } catch (_) { /* ignore — fail open */ }

    function fmtDate(v) {
      if (!v || v === '') return '';
      if (v instanceof Date && !isNaN(v)) return Utilities.formatDate(v, tz, 'MM/dd/yyyy');
      return String(v);
    }
    function asDate(v) {
      if (v instanceof Date && !isNaN(v)) return v;
      if (!v && v !== 0) return null;
      const d = new Date(v);
      return isNaN(d.getTime()) ? null : d;
    }

    const rows = [];
    for (let i = 0; i < raw.length; i++) {
      const cartId = String(raw[i][0] || '').trim();
      if (!cartId) continue;
      if (archivedIds.has(cartId)) continue;

      const rsaStatus_     = String(raw[i][9]  || '').trim();
      const wmStatus_      = String(raw[i][12] || '').trim();
      let   overallStatus_ = String(raw[i][14] || '').trim();
      // W&M is out of scope: a Completed RSA cart is fully done, so surface Overall as
      // 'Passed' (regardless of the old W&M column). All views read overallStatus, so this
      // one derivation flows everywhere.
      if (rsaStatus_ === 'Completed RSA') overallStatus_ = 'Passed';
      const isPassed = overallStatus_ === 'Passed';
      // DRI rule: Completed RSA → CSM (handoff to customer team).
      //           Anything else (Pending, Scheduled, Failed) → HW Ops.
      const dri_ = (rsaStatus_ === 'Completed RSA') ? 'CSM' : 'HW Ops';

      if (isPassed) {
        // Done: hide only once it's been finished longer than the window (Archive owns it).
        // Keep it visible if there's no completion date yet, so nothing is dropped early.
        const doneDate = asDate(raw[i][10]) || asDate(raw[i][17]);   // K: RSA Completion, else R: Last Updated
        if (doneDate && (now - doneDate) / 86400000 > COMPLETED_WINDOW_DAYS) continue;
      } else {
        // Active cart: keep the existing 20-day stale rule (unless it needs action).
        const days = parseInt(disp[i][15], 10);
        const needsAction = rsaStatus_     === 'Failed RSA'        ||
                            overallStatus_ === 'Redispatch Needed' ||
                            overallStatus_ === 'Needs Verification';
        if (!isNaN(days) && days >= 20 && !needsAction) continue;
      }

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
        rsaStatus:         rsaStatus_,
        rsaCompletionDate: fmtDate(raw[i][10]),
        wmRequired:        String(raw[i][11] || '').trim(),
        wmStatus:          wmStatus_,
        wmDate:            fmtDate(raw[i][13]),
        overallStatus:     overallStatus_,
        days:              disp[i][15] !== '' ? disp[i][15] : '',
        dri:               dri_,
        lastUpdated:       fmtDate(raw[i][17]),
        notes:             String(raw[i][18] || '').trim(),
        reportLink:        String(raw[i][19] || '').trim(),   // T — report link
        rsaTestResults:    String(raw[i][20] || '').trim(),   // U — RSA Test Results
        techResults:       String(raw[i][21] || '').trim(),   // V — Tech Results        (FIXED: raw[i][21])
        techResultsLink:   String(raw[i][22] || '').trim(),   // W — Tech Results Link    (FIXED: raw[i][22])
      });
    }

    rows.sort((a, b) => b.sheetRow - a.sheetRow);
    const refreshedAt = Utilities.formatDate(now, tz, 'MMM d, yyyy h:mm a');
    return { rows, refreshedAt, window: COMPLETED_WINDOW_DAYS };
  } catch (err) {
    return { error: err.message };
  }
}
