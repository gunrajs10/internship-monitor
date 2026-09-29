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
      return opportunityReply_({ok: false, error: "delivery_uncertain",
        message: "An earlier attempt is processing or uncertain; reconcile it before retrying."}, eventId);
    }

    row = last + 1;
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
