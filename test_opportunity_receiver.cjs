/* Offline receiver tests: node test_opportunity_receiver.cjs */
"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "opportunity_receiver.gs"), "utf8");
const HEADERS = ["Event ID", "Payload hash", "Status", "Updated"];

function fixture(options = {}) {
  const state = {rows: options.rows ? options.rows.map(r => [...r]) : [],
    exists: !!options.rows, hidden: false, handlerCalls: 0, sheetAccesses: 0,
    locks: 0, releases: 0, finds: 0, flushed: [], handlerEvents: []};
  function range(row, col, height, width) {
    assert.ok(height > 0, "Never request a zero-height header-only search range");
    return {
      getRow: () => row,
      getValues: () => Array.from({length: height}, (_, i) =>
        Array.from({length: width}, (_, j) => state.rows[row - 1 + i]?.[col - 1 + j] ?? "")),
      setValues(values) {
        assert.equal(values.length, height);
        for (let i = 0; i < height; i++) {
          assert.equal(values[i].length, width);
          state.rows[row - 1 + i] ||= [];
          for (let j = 0; j < width; j++) state.rows[row - 1 + i][col - 1 + j] = values[i][j];
        }
        return this;
      },
      createTextFinder(value) {
        assert.equal(col, 1); assert.equal(width, 1); assert.equal(row, 2);
        state.finds++;
        const flags = {};
        return {
          matchEntireCell(v) { flags.entire = v; return this; },
          matchCase(v) { flags.case = v; return this; },
          useRegularExpression(v) { flags.regex = v; return this; },
          findNext() {
            assert.deepEqual(flags, {entire: true, case: true, regex: false});
            for (let i = row - 1; i < row - 1 + height; i++) {
              if (state.rows[i]?.[0] === value) return range(i + 1, 1, 1, 1);
            }
            return null;
          }
        };
      }
    };
  }
  const sheet = {getRange: range, getLastRow: () => state.rows.length,
    hideSheet() { state.hidden = true; }};
  const context = vm.createContext({
    handleMonitorPost_(event) {
      state.handlerCalls++; state.handlerEvents.push(event);
      if (options.handler) return options.handler(event, state);
      if (options.throwHandler) throw new Error("Mail failure; detail must not leak");
      return options.legacyReturn ?? {legacy: true};
    },
    LockService: {getScriptLock() { return {
      tryLock(ms) { assert.equal(ms, 30000); state.locks++; return !options.locked; },
      releaseLock() { state.releases++; }
    }; }},
    Utilities: {DigestAlgorithm: {SHA_256: "sha256"}, Charset: {UTF_8: "utf8"},
      computeDigest(algorithm, text, charset) {
        assert.equal(algorithm, "sha256"); assert.equal(charset, "utf8");
        return [...crypto.createHash(algorithm).update(text, charset).digest()]
          .map(b => b > 127 ? b - 256 : b);
      }},
    SpreadsheetApp: {
      getActiveSpreadsheet() {
        state.sheetAccesses++;
        return {
          getSheetByName(name) { assert.equal(name, "_Opportunity Delivery Receipts"); return state.exists ? sheet : null; },
          insertSheet(name) { assert.equal(name, "_Opportunity Delivery Receipts"); state.exists = true; return sheet; }
        };
      },
      flush() {
        if (options.failSentFlush && state.rows.at(-1)?.[2] === "sent") throw new Error("Flush failed");
        state.flushed.push(state.rows.map(r => [...r]));
      }
    },
    ContentService: {MimeType: {JSON: "application/json"}, createTextOutput(text) {
      return {text, setMimeType(mime) { assert.equal(mime, "application/json"); return this; }};
    }}
  });
  vm.runInContext(source, context, {filename: "opportunity_receiver.gs"});
  return {state, context, call(value) {
    return context.doPost({postData: {contents: typeof value === "string" ? value : JSON.stringify(value)}});
  }, json(value) { return JSON.parse(this.call(value).text); }};
}

const job = {type: "new_roles", event_id: "opportunity-test-1", items: [{title: "MBA Intern"}]};
const tests = [];
function test(name, fn) { tests.push([name, fn]); }

