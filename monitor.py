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

# Titles that count as target roles: internships AND rotational /
# early-career / MBA development programs (CRDP-class postings).
#
# The second block is the real program vocabulary, taken from how these
# programs are actually branded rather than from what they "should" be
# called (checked against live careers pages, July 2026):
#   Commercial Leadership Development Program (CLDP) - Takeda, J&J, BMS, AZ
#   Commercial Leadership Program (CLP)              - Amgen
#   Rotational Development Program (RDP)             - Biogen, Pfizer
#   Commercial Rotational Development Program (CRDP) - Genentech
#   GPS Emerging Leaders Program                     - BMS
#   MBA Leadership Development Program               - Blueprint / Sanofi
#   Early Talent Program                             - Novartis
#   Future Leaders                                   - GSK, AstraZeneca
# "development program"/"rotational" already covered CLDP and RDP; the
# named-cohort brands ("Emerging Leaders", "Future Leaders", "Early
# Talent") and the bare acronyms did not, and were silently invisible.
TITLE_RE = re.compile(
    r"\b(intern(ship)?s?|co[\s\-]?op|mba\b|summer associate|"
    r"graduate (program|scheme|associate|intern)|"
    r"rotation(al)?|development (program|rotation)|"
    r"leadership (development )?program|early[\s\-]career|"
    r"new grad(uate)?s?|campus hire|"
    r"emerging leaders?|future leaders?|early talent|"
    r"associate program|trainee|fellowship|"
    r"crdp|cldp|mldp|ldp|rdp|clp)\b",
    re.IGNORECASE,
)

# ---- Track 2: ordinary early-stage roles that carry no program branding --
# A first-year-MBA-appropriate commercial role is often just "Associate
# Product Manager, Oncology" - no keyword above will ever fire on it. This
# track requires all three conditions: a target function, a junior
# seniority marker, and no senior marker. Deliberately tight; it is a
# precision filter, not a catch-all, because the alternative is burying the
# real programs under hundreds of Director-level postings.
# Bare "analytics", "insights", "operations", "portfolio" and "launch" were
# in this list and were the reason "Associate Scientist, HCP Analytics,
# Product Biochemistry" was reported as a business role. Every ambiguous
# term is now required to appear in a business-qualified form.
FUNCTION_RE = re.compile(
    r"\b(commercial|strateg\w*|market access|marketing|brand\w*|"
    r"business (development|analytics|operations|strategy|intelligence|planning)|"
    r"(commercial|market|customer|consumer|competitive|sales|payer) "
    r"(analytics|insights?|intelligence|research|excellence|operations|strategy)|"
    r"new product planning|competitive intelligence|"
    r"product manage\w+|product marketing|product strategy|"
    r"pricing|reimbursement|payer|forecast\w*|market development|"
    r"go[\s\-]to[\s\-]market|launch (strategy|excellence|planning)|"
    r"portfolio (strategy|management|planning)|"
    r"supply chain|corporate development|alliance management|"
    r"business partner\w*|patient (access|services|advocacy)|"
    r"medical affairs|health economics|heor|"
    r"finance|financial planning)\b",
    re.IGNORECASE,
)

# Field-sales exclusion for the early-stage track. Gunraj is not looking for
# sales. Roughly half of everything this track surfaced was sales-facing:
# "Oncology Sales Specialist", "Primary Care Pharmaceutical Sales Specialist",
# "Sales Associate, Trauma", "Customer Team Leader (District Sales Manager)",
# "Territory Account Manager Roles - Join Vertex's Sales Talent Community".
# The bare word "sales" was also a matching FUNCTION term, which is what let
# them in; that has been removed above and this is the backstop.
#
# Deliberately NOT excluded, because they are wanted:
#   "commercial" on its own - Commercial Strategy / Analytics / Operations /
#   Excellence all stay. Only genuinely sales-facing wording is cut.
#   marketing, brand, market access, payer, pricing, forecasting, insights.
# Applied to the early-stage track ONLY - a rotational program that happens
# to include a sales rotation (Amgen CLP, Genentech CRDP) is still reported,
# since those are the target.
SALES_EXCLUDE_RE = re.compile(
    r"\bsales\b|\bselling\b|\brepresentative\b|\breps?\b|"
    r"\bterritory\b|\bdistrict manager\b|district sales|"
    r"account (executive|manager|director|specialist)|key account|"
    r"business development (rep|representative)|\bbdr\b|\bsdr\b|"
    r"inside sales|field force|customer team leader",
    re.IGNORECASE,
)

