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
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
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
    r"dublin|basel|copenhagen|taipei|taiwan|korea|australia|madrid|"
    r"barcelona|ludwigshafen|maidenhead|campoverde|mainz|wiesbaden|"
    r"zurich|tolochenaz|lyon|paris|london|edinburgh|lisbon|warsaw|"
    r"krakow|budapest|bucharest|beijing|seoul|tokyo|osaka|mumbai|"
    r"delhi|chennai|pune|sao paulo|buenos aires|bogota|lima|santiago|"
    r"toronto|vancouver|montreal|mississauga|israel|egypt|turkey|"
    r"ukraine|vietnam|indonesia|malaysia|philippines|pakistan|panama|"
    r"saudi arabia|south africa|slovakia|czech|romania|bulgaria)\b",
    re.IGNORECASE,
)

# Positive US indicators. A non-empty location must match this (and not
# NON_US_RE) to be kept - Gunraj wants US-only postings.
US_RE = re.compile(
    r"\b(united states|usa|u\.s\.a?\.?|remote)\b"
    r"|\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|"
    r"kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    r"new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|"
    r"pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|"
    r"utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|"
    r"district of columbia|puerto rico)\b"
    r"|,\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
    r"MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|"
    r"VT|VA|WA|WV|WI|WY|DC)\b"
    r"|\b(san francisco|san diego|los angeles|thousand oaks|foster city|"
    r"south san francisco|santa monica|el segundo|carlsbad|la jolla|novato|"
    r"san rafael|redwood city|menlo park|palo alto|sunnyvale|emeryville|"
    r"berkeley|hayward|fremont|alameda|brisbane|boston|waltham|lexington|"
    r"bedford|tarrytown|new york|princeton|summit|basking ridge|nutley|"
    r"rahway|kenilworth|plainsboro|titusville|whitehouse station|horsham|"
    r"malvern|king of prussia|collegeville|spring house|west point|"
    r"gaithersburg|rockville|frederick|wilmington|research triangle park|"
    r"raleigh|durham|clayton|indianapolis|north chicago|chicago|madison|"
    r"cincinnati|columbus|ann arbor|minneapolis|saint louis|st\.? louis|"
    r"salt lake city|phoenix|austin|dallas|houston|denver|boulder|portland|"
    r"philadelphia|pittsburgh|seattle|bothell)\b",
    re.IGNORECASE,
)

# Location strings that reveal nothing about the country ("2 Locations",
# "Multiple Locations"). Kept rather than dropped - better to over-report.
UNKNOWN_LOC_RE = re.compile(r"\b\d+\s+locations\b|multiple locations|various", re.IGNORECASE)

# Undergrad-only postings get dropped (Gunraj is an MBA candidate).
UNDERGRAD_TITLE_RE = re.compile(r"\b(undergrad(uate)?|high school)\b", re.IGNORECASE)
UNDERGRAD_DESC_RE = re.compile(
    r"(pursuing (a |an )?(bachelor|undergraduate)|"
    r"currently enrolled in (a |an )?(bachelor|undergraduate)|"
    r"undergraduate (students? only|degree required)|"
    r"rising (sophomore|junior|senior)|"
    r"must be (a |an )?(current )?undergraduate)",
    re.IGNORECASE,
)

# Titles matching Gunraj's target functions get a HIGH priority flag.
PRIORITY_RE = re.compile(
    r"\b(commercial|strateg|market access|marketing|brand|business development|"
    r"business analytics|mba|leadership development|new product planning|"
    r"portfolio|competitive intelligence|operations|supply chain)\w*",
    re.IGNORECASE,
)

FAILURE_REALERT_HOURS = 24
FAIL_MIN_STREAK = 3   # consecutive failing runs before alerting (site-side)
FAIL_MIN_HOURS = 3    # and the failures must span at least this long