test("first delivery persists processing before handler, then sent", () => {
  const f = fixture({handler(event, state) {
    assert.equal(state.flushed.at(-1)[1][2], "processing");
    assert.equal(event.postData.contents, JSON.stringify(job));
  }});
  assert.deepEqual(f.json(job), {ok: true, event_id: job.event_id});
  assert.deepEqual(f.state.rows[0], HEADERS);
  assert.equal(f.state.rows[1][2], "sent");
  assert.equal(f.state.rows[1][1], crypto.createHash("sha256").update(f.context.opportunityCanonical_(job)).digest("hex"));
  assert.equal(f.state.hidden, true);
  assert.equal(f.state.releases, 1);
});

test("same sent event and exact body acknowledge without sending twice", () => {
  const f = fixture(); f.json(job);
  assert.deepEqual(f.json(job), {ok: true, duplicate: true, event_id: job.event_id});
  assert.equal(f.state.handlerCalls, 1);
  assert.equal(f.state.rows.length, 2);
  assert.equal(f.state.releases, 2);
});

test("same ID with changed job content is a hash conflict", () => {
  const f = fixture(); f.json(job);
  for (const changed of [{...job, items: [{title: "Different role"}]}]) {
    assert.deepEqual(f.json(changed), {ok: false, error: "payload_hash_mismatch",
      message: "This event ID already belongs to different content; no delivery attempted.", event_id: job.event_id});
  }
  assert.equal(f.state.handlerCalls, 1);
  assert.equal(f.state.rows[1][2], "sent");
});

test("queue restore reordering and whitespace do not resend", () => {
  const f = fixture();
  const original = {...job, items: [{title: "MBA Intern", location: "CA"}]};
  assert.equal(f.json(original).ok, true);
  const restored = {items: [{location: "CA", title: "MBA Intern"}], event_id: job.event_id, type: "new_roles"};
  assert.equal(f.json(JSON.stringify(restored, null, 2)).duplicate, true);
  assert.equal(f.state.handlerCalls, 1);
});

test("handler exception records uncertain and cannot resend", () => {
  const f = fixture({throwHandler: true});
  const first = f.json(job);
  assert.equal(first.ok, false); assert.equal(first.error, "delivery_uncertain");
  assert.equal(first.event_id, job.event_id);
  assert.equal(f.state.rows[1][2], "uncertain");
  assert.equal(f.json(job).error, "delivery_uncertain");
  assert.equal(f.state.handlerCalls, 1);
  assert.equal(f.state.releases, 2);
  assert.ok(!JSON.stringify(first).includes("Mail failure"));
});

test("interrupted processing receipt cannot be replayed", () => {
  const hash = crypto.createHash("sha256").update(fixture().context.opportunityCanonical_(job)).digest("hex");
  const f = fixture({rows: [HEADERS, [job.event_id, hash, "processing", "yesterday"]]});
  assert.equal(f.json(job).error, "delivery_uncertain");
  assert.equal(f.state.handlerCalls, 0);
  assert.equal(f.state.rows[1][2], "processing");
});

test("a reconciled unsent receipt can run once and reuses its audit row", () => {
  const hash = crypto.createHash("sha256").update(fixture().context.opportunityCanonical_(job)).digest("hex");
  const f = fixture({rows: [HEADERS, [job.event_id, hash, "confirmed_unsent", "yesterday"]]});
  assert.equal(f.json(job).ok, true);
  assert.equal(f.state.rows.length, 2);
  assert.equal(f.json(job).duplicate, true);
  assert.equal(f.state.handlerCalls, 1);
});

test("tracker repair skips existing URLs and appends only missing rows", () => {
  const f = fixture();
  const data = [["headers"], ["seen", "Employer", "Old", "CA", "", "", "https://example.org/old"]];
  const sh = {getLastRow: () => data.length, getMaxRows: () => 100,
    getRange(row, col, height, width) { return {
      getValues: () => data.slice(row-1, row-1+height).map(r => r.slice(col-1,col-1+width)),
      setValues: rows => rows.forEach((r,i) => {data[row-1+i] = Array.from(r);})
    };}};
  f.context.TAB_EARLY = "Early"; f.context.TAB_PROGRAMS = "Programs";
  f.context.HEADERS = []; f.context.fmtDate = v => String(v || "now");
  f.context.getSheet = () => sh;
  const p={items:[{url:"https://example.org/old"},{url:"https://example.org/new",title:"New"},{url:"https://example.org/new",title:"Duplicate"}]};
  f.context.opportunityWriteRows_({},p);
  assert.equal(data.length,3); assert.equal(data[2][2],"New");
  f.context.opportunityWriteRows_({},p);
  assert.equal(data.length,3);
});

