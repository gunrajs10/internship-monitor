"""Biotech internship monitor.

Deterministic scraper for graduate-eligible internship postings across a
watchlist of biotech companies. No AI at runtime. Designed to run on a
GitHub Actions cron every 2 hours.

Modes:
  python monitor.py            normal run: fetch, diff vs state, alert
  python monitor.py --audit    test every source, print a coverage table,
                               send nothing, save nothing

Core rules (from Gunraj's requirements):
  - Silence must mean "checked and nothing new," never "could not check."
    Any source failure produces a loud failure alert (rate-limited to one
    per source per 24h so it does not spam every run).
  - No silent baseline. The first real run reports every current matching
    posting once, then only new ones.
  - Dedupe by company + normalized title + location, not posting ID or
    URL, so reposts with new requisition numbers are not re-alerted.
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
import yaml

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "companies.yaml")
STATE_PATH = os.path.join(os.path.dirname(__file__), "state", "seen.json")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()

TIMEOUT = 30
RETRIES = 2
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Titles that count as internship-type roles.
TITLE_RE = re.compile(
    r"\b(intern(ship)?s?|co[\s\-]?op|mba\b|summer associate|"
    r"graduate (program|scheme|associate|intern)|leadership development program)\b",
    re.IGNORECASE,
)

# Graduate-eligibility terms searched in the job description.
GRAD_RE = re.compile(
    r"\b(master'?s|mba|graduate (student|degree|program)|ph\.?d|pharmd|"
    r"doctoral|advanced degree)\b",
    re.IGNORECASE,
)

# Locations that are clearly outside the US get dropped.
NON_US_RE = re.compile(
    r"\b(india|china|ireland|germany|switzerland|denmark|united kingdom|"
    r"\buk\b|singapore|japan|canada|mexico|brazil|poland|spain|france|"
    r"italy|netherlands|belgium|austria|hyderabad|shanghai|bangalore|"
    r"dublin|basel|copenhagen|taipei|taiwan|korea|australia)\b",
    re.IGNORECASE,
)

FAILURE_REALERT_HOURS = 24


class SourceError(Exception):
    """A source could not be conclusively checked."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _request(method, url, **kwargs):
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            resp = requests.request(
                method, url, headers=HEADERS, timeout=TIMEOUT, **kwargs
            )
            if resp.status_code == 200:
                return resp
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = type(exc).__name__
        time.sleep(2 * (attempt + 1))
    raise SourceError(f"{url} failed after {RETRIES + 1} attempts: {last_err}")


def _json_or_fail(resp, url):
    try:
        return resp.json()
    except ValueError:
        raise SourceError(f"{url} returned non-JSON (likely bot-blocked or wrong endpoint)")


# ---------------------------------------------------------------------------
# ATS adapters. Each returns a list of dicts:
#   {title, location, url, posted_on, description(optional)}
# and raises SourceError if the source cannot be conclusively read.
# ---------------------------------------------------------------------------

def fetch_workday(cfg):
    tenant = cfg["tenant"]
    host = cfg["host"]          # e.g. wd1, wd3, wd5
    site = cfg["site"]
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"

    results = {}
    for term in cfg.get("search_terms", ["intern", "co-op", "MBA", "graduate"]):
        offset = 0
        while True:
            payload = {
                "appliedFacets": {},
                "limit": 20,
                "offset": offset,
                "searchText": term,
            }
            resp = _request("POST", api, json=payload)
            data = _json_or_fail(resp, api)
            postings = data.get("jobPostings")
            total = data.get("total")
            if postings is None or total is None:
                raise SourceError(f"{api} responded without jobPostings/total (schema change?)")
            for p in postings:
                path = p.get("externalPath", "")
                results[path] = {
                    "title": p.get("title", "").strip(),
                    "location": p.get("locationsText", "") or "",
                    "url": f"{base}/en-US/{site}{path}",
                    "posted_on": p.get("postedOn", ""),
                    "_detail": f"{base}/wday/cxs/{tenant}/{site}{path}",
                }
            offset += 20
            if offset >= total or not postings:
                break
    return list(results.values())