# Hard exclusion for the early-stage track: bench, clinical, engineering,
# manufacturing and IT roles are not business roles no matter how
# business-sounding a fragment of the title is. Applied ONLY to the
# early-stage track - a named rotational program is still reported even if
# it is technical, since those are explicitly wanted.
SCIENCE_EXCLUDE_RE = re.compile(
    r"\b(scientist|scientific|research (associate|scientist|fellow)|"
    r"biochem\w*|chemist\w*|chemistry|biolog\w*|microbiolog\w*|immunolog\w*|"
    r"toxicolog\w*|pharmacolog\w*|patholog\w*|histolog\w*|genomic\w*|"
    r"engineer\w*|technician|technologist|"
    r"laboratory|lab\b|assay|bioprocess|upstream|downstream|cell culture|"
    r"purification|formulation|analytical chemistry|cmc\b|"
    r"quality (control|assurance)|\bqc\b|\bqa\b|validation|"
    r"manufactur\w*|production|biostatistic\w*|bioinformatic\w*|"
    r"statistical programm\w*|pharmacovigilance|clinical (trial|research|"
    r"operations|data|development|scientist)|medical writ\w*|"
    r"software|developer|devops|data engineer\w*|\bit\b|"
    r"device|hardware|mechanical|electrical|automation|"
    r"nurse|physician|pharmacist|toxicology|safety)\b",
    re.IGNORECASE,
)

# Loosened per Gunraj: "Manager" and "Senior Associate/Analyst" are now
# accepted, since both are realistic first post-MBA titles in pharma. The
# hard ceiling stays at Director and above.
JUNIOR_RE = re.compile(
    r"\b(associate|analyst|coordinator|specialist|assistant|manager|"
    r"entry[\s\-]level)\b",
    re.IGNORECASE,
)
# The hard ceiling. "Senior" and "Manager" were removed from this list when
# the dial was loosened; Director and above will not hire a first-year MBA
# into a first post-MBA role, so those still go. "Associate Director" is
# still dropped - the Director wins over the Associate.
SENIOR_RE = re.compile(
    r"\b(director|principal|staff|lead|officer|partner|"
    r"head of|global head|vice president|vp|chief|executive|distinguished)\b",
    re.IGNORECASE,
)
# Loosening "senior" wholesale let "Senior Manager" and "Sr. Manager" in -
# typically five-plus years in pharma, well above a first post-MBA role.
# "Senior Associate" / "Senior Analyst" / "Senior Specialist" are the titles
# actually wanted, so seniority is only disqualifying when it modifies
# manager.
SENIOR_MANAGER_RE = re.compile(
    r"\b(senior|sr\.?)\s+(\w+\s+){0,2}managers?\b", re.IGNORECASE)


def is_senior(title):
    return bool(SENIOR_RE.search(title) or SENIOR_MANAGER_RE.search(title))