# Program-side problems alert IMMEDIATELY (no persistence gate): unexpected
# exceptions in our code, and schema changes where waiting cannot help
# because the adapter itself is now wrong for the site.
IMMEDIATE_FAIL_RE = re.compile(
    r"unexpected:|schema change|schema mismatch|site changed|responded without|"
    r"no adapter yet",
    re.IGNORECASE,
)


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


def fetch_jibe(cfg):
    """iCIMS/Jibe career portals (careers.medpace.com etc.): GET /api/jobs."""
    origin = cfg["origin"].rstrip("/")
    results = {}
    for term in cfg.get("search_terms", ["intern", "co-op", "MBA", "graduate"]):
        page = 1
        while page <= 20:
            api = f"{origin}/api/jobs?keyword={term}&limit=100&page={page}"
            resp = _request("GET", api)
            data = _json_or_fail(resp, api)
            jobs = data.get("jobs")
            total = data.get("totalCount", 0)
            if jobs is None:
                raise SourceError(f"{api} responded without jobs (schema change?)")
            for wrap in jobs:
                j = wrap.get("data") or {}
                key = j.get("slug") or j.get("req_id") or j.get("title")
                desc = " ".join(filter(None, [j.get("description"), j.get("qualifications")]))
                results[key] = {
                    "title": (j.get("title") or "").strip(),
                    "location": ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")])),
                    "url": f"{origin}/jobs/{j.get('slug')}?lang=en-us",
                    "posted_on": (j.get("posted_date") or "")[:10],
                    "description": html.unescape(re.sub(r"<[^>]+>", " ", desc)),
                }
            if page * 100 >= total or not jobs:
                break
            page += 1
    return list(results.values())


def fetch_ukg(cfg):
    """UKG Pro Recruiting boards (recruiting.ultipro.com)."""
    tenant, board = cfg["tenant"], cfg["board"]
    base = f"https://recruiting.ultipro.com/{tenant}/JobBoard/{board}"
    api = f"{base}/JobBoardView/LoadSearchResults"
    results = {}
    for term in cfg.get("search_terms", ["intern", "co-op", "MBA", "graduate"]):
        skip = 0
        while skip < 1000:
            payload = {
                "opportunitySearch": {
                    "Top": 50, "Skip": skip, "QueryString": term,
                    "OrderBy": [{"Value": "postedDateDesc",
                                 "PropertyName": "PostedDate", "Ascending": False}],
                    "Filters": [],
                },
                "matchCriteria": {"PreferredJobs": [], "Certifications": [],
                                  "Skills": [], "Languages": []},
            }
            resp = _request("POST", api, json=payload)
            data = _json_or_fail(resp, api)
            opps = data.get("opportunities")
            total = data.get("totalCount", 0)
            if opps is None:
                raise SourceError(f"{api} responded without opportunities (schema change?)")
            for o in opps:
                locs = []
                for l in o.get("Locations") or []:
                    addr = l.get("Address") or {}
                    state = addr.get("State")
                    country = addr.get("Country")
                    parts = [addr.get("City") or "",
                             (state.get("Code") if isinstance(state, dict) else state) or "",
                             (country.get("Code") if isinstance(country, dict) else country) or ""]
                    locs.append(", ".join(p for p in parts if p))
                results[o.get("Id")] = {
                    "title": (o.get("Title") or "").strip(),
                    "location": "; ".join(l for l in locs if l),
                    "url": f"{base}/OpportunityDetail?opportunityId={o.get('Id')}",
                    "posted_on": (o.get("PostedDate") or "")[:10],
                    "description": html.unescape(re.sub(r"<[^>]+>", " ", o.get("BriefDescription") or "")),
                }
            skip += 50
            if skip >= total or not opps:
                break
    return list(results.values())


