"""Public job inventories and optional LinkedIn guest discovery; standard library only.

No account, cookies, paid API, browser impersonation, or challenge bypass is used.
LinkedIn's guest interface is undocumented and may refuse access. Such failures
are reported, never interpreted as a healthy empty result. ATS APIs are the
independent fallback. A last_success checkpoint advances only after full coverage.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
import json
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class SourceError(RuntimeError):
    """A bounded public-source failure; circuit means stop further guest requests."""

    def __init__(self, message, *, circuit=False):
        super().__init__(message)
        self.circuit = circuit


_LAST_REQUEST = {}
_LINKEDIN_INTERVAL = 2.0
_MAX_BYTES = 32 * 1024 * 1024
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_BLOCK = {"address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "ol", "p", "section", "table", "tr", "ul"}


class _Node:
    def __init__(self, tag="", attrs=None):
        # HTMLParser represents boolean/bare attributes as None, including the
        # bare class attribute found in real LinkedIn guest job descriptions.
        self.tag, self.attrs, self.children = tag, {key: value or "" for key, value in attrs or []}, []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def text(self, blocks=False):
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag not in {"script", "style"}:
                value = child.text(blocks=blocks)
                parts.append("\n" + value + "\n" if blocks and child.tag in _BLOCK else value)
        joined = " ".join(parts)
        if blocks:
            # Keep requirement/preference bullets on distinct lines for ranking.
            return "\n".join(line for line in (" ".join(part.split()) for part in joined.splitlines()) if line)
        return " ".join(joined.split())

    def first(self, *, cls=None, tag=None):
        return next((n for n in self.walk()
                     if (cls is None or cls in n.attrs.get("class", "").split())
                     and (tag is None or n.tag == tag)), None)


class _HTML(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]
        self.feed(value)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_Node(tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _text(value):
    # Greenhouse intentionally HTML-entity-encodes its HTML content.
    return _HTML(unescape(str(value or ""))).root.text(blocks=True)


def _node_text(node):
    return node.text() if node is not None else ""


def _iso(now):
    return now.astimezone(timezone.utc).isoformat()


def _parse_time(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (TypeError, ValueError):
        return None


def _clean_url(value):
    parts = urlsplit(str(value or ""))
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SourceError("Job record lacks a valid public HTTP(S) URL")
    # Do not strip query parameters from ATS URLs: many encode the actual job ID.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _evidence(value):
    """Small diagnostic snippet without storing entire remote documents in state."""
    return re.sub(r"\s+", " ", str(value))[:150]


def _request(url):
    hostname = urlsplit(url).hostname or ""
    linkedin = hostname.endswith("linkedin.com")
    interval = _LINKEDIN_INTERVAL if linkedin else 0.25
    for attempt in range(2):
        delay = interval - (time.monotonic() - _LAST_REQUEST.get(hostname, 0))
        if delay > 0:
            time.sleep(delay)
        _LAST_REQUEST[hostname] = time.monotonic()
        request = Request(url, headers={
            "User-Agent": "OpportunityMonitor/1.0 (personal job alerts)",
            "Accept": "application/json,text/html;q=0.9",
        })
        try:
            with urlopen(request, timeout=20) as response:
                final = response.geturl().lower()
                if linkedin and any(x in final for x in ("/authwall", "/login", "/checkpoint", "/signup")):
                    raise SourceError("LinkedIn redirected to sign-in or challenge; guest access unavailable", circuit=True)
                raw = response.read(_MAX_BYTES + 1)
                if len(raw) > _MAX_BYTES:
                    raise SourceError("Response exceeded 32 MiB safety bound; inventory was not fully collected")
                encoding = response.headers.get_content_charset() or "utf-8"
                value = raw.decode(encoding, errors="replace")
                if linkedin and re.search(r'(?:<title[^>]*>[^<]*(?:sign in|security verification)|class=["\'][^"\']*authwall|id=["\'](?:captcha|challenge)|/checkpoint/challenge)', value, re.I):
                    raise SourceError("LinkedIn returned a login/challenge page; guest access unavailable", circuit=True)
                return value
        except HTTPError as exc:
            if exc.code in {401, 403, 429, 999}:
                raise SourceError(f"HTTP {exc.code} from {hostname}; requests stopped (no bypass attempted)", circuit=linkedin) from exc
            if attempt == 0 and exc.code in {500, 502, 503, 504}:
                time.sleep(1)
                continue
            raise SourceError(f"HTTP {exc.code} from {hostname}") from exc
        except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise SourceError(f"Network failure from {hostname}: {_evidence(exc)}") from exc


def _json(url):
    payload = _request(url)
    try:
        return json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise SourceError(f"Expected JSON from {urlsplit(url).hostname}; got {_evidence(payload)!r}") from exc


def _parse_linkedin_search(payload):
    root = _HTML(payload).root
    cards = [n for n in root.walk() if "job-search-card" in n.attrs.get("class", "").split()]
    jobs, invalid = [], 0
    for card in cards:
        title = _node_text(card.first(cls="base-search-card__title"))
        company = _node_text(card.first(cls="base-search-card__subtitle"))
        link = card.first(cls="base-card__full-link")
        match = re.search(r"jobPosting:(\d+)", card.attrs.get("data-entity-urn", ""))
        if not match and link:
            match = re.search(r"(?:-|/)(\d+)(?:[/?#]|$)", link.attrs.get("href", ""))
        if not title or not company or not match:
            invalid += 1
            continue
        identifier = match.group(1)
        stamp = card.first(tag="time")
        jobs.append({
            "source": "linkedin", "id": identifier, "company": company,
            "title": title, "location": _node_text(card.first(cls="job-search-card__location")),
            "url": f"https://www.linkedin.com/jobs/view/{identifier}",
            "description": "", "posted_on": stamp.attrs.get("datetime", "") if stamp else "",
            "salary": _node_text(card.first(cls="job-search-card__salary-info")),
        })
    if not cards and payload.strip() and not re.search(r"no matching jobs|no results|jobs-search-no-results", payload, re.I):
        raise SourceError(f"LinkedIn search schema not recognized; response starts {_evidence(payload)!r}")
    return jobs, invalid, len(cards)


def _parse_linkedin_detail(payload, job):
    root = _HTML(payload).root
    description = root.first(cls="show-more-less-html__markup")
    title = root.first(cls="top-card-layout__title")
    if description is None or not description.text():
        raise SourceError(f"LinkedIn detail lacks a readable job description; response starts {_evidence(payload)!r}")
    result = dict(job)
    result["description"] = description.text(blocks=True)
    if title and title.text():
        result["title"] = title.text()
    company = root.first(cls="topcard__org-name-link")
    if company and company.text():
        result["company"] = company.text()
    location = root.first(cls="topcard__flavor--bullet")
    if location and location.text():
        result["location"] = location.text()
    criteria = {}
    for item in root.walk():
        if "description__job-criteria-item" not in item.attrs.get("class", "").split():
            continue
        heading = _node_text(item.first(cls="description__job-criteria-subheader"))
        value = _node_text(item.first(cls="description__job-criteria-text"))
        if heading and value:
            criteria[heading] = value
    result["criteria"] = criteria
    if criteria.get("Industries"):
        result["industry"] = criteria["Industries"]
    salary = root.first(cls="compensation__salary-range")
    if salary and salary.text():
        result["salary"] = salary.text()
    result["detail_status"] = "ok"
    return result


def fetch_detail(job):
    """Return a new job dict. LinkedIn descriptions are fetched only on demand."""
    if job.get("source") != "linkedin":
        if not job.get("description"):
            raise SourceError(f"{job.get('source', 'unknown')} listing has no description")
        return dict(job, detail_status="ok")
    identifier = str(job.get("id", ""))
    if not re.fullmatch(r"\d+", identifier):
        raise SourceError("Invalid LinkedIn job ID")
    payload = _request("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/" + identifier)
    return _parse_linkedin_detail(payload, job)


def _health(previous, now, count=0, *, error="", truncated=False, success=False, **extra):
    return {
        "status": "ok" if success and not error and not truncated else ("degraded" if count else "failed"),
        "count": count, "checked_at": _iso(now),
        "last_success": _iso(now) if success and not error and not truncated else previous.get("last_success"),
        "error": error, "truncated": bool(truncated), **extra,
    }


def _window(settings, previous, now):
    maximum = max(1, int(settings.get("max_window_hours", 168)))
    requested = max(1, int(settings.get("lookback_hours", 72)))
    last = _parse_time(previous.get("last_success"))
    if not last:
        requested = max(requested, int(settings.get("bootstrap_hours", requested)))
    elapsed = max(0.0, (now - last).total_seconds() / 3600) if last else 0
    requested = max(requested, int(elapsed + 2 + 0.999)) if last else requested
    hours = min(requested, maximum)
    return hours, maximum, bool(last and elapsed > maximum), now - timedelta(hours=hours)


def _collect_linkedin(settings, previous_health, now):
    global _LINKEDIN_INTERVAL
    _LINKEDIN_INTERVAL = max(1.0, float(settings.get("interval_seconds", 2)))
    pages_limit = max(1, min(100, int(settings.get("max_pages", 3))))
    page_size = max(1, min(100, int(settings.get("page_size", 25))))
    queries = list(dict.fromkeys(str(q).strip() for q in settings.get("queries", []) if str(q).strip()))
    jobs, health, unique, circuit = [], {}, set(), ""
    for query in queries:
        key = "linkedin:" + query
        previous = previous_health.get(key, {})
        hours, maximum, gap, coverage_start = _window(settings, previous, now)
        query_jobs, query_ids, errors, pages, offset = [], set(), [], 0, 0
        truncated, exhausted = False, False
        if circuit:
            errors.append("Skipped after LinkedIn circuit opened: " + circuit)
        else:
            for page in range(pages_limit):
                url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urlencode({
                    "keywords": query, "location": "United States", "f_TPR": "r" + str(hours * 3600),
                    "sortBy": "DD", "start": offset, "count": page_size,
                })
                try:
                    payload = _request(url)
                    batch, invalid, raw_count = _parse_linkedin_search(payload)
                    pages += 1
                    if invalid:
                        errors.append(f"Page {page + 1}: {invalid} malformed job cards skipped")
                    if not raw_count:
                        if page == 0 and not payload.strip():
                            errors.append("Empty first response: no-results could not be confirmed")
                        else:
                            exhausted = True
                        break
                    novel = [job for job in batch if job["id"] not in query_ids]
                    if batch and not novel:
                        errors.append("Pagination repeated a previous page; remaining coverage unknown")
                        truncated = True
                        break
                    for job in novel:
                        query_ids.add(job["id"])
                        query_jobs.append(job)
                    # Guest endpoint can return 10 cards despite count=25. Advancing
                    # by the requested size would silently skip intervening jobs.
                    offset += raw_count
                except SourceError as exc:
                    errors.append(str(exc))
                    if exc.circuit:
                        circuit = str(exc)
                    break
            else:
                truncated = True
                errors.append(f"Pagination cap ({pages_limit} pages) reached; additional jobs may exist")
        if gap:
            errors.append(f"Outage exceeds {maximum}-hour maximum search window; older coverage cannot be guaranteed")
        for job in query_jobs:
            identity = job["id"]
            if identity not in unique:
                unique.add(identity)
                jobs.append(dict(job, source_key="linkedin", discovery_queries=[query]))
            else:
                for saved in jobs:
                    if saved["id"] == identity:
                        saved["discovery_queries"].append(query)
                        break
        health[key] = _health(previous, now, len(query_jobs), error="; ".join(errors),
                              truncated=truncated, success=exhausted, pages=pages,
                              lookback_hours=hours, max_window_hours=maximum,
                              coverage_start=_iso(coverage_start), backfill_gap=gap)
    aggregate_errors = [f"{q}: {health['linkedin:' + q]['error']}" for q in queries if health['linkedin:' + q]['error']]
    complete = bool(queries) and all(health["linkedin:" + q]["status"] == "ok" for q in queries)
    if not queries:
        aggregate_errors.append("LinkedIn enabled but no search queries configured")
    health["linkedin"] = _health(previous_health.get("linkedin", {}), now, len(jobs),
                                  error=" | ".join(aggregate_errors),
                                  truncated=any(item["truncated"] for item in health.values()),
                                  success=complete, circuit_open=bool(circuit),
                                  completed_queries=sum(health["linkedin:" + q]["status"] == "ok" for q in queries),
                                  total_queries=len(queries))
    return jobs, health


def _base_board_job(board, raw, *, identifier, title, location, url, description, posted_on="", salary=""):
    if not identifier or not isinstance(title, str) or not title.strip():
        raise SourceError("Job record lacks id/title")
    if not isinstance(description, str) or not description.strip():
        raise SourceError(f"Job {identifier} lacks description")
    return {
        "source": board["type"], "source_key": board["type"] + ":" + board["token"],
        "board_token": board["token"], "id": str(identifier), "company": board["name"],
        "title": title.strip(), "location": str(location or ""), "url": _clean_url(url),
        "description": _text(description), "posted_on": str(posted_on or ""),
        "industry": str(board.get("industry", "")), "salary": salary,
        "detail_status": "ok",
    }


def _board_record(board, raw):
    if not isinstance(raw, dict):
        raise SourceError("Job record is not an object")
    kind = board["type"]
    if kind == "greenhouse":
        # updated_at is not an original posting date. Do not present it as one.
        return _base_board_job(board, raw, identifier=raw.get("id"), title=raw.get("title"),
                               location=(raw.get("location") or {}).get("name"), url=raw.get("absolute_url"),
                               description=raw.get("content"), posted_on=raw.get("first_published", ""))
    if kind == "lever":
        categories = raw.get("categories") or {}
        extra = " ".join(str(item.get("text", "")) + " " + str(item.get("content", ""))
                         for item in raw.get("lists", []) if isinstance(item, dict))
        description = " ".join(str(raw.get(k) or "") for k in ("descriptionPlain", "description", "additionalPlain", "additional")) + " " + extra
        salary_data = raw.get("salaryRange") or {}
        salary = " ".join(str(salary_data[k]) for k in ("currency", "min", "max", "interval") if k in salary_data)
        return _base_board_job(board, raw, identifier=raw.get("id"), title=raw.get("text"),
                               location=categories.get("location"), url=raw.get("hostedUrl") or raw.get("applyUrl"),
                               description=description, salary=salary)
    if kind == "ashby":
        compensation = raw.get("compensation") or {}
        salary = compensation.get("scrapeableCompensationSalarySummary", "") if isinstance(compensation, dict) else ""
        return _base_board_job(board, raw, identifier=raw.get("id"), title=raw.get("title"),
                               location=raw.get("location"), url=raw.get("jobUrl") or raw.get("applyUrl"),
                               description=raw.get("descriptionPlain") or raw.get("descriptionHtml"),
                               posted_on=raw.get("publishedAt"), salary=salary)
    raise SourceError("Unsupported board type: " + str(kind))


def _collect_board(board, previous, now):
    jobs, errors, seen = [], [], set()
    truncated, complete, pages = False, False, 0
    kind, token = board.get("type"), str(board.get("token", ""))
    try:
        if not board.get("name") or not token:
            raise SourceError("Board configuration requires name and token")
        escaped = quote(token, safe="")
        for page in range(100 if kind == "lever" else 1):
            if kind == "greenhouse":
                url = f"https://boards-api.greenhouse.io/v1/boards/{escaped}/jobs?content=true"
            elif kind == "lever":
                url = f"https://api.lever.co/v0/postings/{escaped}?mode=json&limit=100&skip={page * 100}"
            elif kind == "ashby":
                url = f"https://api.ashbyhq.com/posting-api/job-board/{escaped}?includeCompensation=true"
            else:
                raise SourceError("Unsupported board type: " + str(kind))
            data = _json(url)
            pages += 1
            records = data if kind == "lever" else data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(records, list):
                raise SourceError(f"{kind} expected jobs array; received {_evidence(data)!r}")
            valid_this_page, skipped = 0, 0
            for raw in records:
                if kind == "ashby" and isinstance(raw, dict) and raw.get("isListed") is False:
                    continue
                try:
                    job = _board_record(board, raw)
                    if job["id"] not in seen:
                        seen.add(job["id"])
                        jobs.append(job)
                        valid_this_page += 1
                except (SourceError, TypeError, AttributeError, ValueError):
                    skipped += 1
            if skipped:
                errors.append(f"Page {page + 1}: {skipped} malformed/incomplete job records skipped")
            if kind == "greenhouse":
                total = (data.get("meta") or {}).get("total")
                if total is not None and int(total) != len(records):
                    truncated = True
                    errors.append(f"Inventory count mismatch: received {len(records)}, advertised {total}")
            if kind != "lever" or len(records) < 100:
                complete = True
                break
            if not valid_this_page:
                truncated = True
                errors.append("Pagination made no progress; remaining inventory unknown")
                break
        else:
            truncated = True
            errors.append("Inventory exceeded 100-page bound; remaining jobs were not collected")
    except (SourceError, ValueError, TypeError, AttributeError) as exc:
        errors.append(str(exc))
    return jobs, _health(previous, now, len(jobs), error="; ".join(errors),
                          truncated=truncated, success=complete, pages=pages,
                          inventory_scope="All publicly listed jobs on this configured board")


def collect(config, previous_health, now):
    """Collect public jobs and explicit coverage health; preserve partial successes.

    config contains linkedin settings and boards. Searches cover a rolling 72-hour
    window by default, expanded since each query's successful checkpoint up to
    max_window_hours (default 168). Capped/failed queries never advance checkpoints.
    Employer boards are full inventories, independent of posting timestamps.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    jobs, health = [], {}
    settings = config.get("linkedin") or {}
    if settings.get("enabled", False):
        found, statuses = _collect_linkedin(settings, previous_health or {}, now)
        jobs.extend(found)
        health.update(statuses)
    for board in config.get("boards", []):
        key = str(board.get("type", "unknown")) + ":" + str(board.get("token", ""))
        found, status = _collect_board(board, (previous_health or {}).get(key, {}), now)
        jobs.extend(found)
        health[key] = status
    return jobs, health
