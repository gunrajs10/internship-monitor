"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require("node:path").join(__dirname, "monitor_email_cleanup.gs"), "utf8");
const now = Date.parse("2026-10-08T01:30:00Z"), cutoff = now - 86400000, email = "user@example.test";
let archived = 0, removed = [], installed = [], wrongAccount = false;
function thread(messages, last = Math.max(...messages.map(m => m.time))) {
  return {getLastMessageDate: () => new Date(last), getMessages: () => messages.map(m => ({
    getFrom: () => m.from, getSubject: () => m.subject, getDate: () => new Date(m.time)
  })), addLabel: () => {}, moveToArchive: () => { archived++; }};
}
const good = {from: "User <"+email+">", subject:"[Internships] 2 new posting(s)", time:cutoff-1};
const old = thread([good]), recent = thread([{...good,time:cutoff}]);
const unrelated = thread([{...good,subject:"Internship interview response"}]);
const spoof = thread([{...good,from:"fake-"+email}]);
const mixed = thread([good,{...good,time:now}]);
const ctx = vm.createContext({EMAIL:email, Date: class extends Date {static now(){return now;}},
  Session:{getEffectiveUser:()=>({getEmail:()=>wrongAccount?"other@example.test":email})},
  Logger:{log:()=>{}},
  GmailApp:{search:(q,start,max)=>{assert.ok(q.includes("before:"+Math.floor(cutoff/1000)));assert.equal(start,0);assert.equal(max,500);return [old,recent,unrelated,spoof,mixed];}, getUserLabelByName:()=>({})},
  ScriptApp:{getProjectTriggers:()=>["cleanupOldMonitorEmails","unrelatedHandler"].map(name=>({getHandlerFunction:()=>name})),
    deleteTrigger:t=>removed.push(t.getHandlerFunction()),newTrigger:name=>({timeBased:()=>({everyDays:n=>{assert.equal(n,1);return {atHour:h=>{assert.equal(h,18);return {inTimezone:z=>{assert.equal(z,"America/Los_Angeles");return {create:()=>installed.push(name)};}};}};}})})}
});
vm.runInContext(source,ctx);
assert.equal(ctx.cleanupDryRun(),1); assert.equal(archived,0);
assert.equal(ctx.cleanupOldMonitorEmails().archived,1);assert.equal(archived,1);
ctx.installCleanupTrigger();assert.deepEqual(removed,["cleanupOldMonitorEmails"]);assert.deepEqual(installed,["cleanupOldMonitorEmails"]);
wrongAccount=true;assert.throws(()=>ctx.cleanupOldMonitorEmails(),/configured monitor recipient/);
console.log("PASS: 24-hour boundary, new replies, unrelated mail, sender spoof, dry run, account guard, daily trigger scope");