def fetch_jobvite(cfg):
    """Jobvite hosted boards (jobs.jobvite.com/<slug>): server-rendered HTML."""
    slug = cfg["slug"]
    results = {}
    for term in cfg.get("search_terms", ["intern", "co-op", "MBA", "graduate"]):
        for page in range(1, 21):
            url = f"https://jobs.jobvite.com/{slug}/search?q={term}&p={page}"
            resp = _request("GET", url)
            t = resp.text
            if "jv-job-list" not in t and page == 1:
                raise SourceError(f"{url} returned no job list markup (site changed?)")
            rows = re.findall(
                r'href="(/' + re.escape(slug) + r'/job/([^"]+))"[^>]*>([^<]+)</a>'
                r'[\s\S]{0,400}?jv-job-list-location">\s*([^<]*?)\s*</td>', t)
            before = len(results)
            for href, jid, title, loc in rows:
                results[jid] = {
                    "title": html.unescape(title).strip(),
                    "location": html.unescape(loc).strip(),
                    "url": f"https://jobs.jobvite.com{href}",
                    "posted_on": "",
                }
            m = re.search(r"(\d+)\s*-\s*(\d+)\s*of\s*(\d+)", t)
            done = not rows or len(results) == before or (m and int(m.group(2)) >= int(m.group(3)))
            if done:
                break
    return list(results.values())


def fetch_attrax(cfg):
    """Attrax career sites (careers.abbvie.com): server-rendered tiles."""
    origin = cfg["origin"].rstrip("/")
    results = {}
    for term in cfg.get("search_terms", ["intern", "co-op", "MBA", "graduate"]):
        for page in range(1, 21):
            url = f"{origin}/en/jobs?q={term}&page={page}"
            resp = _request("GET", url)
            t = resp.text
            if "attrax-vacancy-tile" not in t:
                if page == 1:
                    raise SourceError(f"{url} returned no vacancy tiles (site changed?)")
                break
            anchors = list(re.finditer(
                r'<a[^>]*vacancy-tile__title[^>]*href="(/en/job/[^"]+)"[^>]*>([\s\S]{1,200}?)</a>'
                r'|<a[^>]*href="(/en/job/[^"]+)"[^>]*vacancy-tile__title[^>]*>([\s\S]{1,200}?)</a>', t))
            if not anchors:
                break
            before = len(results)
            for i, m in enumerate(anchors):
                href = m.group(1) or m.group(3)
                title = re.sub(r"<[^>]+>", " ", m.group(2) or m.group(4) or "")
                end = anchors[i + 1].start() if i + 1 < len(anchors) else len(t)
                seg = t[m.end():end]
                lm = re.search(r"Location\s*</p>[\s\S]{0,200}?item-value[^>]*>\s*([^<]*?)\s*</p>", seg)
                results[href] = {
                    "title": html.unescape(re.sub(r"\s+", " ", title)).strip(),
                    "location": html.unescape(lm.group(1)).strip() if lm else "",
                    "url": origin + href,
                    "posted_on": "",
                }
            if len(results) == before:
                break
    return list(results.values())


def fetch_jnj_careers(cfg):
    """careers.jnj.com: server-rendered cards; search is client-side only,
    so crawl every page and filter locally by title."""
    origin = "https://www.careers.jnj.com"
    results = {}
    page = 1
    while page <= 120:
        url = f"{origin}/en/jobs/" + (f"?page={page}" if page > 1 else "")
        resp = _request("GET", url)
        t = resp.text
        cards = re.findall(
            r'<a[^>]*href="(/en/jobs/(r-[^/"]+)/[^"]*)"[^>]*>([\s\S]{1,250}?)</a>'
            r'[\s\S]{0,800}?PagePromo-location[^>]*>([\s\S]{0,300}?)</address>', t)
        if not cards:
            if page == 1:
                raise SourceError(f"{url} returned no job cards (site changed?)")
            break
        new = 0
        for href, rid, rawtitle, rawloc in cards:
            if rid in results:
                continue
            new += 1
            title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", rawtitle))).strip()
            loc = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", rawloc))).strip()
            results[rid] = {"title": title, "location": loc,
                            "url": origin + href, "posted_on": ""}
        if new == 0:
            break
        page += 1
    return list(results.values())