def fetch_workday_detail(item):
    """Fetch a Workday job description for grad-eligibility check."""
    resp = _request("GET", item["_detail"])
    data = _json_or_fail(resp, item["_detail"])
    desc = (data.get("jobPostingInfo") or {}).get("jobDescription", "")
    return html.unescape(re.sub(r"<[^>]+>", " ", desc))


def fetch_greenhouse(cfg):
    slug = cfg["slug"]
    api = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    resp = _request("GET", api)
    data = _json_or_fail(resp, api)
    jobs = data.get("jobs")
    if jobs is None:
        raise SourceError(f"{api} responded without a jobs array")
    out = []
    for j in jobs:
        out.append({
            "title": (j.get("title") or "").strip(),
            "location": ((j.get("location") or {}).get("name") or ""),
            "url": j.get("absolute_url", ""),
            "posted_on": (j.get("updated_at") or "")[:10],
            "description": html.unescape(re.sub(r"<[^>]+>", " ", j.get("content") or "")),
        })
    return out


def fetch_phenom(cfg):
    """Phenom People career sites (jobs.jnj.com, careers.abbvie.com, etc.)
    expose a refineSearch widget API used by the site's own search page."""
    origin = cfg["origin"].rstrip("/")
    api = f"{origin}/widgets"
    results = {}
    for term in cfg.get("search_terms", ["intern", "co-op", "MBA"]):
        from_idx = 0
        while True:
            payload = {
                "lang": "en_us",
                "deviceType": "desktop",
                "country": "us",
                "pageName": "search-results",
                "ddoKey": "refineSearch",
                "sortBy": "Most recent",
                "subsearch": "",
                "from": from_idx,
                "jobs": True,
                "counts": True,
                "all_fields": ["category", "country", "state", "city", "type"],
                "size": 20,
                "clearAll": False,
                "jdsource": "facets",
                "isSliderEnable": False,
                "pageId": "page-search",
                "siteType": "external",
                "keywords": term,
                "global": True,
            }
            resp = _request("POST", api, json=payload)
            data = _json_or_fail(resp, api)
            block = data.get("refineSearch") or {}
            payload_jobs = (block.get("data") or {}).get("jobs")
            total = (block.get("data") or {}).get("totalHits", 0)
            if payload_jobs is None:
                raise SourceError(f"{api} responded without refineSearch.data.jobs (schema mismatch)")
            for j in payload_jobs:
                jid = j.get("jobId") or j.get("reqId") or j.get("jobSeqNo") or j.get("title")
                url = j.get("applyUrl") or ""
                if not url:
                    slug_path = j.get("jobUrl") or ""
                    url = slug_path if slug_path.startswith("http") else f"{origin}{slug_path}"
                loc = ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))
                results[jid] = {
                    "title": (j.get("title") or "").strip(),
                    "location": loc,
                    "url": url,
                    "posted_on": (j.get("postedDate") or j.get("dateCreated") or "")[:10],
                    "description": j.get("descriptionTeaser") or "",
                }
            from_idx += 20
            if from_idx >= total or not payload_jobs:
                break
    return list(results.values())


ADAPTERS = {
    "workday": fetch_workday,
    "greenhouse": fetch_greenhouse,
    "phenom": fetch_phenom,
}


# ---------------------------------------------------------------------------
# Filtering, dedupe, state
# ---------------------------------------------------------------------------