# ---- Track 3: description-level rescue for generically-titled programs --
# The failure mode this exists for: a rotational program posted under a
# title with no early-career signal at all. Only the description reveals
# it. Applied to a bounded subset (see worth_deep_scan) because reading
# every description across ~10,000 postings is not affordable per run.
DESC_PROGRAM_RE = re.compile(
    r"(rotational (program|assignment|development)|rotations? (through|across|in)|"
    r"leadership development program|commercial leadership program|"
    r"(recent |current )?mba (graduate|student|candidate)s?|"
    r"mba (is )?(required|preferred|strongly preferred)|"
    r"program (participants|associates|cohort)|two[\s\-]year rotation)",
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
# Two additions after finding real US roles being silently discarded:
#   1. A bare "US" token. AstraZeneca posts "US - Baltimore - MD" and even
#      plain "US"; none of "united states", "usa" or "u.s." match those, so
#      the posting was dropped as non-US.
#   2. State codes after a dash or en-dash, not just a comma. "US - Miami -
#      FL" never matched the old `,\s*(FL)` form.
# Between them, 22 of 40 distinct AstraZeneca US locations (56 postings in
# a single sample) were being thrown away. NON_US_RE is still evaluated
# first, so "US - London - UK" is still correctly rejected.
US_RE = re.compile(
    r"(^|[\s,\-–])US([\s,\-–]|$)"
    r"|[,\-–]\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|"
    r"ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|"
    r"TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b"
    r"|\b(united states|usa|u\.s\.a?\.?|remote)\b"
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
    r"philadelphia|pittsburgh|seattle|bothell|new brunswick|raritan|"
    r"bridgewater|west chester|holly springs|hillsboro|clarksville)\b",
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

DEFAULT_SEARCH_TERMS = ["intern", "co-op", "MBA", "graduate", "rotation",
                        "development program", "early career", "new grad"]

# Phrases a search-results page uses to say "this specific query matched
# nothing," as opposed to the page itself being broken/blocked. Seen on
# Attrax career sites (careers.abbvie.com): a valid, fully-rendered page
# for a query with zero matches, distinct from a real markup change or a
# bot-block page. Matching on this (rather than only on the presence of
# result tiles) is what keeps a single zero-result search term - which is
# a completely normal outcome - from being mistaken for "the site broke."
NO_RESULTS_RE = re.compile(r"returned no results|no results (were )?found", re.IGNORECASE)

FAILURE_REALERT_HOURS = 24

# Track 3 reads job descriptions to catch rotational programs whose titles
# give nothing away. Most sources ship the description in the feed, which is
# free; Workday-style sources need one extra HTTP call per posting, so those
# are capped per company. Raising this trades runtime for recall.
DEEP_SCAN_FETCH_CAP = 25
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

# Webhook (Google Apps Script) POSTs get their own, longer timeout and their
# own retry budget, separate from TIMEOUT/RETRIES above (which are tuned for
# company job-site fetches). Apps Script cold-starts and heavy Sheet writes
# can legitimately take longer than a single site fetch, and that is not a
# program bug - it must never crash the run. See send_webhook() and run().
WEBHOOK_TIMEOUT = 45
WEBHOOK_RETRIES = 2

# A healthy webhook replies with the plain text "ok". Apps Script answers a
# thrown exception with HTTP 200 and an HTML error page, so the body has to
# be inspected - see send_webhook().
WEBHOOK_ERROR_RE = re.compile(
    r"<!doctype html|<title>\s*Error|ReferenceError|TypeError|"
    r"Exception|Script function not found",
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
    for term in cfg.get("search_terms", DEFAULT_SEARCH_TERMS):
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
    for term in cfg.get("search_terms", DEFAULT_SEARCH_TERMS):
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
    for term in cfg.get("search_terms", DEFAULT_SEARCH_TERMS):
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
    for term in cfg.get("search_terms", DEFAULT_SEARCH_TERMS):
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
    for term in cfg.get("search_terms", DEFAULT_SEARCH_TERMS):
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
    """Attrax career sites (careers.abbvie.com): server-rendered tiles.

    A page with zero results for a given search term is a fully valid,
    normal response (Attrax renders "We are sorry but your search has
    returned no results.") - it must NOT be treated the same as a real
    markup change. Before this fix, any single term with zero matches
    (e.g. "new grad" - a term added when search terms were broadened for
    rotational-program coverage) raised SourceError immediately and threw
    away every result already found under the other search terms in the
    same run, silently starving AbbVie of new postings for days.
    """
    origin = cfg["origin"].rstrip("/")
    results = {}
    for term in cfg.get("search_terms", DEFAULT_SEARCH_TERMS):
        for page in range(1, 21):
            url = f"{origin}/en/jobs?q={term}&page={page}"
            resp = _request("GET", url)
            t = resp.text
            if "attrax-vacancy-tile" not in t:
                if NO_RESULTS_RE.search(t):
                    break  # legitimate zero matches for this term - not a break
                if page == 1:
                    raise SourceError(
                        f"{url} returned no vacancy tiles and no 'no results' "
                        f"message (site changed?)"
                    )
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


def fetch_lonza(cfg):
    """Lonza's own careers board (www.lonza.com/careers/job-search).

    Not a hosted ATS - a server-rendered Sitecore search page. Two traps
    that make the obvious approaches fail silently rather than loudly:

      1. Paging is driven by a POST form (fields: q, pg, lid), NOT by query
         string. GET ?page=2 and GET ?q=intern are accepted with HTTP 200
         and simply ignored, always returning page 1. A naive GET crawl
         would therefore re-read the same 25 rows forever and report
         "checked, nothing new" - exactly the silent-failure mode this
         monitor exists to prevent. The `lid` field turns out to be
         optional, so only q and pg are sent.
      2. The site's own `q` search is full-text over the whole job
         description, not the title. q=intern returns 191 hits (most of
         them unrelated roles that merely mention interns) while still
         missing rotational / MBA / development programs whose text never
         uses the word. So `q` is left empty and every page is crawled.

    The board is small (~640 postings, 26 pages), deterministically
    ordered newest-first, so a full crawl is cheap and lets TITLE_RE do the
    filtering - the same approach as fetch_jnj_careers.

      3. Lonza sits behind a bot manager far stricter than any other source
         on the watchlist, and it fingerprints the TLS handshake, not just
         the headers. Verified on the runner: plain `requests` gets a flat
         HTTP 403, and it stays 403 even with a Session that sends the full
         browser header set (document Accept, Sec-Fetch-*, sec-ch-ua,
         same-origin Referer/Origin) and primes itself with a GET first.
         The same requests succeed from a real browser, and from other
         datacenter clients, so the discriminator is python-urllib3's TLS
         ClientHello. curl_cffi replays Chrome's actual handshake, which is
         what gets through. It is used for this source only; every other
         adapter still runs on plain `requests`.

    If Lonza refuses anyway, the adapter raises SourceError rather than
    returning an empty list, so a block shows up as a loud failure alert
    carrying the career_page link - never as a silent "nothing new."
    """
    url = cfg.get("url", "https://www.lonza.com/careers/job-search")
    origin = "https://www.lonza.com"
    row_re = re.compile(
        r'<a[^>]*class="search-result[^"]*"[^>]*href="(/jobs/(R\d+))"'
        r'[\s\S]{0,400}?search-result-title">([\s\S]*?)</div>'
        r'[\s\S]{0,200}?search-result-content">([\s\S]*?)</div>'
    )
    doc_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": '"Chromium";v="138", "Google Chrome";v="138", "Not)A;Brand";v="8"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }

    try:
        from curl_cffi import requests as impersonating
        sess = impersonating.Session(impersonate="chrome")
        transport = "curl_cffi/chrome"
    except ImportError:
        # Degrade rather than crash the whole run; this will very likely be
        # refused with a 403, which surfaces as a normal loud source failure.
        sess = requests.Session()
        transport = "requests (curl_cffi unavailable - expect HTTP 403)"
    sess.headers.update(doc_headers)

    def _get(method, **kwargs):
        last = None
        for attempt in range(RETRIES + 1):
            try:
                r = sess.request(method, url, timeout=TIMEOUT, **kwargs)
                if r.status_code == 200:
                    return r
                last = f"HTTP {r.status_code}"
            except Exception as exc:
                last = type(exc).__name__
            time.sleep(2 * (attempt + 1))
        raise SourceError(
            f"{url} failed after {RETRIES + 1} attempts via {transport}: {last}"
        )

    # Prime the session: a first-party GET, exactly as a browser would do
    # before submitting the paging form.
    _get("GET", headers={"Sec-Fetch-Site": "none"})

    results = {}
    for pg in range(1, 61):
        resp = _get("POST", data={"q": "", "pg": str(pg)},
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Origin": origin, "Referer": url})
        t = resp.text
        rows = row_re.findall(t)
        if not rows:
            if pg == 1:
                raise SourceError(
                    f"{url} returned no search-result rows on page 1 (site changed?)"
                )
            break  # walked off the end of the listing - normal
        before = len(results)
        for href, rid, rawtitle, rawloc in rows:
            results[rid] = {
                "title": html.unescape(
                    re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", rawtitle))).strip(),
                "location": html.unescape(
                    re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", rawloc))).strip(),
                "url": origin + href,
                "posted_on": "",
            }
        if len(results) == before:
            break  # page repeated itself: paging stopped advancing
    return list(results.values())


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
    "lonza": fetch_lonza,
}


# ---------------------------------------------------------------------------
# Filtering, dedupe, state
# ---------------------------------------------------------------------------

def normalize(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def dedupe_key(company, title, location):
    raw = f"{normalize(company)}|{normalize(title)}|{normalize(location)}"
    return hashlib.sha1(raw.encode()).hexdigest()


def location_ok(item):
    """US-only rule, shared by every match track."""
    loc = item.get("location") or ""
    if not loc:
        return True
    if NON_US_RE.search(loc):
        return False
    if UNKNOWN_LOC_RE.search(loc):
        return True  # "2 Locations" etc. - cannot tell, keep
    return bool(US_RE.search(loc))


def title_match(item):
    """Which title track a posting hits, or None.

    'program'     - explicit internship / rotational / named-cohort branding
    'early-stage' - no program branding, but a junior-level role in a target
                    function (Associate Product Manager, Commercial Analyst)
    """
    title = item.get("title") or ""
    if UNDERGRAD_TITLE_RE.search(title):
        return None
    if TITLE_RE.search(title):
        return "program"
    if (FUNCTION_RE.search(title) and JUNIOR_RE.search(title)
            and not is_senior(title)
            and not SCIENCE_EXCLUDE_RE.search(title)
            and not SALES_EXCLUDE_RE.search(title)):
        return "early-stage"
    return None


def worth_deep_scan(item):
    """Should this title-miss have its description read (track 3)?

    Bounded on purpose. Reading every description across ~10,000 postings a
    run is not affordable, so the scan is limited to postings that at least
    land in a target function - which is where a generically-titled
    rotational program would actually sit. A posting whose description is
    already in the feed costs nothing extra; only Workday-style sources
    need a separate fetch, and those are capped by the caller.
    """
    title = item.get("title") or ""
    if UNDERGRAD_TITLE_RE.search(title):
        return False
    return bool(FUNCTION_RE.search(title))


def is_candidate(item):
    """Kept for the audit path: does this posting match on title alone?"""
    return title_match(item) is not None and location_ok(item)


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
    """POST to the Apps Script webhook.

    Root cause of the 2026-07-23 crash (run #109): this used to be a single
    unretried POST at the same 30s TIMEOUT used for company-site fetches.
    Apps Script occasionally takes longer than that to respond (cold start,
    or a heavy Sheet write/formatting/email pass) - that one slow response
    raised an uncaught requests.exceptions.ReadTimeout, which killed the
    entire process before state could be saved. That is a program bug, not
    a "the website is down" situation, so the fix is retry + a realistic
    timeout, not the site-failure persistence gate.

    Retries transient failures with backoff at a longer, Apps-Script-sized
    timeout. Raises SourceError only after every retry is exhausted -
    callers decide whether that's fatal. new_roles/failures (the actual
    alert path) are allowed to let that propagate, because if the webhook
    is still unreachable after 3 attempts spanning ~15s of backoff, that is
    genuinely worth surfacing - via the workflow's exit-code-1, which fires
    GitHub's own native "workflow failed" email to the repo owner
    independent of this webhook, plus the crash-safety-net curl step in
    monitor.yml. heartbeat/expired/digest are cosmetic/supplementary and are
    always called from a try/except in run() so they can never crash a run.
    """
    if not WEBHOOK_URL:
        print("WARN: WEBHOOK_URL not set; printing payload instead")
        print(json.dumps(payload, indent=2))
        return
    last_err = None
    for attempt in range(WEBHOOK_RETRIES + 1):
        try:
            resp = requests.post(WEBHOOK_URL, json=payload, timeout=WEBHOOK_TIMEOUT)
            body = (resp.text or "").strip()
            # HTTP 200 is NOT sufficient. An Apps Script web app that throws
            # still answers 200 and serves an HTML error page, so checking
            # only the status code reports success while the Sheet write or
            # the email silently did not happen. That is exactly how a broken
            # `n is not defined` in the webhook went unnoticed: rows appeared,
            # no email was ever sent, and every run logged "HTTP 200".
            if resp.status_code == 200 and not WEBHOOK_ERROR_RE.search(body):
                print(f"webhook -> HTTP {resp.status_code} {body[:40]!r}")
                return
            last_err = (f"HTTP {resp.status_code}, body: "
                        f"{re.sub(r'<[^>]+>', ' ', body)[:200].strip()!r}")
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < WEBHOOK_RETRIES:
            time.sleep(5 * (attempt + 1))
    raise SourceError(
        f"webhook POST (type={payload.get('type')}) failed after "
        f"{WEBHOOK_RETRIES + 1} attempts: {last_err}"
    )


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

            # ---- Tracks 1 and 2: title-based matches ----
            kinds = {}          # id(posting) -> match kind
            for p in postings:
                kind = title_match(p)
                if kind and location_ok(p):
                    kinds[id(p)] = kind

            # ---- Track 3: description rescue for generically-titled
            # programs. Runs on postings in a target function that pass the
            # US rule and are NOT senior-level. The seniority gate matters:
            # a rotational program is never posted as "Director, Commercial
            # Strategy", but plenty of Director postings say "MBA required"
            # and would otherwise flood the sheet with roles that will not
            # hire a first-year MBA. Descriptions already in the feed are
            # free; the ones needing a per-posting fetch are capped so a
            # single company cannot blow up the run. A posting already
            # matched as early-stage is UPGRADED here when its description
            # proves it is actually a program.
            fetched = 0
            for p in postings:
                if kinds.get(id(p)) == "program":
                    continue  # title already proved it
                if not worth_deep_scan(p) or not location_ok(p):
                    continue
                if is_senior(p.get("title") or ""):
                    continue
                desc = p.get("description", "")
                if not desc and "_detail" in p:
                    if fetched >= DEEP_SCAN_FETCH_CAP:
                        continue
                    fetched += 1
                    try:
                        desc = fetch_workday_detail(p)
                        p["description"] = desc
                    except SourceError:
                        continue
                if desc and DESC_PROGRAM_RE.search(desc):
                    kinds[id(p)] = "program-desc"

            candidates = [(p, kinds[id(p)]) for p in postings if id(p) in kinds]

            n_prog = sum(1 for _, k in candidates if k != "early-stage")
            n_early = sum(1 for _, k in candidates if k == "early-stage")
            audit_rows.append((
                name, "OK",
                f"{len(postings)} postings, {n_prog} program-type, {n_early} early-stage"))
            if audit:
                continue
            for item, kind in candidates:
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
                label = {
                    "program": "rotational/internship program",
                    "program-desc": "PROGRAM FOUND IN DESCRIPTION (title gives no hint)",
                    "early-stage": "early-stage commercial role (not a named program)",
                }[kind]
                role = {
                    "company": name,
                    "title": item["title"],
                    "location": item["location"],
                    "posted_on": item.get("posted_on", ""),
                    "url": item["url"],
                    "eligibility": f"{label} - {note}",
                    # Routes the row to a sheet tab: named programs and
                    # description-rescued programs go to the programs tab,
                    # ordinary junior commercial roles to the other.
                    "track": "early-stage" if kind == "early-stage" else "program",
                    # A program found only in the description is always HIGH -
                    # that is precisely the class of posting that went missed
                    # before. Everything else earns HIGH on function match.
                    "priority": ("HIGH" if kind == "program-desc"
                                 or PRIORITY_RE.search(item["title"]) else ""),
                    "first_seen": now.isoformat(),
                }
                new_roles.append(role)
                state["seen"][key] = {
                    "company": name, "title": item["title"],
                    "location": item["location"], "first_seen": now.isoformat(),
                    "url": item["url"], "match": kind,
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

    # ---- Alert-critical sends. Allowed to raise after WEBHOOK_RETRIES
    # attempts: if these are still failing at that point the notification
    # pipe itself is broken, which is worth surfacing via the workflow's
    # own failure path (see send_webhook docstring).
    if new_roles:
        send_webhook({"type": "new_roles", "items": new_roles})
        print(f"{len(new_roles)} new role(s) reported")
    if fresh_failures:
        send_webhook({"type": "failures", "items": fresh_failures})
        print(f"{len(fresh_failures)} source failure(s) reported")
    if not new_roles and not fresh_failures:
        print("Nothing new; all configured sources checked or already-alerted failures.")

    # ---- Weekly Saturday tasks (~7am Pacific): dead-link sweep + digest ----
    # Best-effort: supplementary, must never crash the run or block state
    # from saving.
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
            try:
                send_webhook({"type": "expired", "items": expired})
                print(f"{len(expired)} expired posting(s) reported")
            except SourceError as exc:
                print(f"WARN: expired-postings webhook failed (non-fatal): {exc}")
        try:
            send_webhook({"type": "digest"})
            print("weekly digest requested")
        except SourceError as exc:
            print(f"WARN: digest webhook failed (non-fatal): {exc}")

    # State is saved here, BEFORE the heartbeat send. This is the direct fix
    # for run #109: previously save_state() ran after the heartbeat webhook,
    # so when that single decorative ping timed out, the entire run's state
    # (new postings found, dedupe keys, failure-gate bookkeeping) was lost,
    # not just the heartbeat itself.
    save_state(state)

    # ---- Silent heartbeat: status stamp in the sheet, no email. Best-effort
    # and fully isolated - it can never crash the run or affect state.
    hb_now = datetime.now(timezone.utc)
    try:
        send_webhook({"type": "heartbeat",
                      "ran_at": hb_now.isoformat(),
                      "next_at": _next_cron_slot(hb_now).isoformat(),
                      "new_count": len(new_roles)})
    except SourceError as exc:
        print(f"WARN: heartbeat webhook failed (non-fatal): {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", action="store_true", help="test all sources, alert nothing")
    args = parser.parse_args()
    try:
        run(audit=args.audit)
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