def fetch_novo(cfg):
    """Novo Nordisk AEM careersearch JSON servlet, US-filtered."""
    api = ("https://www.novonordisk.com/bin/nncorp/careersearch"
           "?keyword=&country=United%20States&category=&locale=en")
    resp = _request("GET", api)
    data = (_json_or_fail(resp, api) or {}).get("data") or {}
    jobs = data.get("jobs")
    if jobs is None:
        raise SourceError(f"{api} responded without data.jobs (schema change?)")
    def _s(v):
        if isinstance(v, dict):
            v = v.get("label") or v.get("value") or v.get("name") or ""
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v if x)
        return str(v or "").strip()

    out = []
    for j in jobs:
        loc = _s(j.get("jobLocationLabel")) or ", ".join(
            p for p in (_s(j.get("jobCity")), _s(j.get("jobState")), _s(j.get("jobCountry"))) if p)
        out.append({
            "title": _s(j.get("jobTitle")),
            "location": loc,
            "url": f"https://www.novonordisk.com/careers/find-a-job/job-ad.html?id={_s(j.get('jobId'))}",
            "posted_on": "",
        })
    return out


ADAPTERS = {
    "workday": fetch_workday,
    "greenhouse": fetch_greenhouse,
    "phenom": fetch_phenom,
    "jibe": fetch_jibe,
    "ukg": fetch_ukg,
    "jobvite": fetch_jobvite,
    "attrax": fetch_attrax,
    "jnj": fetch_jnj_careers,
    "novo": fetch_novo,
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
    if UNDERGRAD_TITLE_RE.search(item["title"]):
        return False
    loc = item["location"] or ""
    if loc:
        if NON_US_RE.search(loc):
            return False
        if UNKNOWN_LOC_RE.search(loc):
            return True  # "2 Locations" etc. - cannot tell, keep
        if not US_RE.search(loc):
            return False  # US-only: identifiable non-US locations are dropped
    return True


def eligibility(item):
    """Returns (keep, note). Drops postings that are clearly undergrad-only;
    keeps everything else (with a note when eligibility cannot be verified)."""
    desc = item.get("description", "")
    if not desc and "_detail" in item:
        try:
            desc = fetch_workday_detail(item)
        except SourceError:
            return True, "eligibility unverified (detail page unreachable) - check posting"
    if not desc:
        return True, "eligibility not stated in feed - check posting"
    grad = bool(GRAD_RE.search(desc))
    undergrad = bool(UNDERGRAD_DESC_RE.search(desc))
    if undergrad and not grad:
        return False, ""
    if grad:
        return True, "graduate-eligible (verified in description)"
    return True, "grad eligibility not stated - check posting"


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

def _next_cron_slot(after):
    """Mirror of the schedule in monitor.yml (UTC):
    work hours (7am-5pm PDT = 14:00-00:59 UTC): :17 and :47 each hour;
    overnight: every 2 hours at :17."""
    cands = []
    for d in (0, 1):
        base = (after + timedelta(days=d)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        for h in [0] + list(range(14, 24)):
            for m in (17, 47):
                cands.append(base.replace(hour=h, minute=m))
        for h in (1, 3, 5, 7, 9, 11, 13):
            cands.append(base.replace(hour=h, minute=17))
    return min(c for c in cands if c > after)


def _gate_failures(state, failures, now):
    """Persistent-failure gate. A source is only alerted after it has failed
    FAIL_MIN_STREAK consecutive runs spanning at least FAIL_MIN_HOURS. One-off
    blips - e.g. Workday's weekly Saturday-early-morning maintenance window,
    which took down all 13 classic-pod Workday tenants at once on 2026-07-18 -
    recover silently. Real breakage still alerts the same day. Identical
    alerts remain rate-limited to one per FAILURE_REALERT_HOURS."""
    failing = state.setdefault("failing", {})
    failed_names = {f["company"] for f in failures}
    for name in [n for n in list(failing) if n not in failed_names]:
        del failing[name]  # recovered - clear the streak silently

    fresh = []
    for f_item in failures:
        rec = failing.get(f_item["company"]) or {"since": now.isoformat(), "streak": 0}
        rec["streak"] += 1
        rec["reason"] = f_item["reason"]
        failing[f_item["company"]] = rec
        hours_failing = (now - datetime.fromisoformat(rec["since"])).total_seconds() / 3600
        immediate = bool(IMMEDIATE_FAIL_RE.search(f_item["reason"]))
        if not immediate and (rec["streak"] < FAIL_MIN_STREAK or hours_failing < FAIL_MIN_HOURS):
            continue  # site-side blip - wait for persistence
        sig = hashlib.sha1(f"{f_item['company']}|{f_item['reason'][:80]}".encode()).hexdigest()
        last = state["failures"].get(sig)
        if last and now - datetime.fromisoformat(last) < timedelta(hours=FAILURE_REALERT_HOURS):
            continue
        state["failures"][sig] = now.isoformat()
        fresh.append(f_item)
    return fresh


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
                keep, note = eligibility(item)
                if not keep:
                    state["seen"][key] = {
                        "company": name, "title": item["title"],
                        "location": item["location"], "first_seen": now.isoformat(),
                        "skipped": "undergrad-only",
                    }
                    continue
                role = {
                    "company": name,
                    "title": item["title"],
                    "location": item["location"],
                    "posted_on": item.get("posted_on", ""),
                    "url": item["url"],
                    "eligibility": note,
                    "priority": "HIGH" if PRIORITY_RE.search(item["title"]) else "",
                    "first_seen": now.isoformat(),
                }
                new_roles.append(role)
                state["seen"][key] = {
                    "company": name, "title": item["title"],
                    "location": item["location"], "first_seen": now.isoformat(),
                    "url": item["url"],
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

    fresh_failures = _gate_failures(state, failures, now)

    if new_roles:
        send_webhook({"type": "new_roles", "items": new_roles})
        print(f"{len(new_roles)} new role(s) reported")
    if fresh_failures:
        send_webhook({"type": "failures", "items": fresh_failures})
        print(f"{len(fresh_failures)} source failure(s) reported")
    if not new_roles and not fresh_failures:
        print("Nothing new; all configured sources checked or already-alerted failures.")

    # ---- Weekly Saturday tasks (~7am Pacific): dead-link sweep + digest ----
    if now.weekday() == 5 and now.hour == 14:
        expired = []
        checked = 0
        for key, meta in state["seen"].items():
            url = meta.get("url")
            if not url or meta.get("expired") or meta.get("skipped"):
                continue
            if checked >= 200:
                break
            checked += 1
            try:
                resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
                if resp.status_code in (404, 410):
                    meta["expired"] = True
                    expired.append({"company": meta.get("company", ""),
                                    "title": meta.get("title", ""), "url": url})
            except requests.RequestException:
                pass  # network hiccup: never mark expired on uncertainty
            time.sleep(0.3)
        if expired:
            send_webhook({"type": "expired", "items": expired})
            print(f"{len(expired)} expired posting(s) reported")
        send_webhook({"type": "digest"})
        print("weekly digest requested")

    # Heartbeat: silent status stamp in the sheet (no email). Lets Gunraj
    # confirm the monitor is alive even when there is nothing new.
    # Timestamps are computed at send time (end of run). The "next" time is
    # the next cron SLOT (see monitor.yml): every 30 min at :17/:47 during
    # 7am-5pm Pacific work hours, every 2 hours overnight. GitHub starts
    # scheduled runs late under load, so the sheet labels it "or later".
    hb_now = datetime.now(timezone.utc)
    send_webhook({"type": "heartbeat",
                  "ran_at": hb_now.isoformat(),
                  "next_at": _next_cron_slot(hb_now).isoformat(),
                  "new_count": len(new_roles)})

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
