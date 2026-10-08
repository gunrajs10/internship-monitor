"""Zero-AI companion job monitor. Public sources, durable queue, verified delivery.

Run --dry-run for a live preview; --normal requires the existing WEBHOOK_URL.
No LinkedIn credentials, paid APIs, or model calls are used.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
import urllib.error
import urllib.parse
import urllib.request

from opportunity_sources import collect, fetch_detail
from opportunity_rank import assess, needs_detail

ROOT = Path(__file__).resolve().parent
SCHEMA = 1


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def read_json(path, default=None):
    if not Path(path).exists():
        return copy.deepcopy(default)
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"Cannot read {Path(path).name}; preserving it rather than resetting state") from exc


def new_state():
    return {"schema": SCHEMA, "pending": {}, "delivered": {}, "rejected": {},
            "health": {}, "notifications": {}, "last_run": None, "last_delivery": None}


def load_state(path):
    state = read_json(path, new_state())
    if (not isinstance(state, dict) or state.get("schema") != SCHEMA or
            any(not isinstance(state.get(key), dict) for key in
                ("pending", "delivered", "rejected", "health", "notifications"))):
        raise RuntimeError("Invalid monitor state; refusing to silently reset the delivery ledger")
    return state


def atomic_save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def normalize(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def fingerprint(job):
    # Location remains in the fallback: separate geographical requisitions matter.
    value = "|".join(normalize(job.get(k)) for k in ("company", "title", "location"))
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def identity(job):
    source = str(job.get("source", "unknown"))
    board = str(job.get("board_token", ""))
    value = job.get("id") or job.get("url") or fingerprint(job)
    return f"{source}:{board}:{value}"


def safe_job(job):
    result = dict(job)
    # Only public posting data are retained. No HTTP headers or credentials.
    for k in list(result):
        if k.startswith("_"):
            result.pop(k)
    result["fingerprint"] = fingerprint(job)
    return result


def ingest(state, jobs, config, now):
    # A new requisition at the same employer can have the same title/location.
    # Fuzzy dedupe is only for contemporary sightings on different platforms.
    duplicates = list(state["pending"].values()) + [x for x in state["delivered"].values()
        if x.get("delivered_at", "") >= iso(now - timedelta(days=30))]
    added = 0
    # Prefer a complete employer inventory record over a matching discovery card.
    for raw in sorted(jobs, key=lambda j: not bool(j.get("description"))):
        job = safe_job(raw)
        key = identity(job)
        if key in state["delivered"]:
            continue
        if key in state["pending"]:
            if key in state.get("inflight", {}).get("keys", []):
                continue  # Freeze an ambiguously delivered batch until acknowledged.
            # Keep failed delivery/detail metadata; fresh public data may improve it.
            previous = state["pending"][key]
            first_seen = previous["first_seen"]
            previous.update({k: v for k, v in job.items() if v not in (None, "")})
            previous["first_seen"] = first_seen
            continue
        duplicate = next((x for x in duplicates if job["fingerprint"] == x.get("fingerprint") and
                          job.get("source") != x.get("source")), None)
        if duplicate is not None:
            if job.get("description") and not duplicate.get("description") and "first_seen" in duplicate:
                for field in ("description", "url", "industry", "salary", "posted_on"):
                    if job.get(field):
                        duplicate[field] = job[field]
                duplicate.update(detail_pending=False, detail_error="", detail_status="ok",
                                 detail_source=job.get("source"))
            continue
        # Recheck previously rejected content when its public description/title changes.
        signature = hashlib.sha256(json.dumps(job, sort_keys=True, default=str).encode()).hexdigest()[:20]
        if state["rejected"].get(key, {}).get("signature") == signature:
            continue
        verdict = assess(job, config)
        detail_needed = not job.get("description") and needs_detail(job, config)
        if not verdict["include"] and not detail_needed:
            state["rejected"][key] = {"signature": signature, "checked_at": iso(now)}
            continue
        job.update(first_seen=iso(now), assessment=verdict, detail_pending=detail_needed,
                   detail_attempts=0, signature=signature)
        state["pending"][key] = job
        duplicates.append(job)
        added += 1
    return added


def rank_key(job):
    verdict = job.get("assessment", {})
    posted = str(job.get("posted_on") or job.get("first_seen") or "")
    return (verdict.get("location_priority", 0), -verdict.get("score", 0), not verdict.get("california", False),
            tuple(-ord(c) for c in posted), normalize(job.get("company")), identity(job))


def enrich_pending(state, config, now, detail_fetcher=fetch_detail):
    budget = int(config.get("detail_limit", 50))
    attempted = 0
    # Apply a narrowed geographic policy to the existing queue before spending
    # scarce detail requests. Never alter an ambiguous in-flight delivery.
    inflight = set(state.get("inflight", {}).get("keys", []))
    for key, job in list(state["pending"].items()):
        if key in inflight:
            continue
        verdict = assess(job, config)
        job["assessment"] = verdict
        if verdict.get("geography_excluded"):
            state["rejected"][key] = {"signature": job.get("signature", ""), "checked_at": iso(now)}
            del state["pending"][key]
    # Spend most capacity on priority, reserving a fifth for the oldest deferred
    # candidates so fresh strong fits are prompt and less obvious roles progress.
    items = list(state["pending"].items())
    deferred = [(k, j) for k, j in items if j.get("detail_pending")]
    priority_order = sorted(deferred, key=lambda p: (p[1].get("detail_attempts", 0), rank_key(p[1])))
    selected = priority_order[:max(1, budget - max(1, budget // 5))]
    selected_keys = {k for k, _ in selected}
    oldest = sorted((p for p in deferred if p[0] not in selected_keys),
                    key=lambda p: (p[1].get("detail_attempts", 0), p[1]["first_seen"], rank_key(p[1])))
    selected.extend(oldest[:max(0, budget - len(selected))])
    selected_keys = {k for k, _ in selected}
    queued = selected + [(k, j) for k, j in items if k not in selected_keys]
    linkedin_blocked = state.get("health", {}).get("linkedin", {}).get("circuit_open", False)
    for key, job in queued:
        if key in state.get("inflight", {}).get("keys", []):
            continue
        stopped = False
        if job.get("detail_pending"):
            if linkedin_blocked and job.get("source") == "linkedin":
                continue
            if attempted >= budget:
                continue
            attempted += 1
            job["detail_attempts"] = job.get("detail_attempts", 0) + 1
            try:
                result = detail_fetcher(job)
                job.update({k: v for k, v in result.items() if v not in (None, "")})
                job["detail_error"] = ""
            except Exception as exc:
                # No credential-bearing exception URLs are logged or persisted.
                job["detail_error"] = f"Detail unavailable ({type(exc).__name__}); verify requirements on the posting"
                if getattr(exc, "circuit", False) and job.get("source") == "linkedin":
                    linkedin_blocked = True
                    stopped = True
                    state["health"]["linkedin:details"] = {
                        "status": "failed", "checked_at": iso(now), "count": 0,
                        "error": "Public LinkedIn details unavailable; further LinkedIn requests stopped for this run. Pending candidates are retained."}
            job["detail_pending"] = False
        verdict = assess(job, config)
        if job.get("detail_error"):
            verdict.setdefault("cautions", []).append(job["detail_error"])
        job["assessment"] = verdict
        if stopped:
            job["detail_pending"] = True
            continue
        if not verdict["include"]:
            if job.get("detail_error") and job.get("detail_attempts", 0) < 3:
                job["detail_pending"] = True
                continue
            # Unclassifiable failures stay deferred, not silently discarded.
            if job.get("detail_error"):
                job["detail_pending"] = True
                job["review_needed"] = True
                continue
            state["rejected"][key] = {"signature": job["signature"], "checked_at": iso(now)}
            del state["pending"][key]
    return attempted


class DeliveryError(RuntimeError):
    pass


def post_webhook(payload, endpoint=None):
    try:
        return _post_webhook_once(payload, endpoint)
    except DeliveryError as original:
        event = str(payload.get("event_id", ""))
        if payload.get("type") not in {"new_roles", "failures"} or not event.startswith(("opportunity-", "windows-opportunity-")):
            raise
        # Verify the completed receipt with a small, read-only request. Never
        # automatically replay the job/email POST after losing its response.
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        check = {"type": "receipt_check", "event_id": event,
                 "payload_hash": hashlib.sha256(canonical.encode()).hexdigest()}
        try:
            return _post_webhook_once(check, endpoint)
        except DeliveryError:
            raise original from None


def _post_webhook_once(payload, endpoint=None):
    endpoint = endpoint or os.environ.get("WEBHOOK_URL", "").strip()
    if not endpoint:
        raise DeliveryError("WEBHOOK_URL is missing; no jobs will be marked delivered")
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https":
        raise DeliveryError("The delivery endpoint must use HTTPS")
    # Apps Script acknowledgements redirect to a one-time content URL. Keep
    # each transport request fresh without changing the durable event/payload.
    if parsed.hostname == "script.google.com":
        endpoint += ("&" if parsed.query else "?") + "monitor_request_id=" + str(time.time_ns())
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    request = urllib.request.Request(endpoint, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "OpportunityMonitor/1.0",
                 "Cache-Control": "no-cache"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read(200000).decode("utf-8", "replace").strip()
            status = response.status
    except Exception as exc:
        # Automatic POST retries are avoided: a timeout may follow actual delivery.
        raise DeliveryError(f"Delivery unconfirmed ({type(exc).__name__}); pending jobs retained") from None
    if status != 200:
        raise DeliveryError(f"Delivery returned HTTP {status}; pending jobs retained")
    try:
        reply = json.loads(body)
    except ValueError:
        reply = None
    accepted = body.casefold() in {"ok", "success", "received"}
    if isinstance(reply, dict):
        accepted = (reply.get("ok") is True or reply.get("success") is True or
                    str(reply.get("status", "")).casefold() in {"ok", "success"})
        if reply.get("error") or reply.get("ok") is False or reply.get("success") is False:
            accepted = False
        if "event_id" in reply and reply["event_id"] != payload.get("event_id"):
            accepted = False
    if not accepted:
        raise DeliveryError("Delivery did not return a recognized success acknowledgement; pending jobs retained")
    if payload.get("type") == "receipt_check" and (not isinstance(reply, dict) or
            reply.get("receipt_verified") is not True or reply.get("event_id") != payload.get("event_id")):
        raise DeliveryError("Completed receipt was not verified; pending jobs retained")
    return True


def role_payload(job):
    a = job["assessment"]
    parts = [f"Opportunity Monitor | {a.get('priority', 'PROMISING')} | fit {a.get('score', 0)}/100"]
    if a.get("california"):
        parts.append("CALIFORNIA")
    parts.extend(a.get("reasons", []))
    if a.get("salary_text"):
        parts.append("Posted pay: " + a["salary_text"])
    parts.append(a.get("eligibility") or "Check eligibility")
    if a.get("cautions"):
        parts.append("CHECK: " + "; ".join(dict.fromkeys(a["cautions"])))
    parts.append("Source: " + str(job.get("source", "")))
    return {"company": job["company"], "title": job["title"], "location": job.get("location", ""),
            "url": job["url"], "posted_on": job.get("posted_on", ""),
            "first_seen": job["first_seen"], "track": a.get("track", "early-stage"),
            "priority": a.get("priority", "PROMISING"), "eligibility": " | ".join(parts)}


def send_pending(state, config, path, now, sender=post_webhook):
    ready = sorted(((k, j) for k, j in state["pending"].items()
                    if not j.get("detail_pending") and j.get("assessment", {}).get("include")),
                   key=lambda p: rank_key(p[1]))
    batch_size = max(1, int(config.get("delivery", {}).get("batch_size", 25)))
    max_batches = max(1, int(config.get("delivery", {}).get("max_batches_per_run", 8)))
    sent = 0
    inflight = state.get("inflight")
    frozen_keys = set(inflight.get("keys", [])) if inflight else set()
    if frozen_keys and not frozen_keys.issubset(state["pending"]):
        raise DeliveryError("Saved in-flight batch is inconsistent; preserve state and review it")
    ready = [(k, j) for k, j in ready if k not in frozen_keys]
    batches = [[(k, state["pending"][k]) for k in inflight["keys"]]] if frozen_keys else []
    for start in range(0, len(ready), batch_size):
        batches.append(ready[start:start + batch_size])
    for index, batch in enumerate(batches[:max_batches]):
        if index == 0 and frozen_keys:
            event = inflight["event_id"]
            payload = inflight.get("payload") or {"type": "new_roles", "event_id": event,
                                                   "items": [role_payload(j) for _, j in batch]}
        else:
            event = "opportunity-" + hashlib.sha256("|".join(sorted(k for k, _ in batch)).encode()).hexdigest()[:24]
            payload = {"type": "new_roles", "event_id": event, "items": [role_payload(j) for _, j in batch]}
        state["inflight"] = {"event_id": event, "keys": [k for k, _ in batch], "payload": payload,
                             "started": iso(now)}
        atomic_save(path, state)
        if sender(payload) is not True:
            raise DeliveryError("Delivery was not acknowledged; pending jobs retained")
        for key, job in batch:
            state["delivered"][key] = {"fingerprint": job["fingerprint"], "company": job["company"],
                                      "source": job.get("source"),
                                      "title": job["title"], "location": job.get("location", ""),
                                      "url": job["url"], "delivered_at": iso(now), "event_id": event}
            del state["pending"][key]
        state.pop("inflight", None)
        state["last_delivery"] = iso(now)
        atomic_save(path, state)
        sent += len(batch)
    return sent


def notify_health(state, path, now, sender=post_webhook):
    problems = []
    for name, record in state["health"].items():
        if record.get("status") not in {"ok", "disabled"}:
            if name == "linkedin":  # Per-query detail already describes the aggregate.
                continue
            message = record.get("error") or ("Result limit reached; coverage incomplete" if record.get("truncated") else record.get("status", "unavailable"))
            problems.append({"company": "Opportunity Monitor / " + name, "reason": message})
    stalled = sum(1 for job in state["pending"].values() if job.get("review_needed"))
    if stalled:
        problems.append({"company": "Opportunity Monitor / pending details",
                         "reason": f"{stalled} candidate(s) retained because details could not be verified; discovery continues."})
    if not problems:
        return
    sig = hashlib.sha256(json.dumps(problems, sort_keys=True).encode()).hexdigest()[:20]
    previous = state["notifications"].get(sig)
    if previous and now - datetime.fromisoformat(previous) < timedelta(hours=24):
        return
    if sender({"type": "failures", "event_id": "opportunity-health-" + sig + "-" + now.strftime("%Y%m%d"), "items": problems}) is not True:
        raise DeliveryError("Health alert was not acknowledged")
    state["notifications"][sig] = iso(now)
    atomic_save(path, state)


def preview_document(state, now):
    jobs = sorted(state["pending"].values(), key=rank_key)
    rows = []
    for job in jobs:
        if not job.get("assessment", {}).get("include"):
            continue
        payload = role_payload(job)
        url = payload["url"] if str(payload["url"]).startswith("https://") else "#"
        rows.append('<article><h2><a href="' + html.escape(url, quote=True) + '">' +
                    html.escape(payload["title"]) + '</a></h2><p><strong>' +
                    html.escape(payload["company"]) + '</strong> · ' + html.escape(payload["location"]) +
                    '</p><p>' + html.escape(payload["eligibility"]) + '</p><small>Posted: ' +
                    html.escape(payload["posted_on"] or "not provided") + '</small></article>')
    health_rows = ''.join('<tr><td>' + html.escape(k) + '</td><td>' + html.escape(str(v.get("status"))) +
                         '</td><td>' + html.escape(str(v.get("count", ""))) + '</td><td>' +
                         html.escape(str(v.get("error") or "")) + '</td></tr>' for k, v in state["health"].items())
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>Biotech Opportunity Monitor</title><style>body{font:16px/1.6 system-ui;max-width:1000px;margin:40px auto;padding:0 20px;background:#f5f7fa;color:#172c3c}h1{line-height:1.15}article{background:white;border:1px solid #dce3ea;border-radius:12px;padding:20px;margin:18px 0}h2{font-size:20px;margin:0}a{color:#075c70}small{color:#586778}table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:8px;border-bottom:1px solid #ccd6df}p{overflow-wrap:anywhere}</style>'
            '<h1>Biotech Opportunity Monitor</h1><p>Commercial strategy first · US opportunities · California highlighted</p>'
            '<p>Preview generated ' + html.escape(iso(now)) + '. Rules-based ranking; eligibility requires checking the employer posting. No AI used during monitoring.</p>'
            '<p>' + str(len(rows)) + ' promising candidates; ' + str(len(jobs)) + ' total pending.</p>' + ''.join(rows) +
            '<h2>Source health</h2><table><tr><th>Source</th><th>Status</th><th>Jobs</th><th>Coverage note</th></tr>' + health_rows + '</table></html>')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--normal", action="store_true")
    mode.add_argument("--audit", action="store_true")
    mode.add_argument("--test-email", action="store_true")
    mode.add_argument("--failure-email", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, default=ROOT / "opportunity_config.json")
    parser.add_argument("--state", type=Path, default=ROOT / "state" / "opportunity.json")
    parser.add_argument("--preview", type=Path, default=ROOT / "opportunity-preview.html")
    args = parser.parse_args(argv)
    now = utcnow()
    if args.failure_email:
        repo = os.environ.get("GITHUB_REPOSITORY", "gunrajs10/internship-monitor")
        run = os.environ.get("GITHUB_RUN_ID", "")
        url = "https://github.com/" + repo + "/actions" + ("/runs/" + run if run else "")
        post_webhook({"type": "failures", "event_id": "opportunity-run-failure-" + run,
                      "items": [{"company": "OPPORTUNITY MONITOR — RUN INCOMPLETE",
                                 "reason": "Discovery, delivery, tests, or state persistence failed. Pending jobs are retained when possible; a recovery artifact may be available. Check this run: " + url}]})
        return 0
    if args.test_email:
        post_webhook({"type": "failures", "event_id": "opportunity-setup-test-" + now.strftime("%Y%m%d%H%M"),
                      "items": [{"company": "OPPORTUNITY MONITOR — DELIVERY TEST",
                                 "reason": "Successful setup test. This is not a source failure or a job application. New ranked biotech/healthcare roles will use this email connection; no AI or LinkedIn login is required."}]})
        print("Test notification acknowledged by existing email connection.")
        return 0
    if args.normal and not os.environ.get("WEBHOOK_URL", "").strip():
        raise DeliveryError("WEBHOOK_URL is missing; refusing a production run")
    config = read_json(args.config)
    state = load_state(args.state)
    print("Checking public job sources...", flush=True)
    jobs, health = collect(config, state["health"], now)
    print(f"Collected {len(jobs)} public postings; screening and checking candidate details...", flush=True)
    state["health"] = health
    state["last_run"] = iso(now)
    if args.audit:
        print(json.dumps({"discovered": len(jobs), "sources": health}, indent=2))
        return 2 if not any(v.get("status") == "ok" for v in health.values()) else 0
    added = ingest(state, jobs, config, now)
    if args.normal:
        atomic_save(args.state, state)  # Queue survives detail/network failures.
    details = enrich_pending(state, config, now)
    if args.normal:
        atomic_save(args.state, state)
    args.preview.parent.mkdir(parents=True, exist_ok=True)
    args.preview.write_text(preview_document(state, now), encoding="utf-8")
    # A dry-run snapshot permits quality review without repeatedly hitting sources.
    if args.dry_run:
        atomic_save(args.preview.with_suffix(".json"), state)
    sent = 0
    if args.normal:
        sent = send_pending(state, config, args.state, now)
        notify_health(state, args.state, now)
    summary = {"mode": "normal" if args.normal else "dry-run", "discovered": len(jobs), "new_pending": added,
               "detail_attempts": details, "sent": sent, "pending": len(state["pending"]),
               "sources_ok": sum(v.get("status") == "ok" for k, v in health.items() if k != "linkedin"),
               "sources_degraded": sum(v.get("status") not in {"ok", "disabled"} for k, v in health.items() if k != "linkedin")}
    print(json.dumps(summary, indent=2))
    return 0 if summary["sources_ok"] else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (DeliveryError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
