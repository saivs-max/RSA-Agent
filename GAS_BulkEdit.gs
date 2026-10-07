// ═══════════════════════════════════════════════════════════════════════════════
// GAS_BulkEdit.gs — bulk status editing for the webapp.
//
// Paste into the Apps Script project alongside Code.gs. Called by the dashboard's
// bulk-edit toolbar via google.script.run.bulkUpdateCarts(...).
//
// Updates one or more of RSA Status (J), Overall Status (O), DRI (Q) for a set of cart
// IDs in ONE batched write, and stamps Last Updated (R). Uses SPREADSHEET_ID from Code.gs.
// ═══════════════════════════════════════════════════════════════════════════════
function bulkUpdateCarts(payload) {
  try {
    const ids    = (payload && payload.cartIds) || [];
    const fields = (payload && payload.fields)  || {};
    if (!ids.length)  return { error: 'No carts selected.' };
    if (!fields || !Object.keys(fields).length) return { error: 'No field/value to set.' };

    const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
    const tracker = ss.getSheetByName('RSA Tracker');
    if (!tracker) return { error: 'RSA Tracker sheet not found.' };

    const nRows = Math.max(tracker.getLastRow() - 3, 0);   // data starts row 4
    if (!nRows) return { error: 'No data rows in the tracker.' };

    const rng  = tracker.getRange(4, 1, nRows, 19);        // A..S
    const vals = rng.getValues();

    const want = {};
    ids.forEach(id => { want[String(id).trim().toLowerCase()] = true; });

    const tz    = Session.getScriptTimeZone();
    const today = Utilities.formatDate(new Date(), tz, 'MM/dd/yyyy');
    const found = {};
    let updated = 0;

    for (let i = 0; i < vals.length; i++) {
      const cid = String(vals[i][0] || '').trim();
      if (!cid || !want[cid.toLowerCase()]) continue;
      found[cid.toLowerCase()] = true;
      if (fields.rsaStatus) {
        vals[i][9] = fields.rsaStatus;                               // J — RSA Status
        // Rule: Completed RSA → Overall Passed (W&M out of scope), unless an explicit
        // Overall was also chosen in the same bulk action.
        if (fields.rsaStatus === 'Completed RSA' && !fields.overallStatus) vals[i][14] = 'Passed';
      }
      if (fields.overallStatus) vals[i][14] = fields.overallStatus;  // O — Overall Status
      if (fields.dri)           vals[i][16] = fields.dri;            // Q — DRI
      vals[i][17] = today;                                           // R — Last Updated
      updated++;
    }
    if (updated) rng.setValues(vals);

    const missing = ids.filter(id => !found[String(id).trim().toLowerCase()]);
    return { updated: updated, missing: missing };
  } catch (err) {
    return { error: err.message };
  }
}
