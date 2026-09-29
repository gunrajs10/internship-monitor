/* Add beside the existing receiver after renaming its doPost to handleMonitorPost_. */
function doPost(e) {
  var payload;
  try { payload = JSON.parse(e.postData.contents); }
  catch (_) { return handleMonitorPost_(e); }
  var eventId = payload && typeof payload.event_id === "string" ? payload.event_id : "";
  var ours = /^(opportunity-|windows-opportunity-)/.test(eventId);
  if (payload && payload.type === "connection_check") {
    return opportunityReply_({ok: true, protocol: "opportunity-receipts-v1"}, ours ? eventId : "");
  }
  if (!ours || (payload.type !== "new_roles" && payload.type !== "failures")) {
    return handleMonitorPost_(e);
  }
  if (!Array.isArray(payload.items) || payload.items.length < 1 || payload.items.length > 200) {
    return opportunityReply_({ok: false, error: "invalid_batch", message: "Expected 1 to 200 items."}, eventId);
  }

  var lock = null, acquired = false, sheet = null, row = 0, hash = "";
  try {
    lock = LockService.getScriptLock();
    acquired = lock.tryLock(30000);
    if (!acquired) {
      return opportunityReply_({ok: false, error: "locked", message: "No delivery attempted; receiver is busy."}, eventId);
    }
    hash = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,
      opportunityCanonical_(payload), Utilities.Charset.UTF_8).map(function (b) {
        return ("0" + ((b + 256) % 256).toString(16)).slice(-2);
      }).join("");
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    sheet = ss.getSheetByName("_Opportunity Delivery Receipts");
    if (!sheet) {
      sheet = ss.insertSheet("_Opportunity Delivery Receipts");
      sheet.getRange(1, 1, 1, 4).setValues([["Event ID", "Payload hash", "Status", "Updated"]]);
    }
    sheet.hideSheet();
    var last = sheet.getLastRow();
    var found = last > 1 ? sheet.getRange(2, 1, last - 1, 1)
      .createTextFinder(eventId).matchEntireCell(true).matchCase(true)
      .useRegularExpression(false).findNext() : null;
    if (found) {
      var prior = sheet.getRange(found.getRow(), 1, 1, 4).getValues()[0];
      if (prior[1] !== hash) {
        return opportunityReply_({ok: false, error: "payload_hash_mismatch",
          message: "This event ID already belongs to different content; no delivery attempted."}, eventId);
      }
      if (prior[2] === "sent") {
        return opportunityReply_({ok: true, duplicate: true}, eventId);
      }
      if (prior[2] !== "confirmed_unsent") {
        return opportunityReply_({ok: false, error: "delivery_uncertain",
          message: "An earlier attempt is processing or uncertain; reconcile it before retrying."}, eventId);
      }
      // This status can only be set by a human/operator after reconciliation,
      // never by a webhook request. Reuse its audit row and immutable hash.
      row = found.getRow();
    }

    row = row || last + 1;
    sheet.getRange(row, 1, 1, 4).setValues([[eventId, hash, "processing", new Date()]]);
    SpreadsheetApp.flush(); // Persist the guard before any legacy side effect.
    handleMonitorPost_(e);
    sheet.getRange(row, 3, 1, 2).setValues([["sent", new Date()]]);
    SpreadsheetApp.flush();
    return opportunityReply_({ok: true}, eventId);
  } catch (_) {
    if (sheet && row) {
      try {
        sheet.getRange(row, 3, 1, 2).setValues([["uncertain", new Date()]]);
        SpreadsheetApp.flush();
      } catch (ignored) { /* A persisted processing guard also prevents replay. */ }
    }
    return opportunityReply_({ok: false, error: row ? "delivery_uncertain" : "receipt_store_unavailable",
      message: row ? "Delivery could not be confirmed; reconcile this event before retrying." :
        "Receipt storage is unavailable; no delivery attempted."}, eventId);
  } finally {
    if (acquired) {
      try { lock.releaseLock(); } catch (ignored) { /* Apps Script also releases on exit. */ }
    }
  }
}

function opportunityWriteRows_(ss, payload) {
  // Append only absent URLs. This also repairs a partial tracker write safely.
  var groups = {};
  payload.items.forEach(function (item) {
    var tab = item.track === "early-stage" ? TAB_EARLY : TAB_PROGRAMS;
    (groups[tab] || (groups[tab] = [])).push(item);
  });
  Object.keys(groups).forEach(function (tab) {
    var sheet = getSheet(ss, tab, HEADERS);
    var last = sheet.getLastRow();
    var existing = {};
    if (last > 1) sheet.getRange(2, 7, last - 1, 1).getValues().forEach(function (r) { existing[String(r[0])] = true; });
    var rows = [];
    groups[tab].forEach(function (r) {
      if (existing[r.url]) return;
      existing[r.url] = true;
      rows.push([fmtDate(r.first_seen), r.company, r.title, r.location,
        r.posted_on, r.eligibility, r.url, "New", r.priority || "", ""].map(function(v) { return v == null ? "" : String(v); }));
    });
    if (!rows.length) return;
    if (last + rows.length > sheet.getMaxRows()) sheet.insertRowsAfter(sheet.getMaxRows(), last + rows.length - sheet.getMaxRows());
    sheet.getRange(last + 1, 1, rows.length, 10).setValues(rows);
  });
  SpreadsheetApp.flush();
}

function opportunityCanonical_(value) {
  // The durable Python queue sorts object keys; whitespace/order are not content.
  if (Array.isArray(value)) return "[" + value.map(opportunityCanonical_).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map(function (key) {
      return JSON.stringify(key) + ":" + opportunityCanonical_(value[key]);
    }).join(",") + "}";
  }
  return JSON.stringify(value);
}

function opportunityReply_(value, eventId) {
  if (eventId) value.event_id = eventId;
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