def normalize(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def dedupe_key(company, title, location):
    raw = f"{normalize(company)}|{normalize(title)}|{normalize(location)}"
    return hashlib.sha1(raw.encode()).hexdigest()


def is_candidate(item):
    if not TITLE_RE.search(item["title"]):
        return False
    if item["location"] and NON_US_RE.search(item["location"]):
        return False
    return True


def eligibility_note(item, cfg):
    """Best-effort grad-eligibility label. Never drops a posting."""
    desc = item.get("description", "")
    if not desc and "_detail" in item:
        try:
            desc = fetch_workday_detail(item)
        except SourceError:
            return "eligibility unverified (detail page unreachable) - check posting"
    if not desc:
        return "eligibility not stated in feed - check posting"
    if GRAD_RE.search(desc):
        return "graduate-eligible (verified in description)"
    return "grad eligibility not stated - check posting"


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"seen": {}, "failures": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_webhook(payload):
    if not WEBHOOK_URL:
        print("WARN: WEBHOOK_URL not set; printing payload instead")
        print(json.dumps(payload, indent=2))
        return
    resp = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    print(f"webhook -> HTTP {resp.status_code}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(audit=False):
    with open(CONFIG_PATH) as f:
        companies = yaml.safe_load(f)["companies"]

    state = load_state() if not audit else {"seen": {}, "failures": {}}
    now = datetime.now(timezone.utc)
    new_roles, failures, audit_rows = [], [], []

    for cfg in companies:
        name = cfg["name"]
        ats = cfg.get("ats", "unsupported")
        adapter = ADAPTERS.get(ats)
        if adapter is None:
            msg = f"no adapter yet for ATS type '{ats}' - check manually: {cfg.get('career_page', '')}"
            failures.append({"company": name, "reason": msg})
            audit_rows.append((name, "NO ADAPTER", msg))
            continue
        try:
            postings = adapter(cfg)
            candidates = [p for p in postings if is_candidate(p)]
            audit_rows.append((name, "OK", f"{len(postings)} postings, {len(candidates)} internship-type"))
            if audit:
                continue
            for item in candidates:
                key = dedupe_key(name, item["title"], item["location"])
                if key in state["seen"]:
                    continue
                note = eligibility_note(item, cfg)
                role = {
                    "company": name,
                    "title": item["title"],
                    "location": item["location"],
                    "posted_on": item.get("posted_on", ""),
                    "url": item["url"],
                    "eligibility": note,
                    "first_seen": now.isoformat(),
                }
                new_roles.append(role)
                state["seen"][key] = {
                    "company": name, "title": item["title"],
                    "location": item["location"], "first_seen": now.isoformat(),
                }
        except SourceError as exc:
            failures.append({"company": name, "reason": str(exc)})
            audit_rows.append((name, "FAIL", str(exc)))
        except Exception as exc:  # never let one company kill the run
            failures.append({"company": name, "reason": f"unexpected: {exc}"})
            audit_rows.append((name, "ERROR", f"unexpected: {exc}"))

    if audit:
        print("\n=== AUDIT RESULTS ===")
        for name, status, detail in audit_rows:
            print(f"{status:<11} {name}: {detail}")
        ok = sum(1 for r in audit_rows if r[1] == "OK")
        print(f"\n{ok}/{len(audit_rows)} sources fully readable.")
        return

    # Rate-limit failure alerts to one per source per 24h.
    fresh_failures = []
    for f_item in failures:
        sig = hashlib.sha1(f"{f_item['company']}|{f_item['reason'][:80]}".encode()).hexdigest()
        last = state["failures"].get(sig)
        if last and now - datetime.fromisoformat(last) < timedelta(hours=FAILURE_REALERT_HOURS):
            continue
        state["failures"][sig] = now.isoformat()
        fresh_failures.append(f_item)

    if new_roles:
        send_webhook({"type": "new_roles", "items": new_roles})
        print(f"{len(new_roles)} new role(s) reported")
    if fresh_failures:
        send_webhook({"type": "failures", "items": fresh_failures})
        print(f"{len(fresh_failures)} source failure(s) reported")
    if not new_roles and not fresh_failures:
        print("Nothing new; all configured sources checked or already-alerted failures.")

    save_state(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", action="store_true", help="test all sources, alert nothing")
    args = parser.parse_args()
    try:
        run(audit=args.audit)
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
