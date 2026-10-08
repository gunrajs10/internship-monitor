/* Uses the existing receiver's EMAIL setting. Archives, never deletes. */
var CLEANUP_SUBJECT_TAG = "[Internships]";
var CLEANUP_AFTER_DAYS = 1;
var CLEANUP_LABEL = "Internship Monitor/Archived";
var CLEANUP_MAX_THREADS = 500;

function cleanupQuery_() {
  var me = String(EMAIL).trim().toLowerCase();
  if (!me || Session.getEffectiveUser().getEmail().toLowerCase() !== me) {
    throw new Error("Run cleanup as the configured monitor recipient");
  }
  var cutoff = Date.now() - 24 * 60 * 60 * 1000;
  return {me: me, cutoff: cutoff,
    q: "in:inbox from:" + me + " subject:Internships before:" + Math.floor(cutoff / 1000)};
}

function safeMonitorThread_(thread, settings) {
  if (thread.getLastMessageDate().getTime() >= settings.cutoff) return false;
  var messages = thread.getMessages();
  return messages.length > 0 && messages.every(function (message) {
    var from = String(message.getFrom() || "").trim().toLowerCase();
    var address = from.match(/<([^<>]+)>\s*$/);
    from = address ? address[1] : from;
    return from === settings.me && String(message.getSubject() || "").indexOf(CLEANUP_SUBJECT_TAG) === 0 &&
      message.getDate().getTime() < settings.cutoff;
  });
}

function cleanupDryRun() {
  var settings = cleanupQuery_();
  var threads = GmailApp.search(settings.q, 0, CLEANUP_MAX_THREADS);
  var count = threads.filter(function (thread) { return safeMonitorThread_(thread, settings); }).length;
  Logger.log("Eligible monitor threads older than 24 hours: " + count);
  return count;
}

function cleanupOldMonitorEmails() {
  var settings = cleanupQuery_();
  var threads = GmailApp.search(settings.q, 0, CLEANUP_MAX_THREADS);
  var label = GmailApp.getUserLabelByName(CLEANUP_LABEL) || GmailApp.createLabel(CLEANUP_LABEL);
  var archived = 0;
  threads.forEach(function (thread) {
    if (!safeMonitorThread_(thread, settings)) return;
    thread.addLabel(label);
    thread.moveToArchive();
    archived++;
  });
  var result = {archived: archived, skipped: threads.length - archived, scanned: threads.length};
  Logger.log(JSON.stringify(result));
  return result;
}

function installCleanupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (trigger) {
    if (trigger.getHandlerFunction() === "cleanupOldMonitorEmails") ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger("cleanupOldMonitorEmails").timeBased()
    .everyDays(1).atHour(18).inTimezone("America/Los_Angeles").create();
  Logger.log("Daily cleanup installed for 6-7 p.m. Pacific; archives only monitor threads older than 24 hours.");
}
