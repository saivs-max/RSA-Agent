/**
 * addCartManual — web-app "Add Cart to Tracker" backend (google.script.run).
 * ---------------------------------------------------------------------------
 * DROP-IN: paste this into the Apps Script project alongside GAS_BotEndpoints.gs.
 * It reuses functions defined there: _findCartRow, _botAddCart, _botUpdateCart,
 * _botScheduleTruno, and the globals SPREADSHEET_ID / TRACKER_URL.
 *
 * It mirrors the Slack-bot onboarding for a single manually-entered cart:
 *   1. Add (or update) the cart in the RSA Tracker.
 *   2. TRUNO carts → write a Truno row (with the store ADDRESS in col E) and email
 *      Nicole, via _botScheduleTruno. Winter Scales is NOT scheduled here — the form
 *      makes a separate webappScheduleWinterScales call for that.
 *
 * ADDRESS RULE (per the agent spec): a Truno row must carry the store address, so we
 * BLOCK (return an error, write nothing to Truno) when a TRUNO cart has no address.
 * GAS can't reach the Caper deployment data, so the address comes from the form's
 * "Store Address" field (body.address).
 *
 * Return shape expected by Index.html's add-cart handler:
 *   { error? , row , trunoRow:{row}|null , trackerUrl , address }
 */
function addCartManual(body) {
  try {
    body = body || {};
    if (!body.cartId || !body.store) return { error: 'Cart ID and Store are required.' };

    const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
    const tracker = ss.getSheetByName('RSA Tracker');
    if (!tracker) return { error: 'RSA Tracker sheet not found.' };

    const state   = String(body.state || '').toUpperCase();
    const vendor  = String(body.rsaVendor || 'TBD').trim();
    const address = String(body.address || '').trim();
    const isWS    = /winter/i.test(vendor);
    const isTruno = /truno/i.test(vendor);

    // NJ rule: never auto-assign TRUNO — caller must pick TRUNO or Winter Scales.
    if (state === 'NJ' && (!vendor || /^tbd$/i.test(vendor))) {
      return { error: 'NJ store — choose TRUNO or Winter Scales (NJ carts are not auto-assigned to TRUNO).' };
    }

    // BLOCK: a TRUNO row needs the store address (written to col E + emailed to Nicole).
    if (isTruno && !address) {
      return { error: 'Store address is required to schedule with TRUNO — it is written to the Truno '
                    + 'tracker and included in the email to Nicole. Add the address and try again.',
               needAddress: true };
    }

    // ── Step 1: add (or update) the cart in the RSA Tracker ────────────────────
    const defaultStatus = isWS ? 'Pending RSA' : (isTruno ? 'Pending Truno Scheduled' : 'Pending RSA');
    const found = _findCartRow(tracker, body.cartId, body.store);
    let rsaRow;
    if (found && found.row) {
      const upd = _botUpdateCart({
        cartId:        body.cartId,
        storeHint:     body.store,
        rsaVendor:     vendor,
        rsaStatus:     body.rsaStatus || defaultStatus,
        wmStatus:      body.wmStatus  || 'Pending W&M',
        overallStatus: body.overallStatus || null,   // null → _botUpdateCart leaves it unchanged
        dri:           body.dri || null,              // (only overwrite when the form provided one)
        notes:         body.notes || null,
        appendNotes:   true,
      });
      if (upd.error) return upd;
      rsaRow = found.row;
    } else {
      const wsNote = 'Winter Scales (NJ) — emailed to schedule; ops to follow up. Added via RSA Webapp';
      const add = _botAddCart({
        cartId:        body.cartId,
        store:         body.store,
        state:         state,
        trigger:       body.trigger || '',
        rsaVendor:     vendor,
        rsaStatus:     body.rsaStatus || defaultStatus,
        wmStatus:      body.wmStatus  || 'Pending W&M',
        overallStatus: body.overallStatus || 'Pending RSA',
        dri:           body.dri || 'HW Ops',           // persist the form's DRI (was dropped)
        notes:         body.notes || (isWS ? wsNote : 'Added via RSA Webapp'),
      });
      if (add.error) return add;
      rsaRow = add.row;
    }

    const result = {
      ok:         true,
      cartId:     body.cartId,
      store:      body.store,
      address:    address,
      row:        rsaRow,
      trunoRow:   null,
      trackerUrl: _webAppUrl(body.cartId),
    };

    // ── Step 2: TRUNO → schedule a Truno row (with address) + email Nicole ──────
    if (isTruno) {
      const tr = _botScheduleTruno({
        cartId:      body.cartId,
        store:       body.store,
        state:       state,
        address:     address,            // → Truno col E + the email to Nicole
        visitDate:   body.visitDate || null,
        notes:       body.notes || '',
        submittedBy: body.submittedBy || 'Webapp',
        sendEmail:   true,
      });
      if (tr.error) {
        // The cart is in the RSA Tracker, but Truno scheduling was blocked/failed.
        result.trunoError = tr.error;
        if (tr.needAddress) result.needAddress = true;
      } else {
        result.trunoRow  = { row: tr.trunoRow };   // Index.html reads res.trunoRow.row
        result.visitDate = tr.visitDate;
        result.emailTo   = tr.emailTo;
        result.trunoUrl  = tr.trunoUrl;
      }
    }

    return result;
  } catch (err) {
    return { error: 'addCartManual: ' + err.message };
  }
}