test("receipt write failure after handler leaves uncertain guard", () => {
  const f = fixture({failSentFlush: true});
  assert.equal(f.json(job).error, "delivery_uncertain");
  assert.equal(f.state.rows[1][2], "uncertain");
  assert.equal(f.json(job).error, "delivery_uncertain");
  assert.equal(f.state.handlerCalls, 1);
});

test("busy receiver returns locked without sheets or delivery", () => {
  const f = fixture({locked: true});
  const reply = f.json(job);
  assert.equal(reply.ok, false); assert.equal(reply.error, "locked");
  assert.equal(reply.event_id, job.event_id);
  assert.equal(f.state.sheetAccesses, 0); assert.equal(f.state.handlerCalls, 0);
  assert.equal(f.state.releases, 0);
});

test("legacy requests pass through unchanged", () => {
  const legacy = {legacy: "original response"};
  const f = fixture({legacyReturn: legacy});
  for (const input of [{type: "new_roles", items: [{}]},
    {type: "heartbeat", event_id: "opportunity-heartbeat", items: []},
    {type: "failures", event_id: "other-monitor-1", items: [{}]}]) {
    assert.equal(f.call(input), legacy);
    assert.equal(f.state.handlerEvents.at(-1).postData.contents, JSON.stringify(input));
  }
  assert.equal(f.state.locks, 0); assert.equal(f.state.sheetAccesses, 0);
});

test("connection check proves protocol without any side effects", () => {
  const f = fixture({throwHandler: true, locked: true});
  assert.deepEqual(f.json({type: "connection_check"}), {ok: true, protocol: "opportunity-receipts-v1"});
  assert.deepEqual(f.json({type: "connection_check", event_id: "opportunity-probe"}),
    {ok: true, protocol: "opportunity-receipts-v1", event_id: "opportunity-probe"});
  assert.equal(f.state.locks, 0); assert.equal(f.state.sheetAccesses, 0);
  assert.equal(f.state.handlerCalls, 0);
});

test("header-only receipt sheet does not request zero rows", () => {
  const f = fixture({rows: [HEADERS]});
  assert.equal(f.json(job).ok, true);
  assert.equal(f.state.finds, 0);
});

test("receipt lookup uses whole-cell case-sensitive ID matching", () => {
  const f = fixture({rows: [HEADERS, ["opportunity-test-10", "other", "sent", "today"]]});
  assert.equal(f.json(job).ok, true);
  assert.equal(f.state.rows.length, 3);
  assert.equal(f.state.handlerCalls, 1);
});

test("batch bounds reject invalid items before lock and accept 200", () => {
  const f = fixture();
  for (const items of [null, "bad", [], Array.from({length: 201}, () => ({}))]) {
    const reply = f.json({...job, items});
    assert.equal(reply.error, "invalid_batch"); assert.equal(reply.event_id, job.event_id);
  }
  assert.equal(f.state.locks, 0); assert.equal(f.state.handlerCalls, 0);
  assert.equal(f.json({...job, items: Array.from({length: 200}, () => ({}))}).ok, true);
});

test("local failure event uses the same receipt guard", () => {
  const f = fixture();
  const payload = {type: "failures", event_id: "windows-opportunity-failure-1", items: [{reason: "Offline"}]};
  assert.equal(f.json(payload).ok, true);
  assert.equal(f.json(payload).duplicate, true);
  assert.equal(f.state.handlerCalls, 1);
});

let failed = 0;
for (const [name, fn] of tests) {
  try { fn(); process.stdout.write("PASS " + name + "\n"); }
  catch (error) { failed++; process.stderr.write("FAIL " + name + "\n" + error.stack + "\n"); }
}
process.stdout.write(`${tests.length - failed}/${tests.length} receiver tests passed.\n`);
if (failed) process.exitCode = 1;
