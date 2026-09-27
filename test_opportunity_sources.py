import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import opportunity_sources as sources


NOW = datetime(2026, 9, 26, 22, tzinfo=timezone.utc)


def card(identifier="123", title="Commercial Strategy Intern", company="A &amp; B Bio"):
    return f'''<li><div class="base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:{identifier}">
    <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/intern-{identifier}?tracking=yes">Go</a>
    <h3 class="base-search-card__title">{title}</h3>
    <h4 class="base-search-card__subtitle"><a>{company}</a></h4>
    <span class="job-search-card__location">San Francisco, CA</span>
    <time datetime="2026-09-25">Yesterday</time></div></li>'''


def gh_job(identifier=5):
    return {"id": identifier, "title": "MBA Commercial Intern", "absolute_url": "https://example.org/careers?gh_jid=" + str(identifier),
            "location": {"name": "California"}, "content": "&lt;p&gt;MBA candidates, commercial strategy.&lt;/p&gt;",
            "updated_at": "2026-09-25T10:00:00Z"}


class SourceTests(unittest.TestCase):
    def test_live_style_card_entities_and_canonical_url(self):
        jobs, invalid, count = sources._parse_linkedin_search("<!DOCTYPE html>" + card())
        self.assertEqual((invalid, count), (0, 1))
        self.assertEqual(jobs[0]["company"], "A & B Bio")
        self.assertEqual(jobs[0]["url"], "https://www.linkedin.com/jobs/view/123")
        self.assertEqual(jobs[0]["posted_on"], "2026-09-25")

    def test_broken_schema_is_not_empty_success(self):
        with self.assertRaisesRegex(sources.SourceError, "schema not recognized"):
            sources._parse_linkedin_search("<html><h1>Something changed</h1></html>")

    def test_bad_cards_preserve_valid_records(self):
        jobs, invalid, count = sources._parse_linkedin_search(card() + card("124", company=""))
        self.assertEqual((len(jobs), invalid, count), (1, 1, 2))

    def test_detail_retains_requirements_and_industry(self):
        payload = '''<span class></span><h1 class="top-card-layout__title">MBA Intern</h1>
        <div class="show-more-less-html__markup"><p>Must be enrolled in an MBA.</p><ul><li class>0–2 years experience.</li><li>Preferred: consulting experience.</li></ul></div>
        <li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader">Industries</h3><span class="description__job-criteria-text">Biotechnology Research</span></li>'''
        original = {"source": "linkedin", "id": "123", "description": ""}
        result = sources._parse_linkedin_detail(payload, original)
        self.assertIn("0–2 years", result["description"])
        self.assertIn("experience.\nPreferred:", result["description"])
        self.assertEqual(result["industry"], "Biotechnology Research")
        self.assertEqual(original["description"], "")

    def test_missing_detail_raises(self):
        with self.assertRaisesRegex(sources.SourceError, "lacks a readable job description"):
            sources._parse_linkedin_detail("<h1>Sorry</h1>", {"id": "1"})

    def test_detail_rejects_invalid_identifier_before_request(self):
        with patch.object(sources, "_request") as request:
            with self.assertRaises(sources.SourceError):
                sources.fetch_detail({"source": "linkedin", "id": "../login"})
            request.assert_not_called()

    def test_guest_actual_page_size_prevents_skipped_jobs(self):
        urls = []
        def request(url):
            urls.append(url)
            return [card("1") + card("2"), card("3"), ""][len(urls) - 1]
        config = {"linkedin": {"enabled": True, "queries": ["biotech intern"], "max_pages": 4, "page_size": 25}}
        with patch.object(sources, "_request", side_effect=request):
            jobs, health = sources.collect(config, {}, NOW)
        self.assertEqual([parse_qs(urlsplit(u).query)["start"][0] for u in urls], ["0", "2", "3"])
        self.assertEqual(len(jobs), 3)
        self.assertEqual(health["linkedin"]["status"], "ok")
        self.assertEqual(health["linkedin:biotech intern"]["last_success"], NOW.isoformat())

    def test_circuit_preserves_partial_jobs_skips_queries_keeps_boards(self):
        config = {"linkedin": {"enabled": True, "queries": ["a", "b"]},
                  "boards": [{"name": "Bio", "type": "greenhouse", "token": "bio"}]}
        def request(url):
            if "greenhouse" in url:
                return '{"jobs": [], "meta": {"total": 0}}'
            if "start=0" in url:
                return card()
            raise sources.SourceError("HTTP 429", circuit=True)
        with patch.object(sources, "_request", side_effect=request) as req:
            jobs, health = sources.collect(config, {}, NOW)
        self.assertEqual(req.call_count, 3)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(health["linkedin"]["circuit_open"])
        self.assertEqual(health["linkedin:a"]["status"], "degraded")
        self.assertIn("Skipped", health["linkedin:b"]["error"])
        self.assertEqual(health["greenhouse:bio"]["status"], "ok")

    def test_truncation_retains_checkpoint_and_reports_coverage(self):
        old = (NOW - timedelta(days=4)).isoformat()
        with patch.object(sources, "_request", return_value=card()):
            _, health = sources.collect({"linkedin": {"enabled": True, "queries": ["a"], "max_pages": 1}},
                                        {"linkedin:a": {"last_success": old}}, NOW)
        state = health["linkedin:a"]
        self.assertTrue(state["truncated"])
        self.assertEqual(state["last_success"], old)
        self.assertEqual(state["lookback_hours"], 98)
        self.assertEqual(state["status"], "degraded")

    def test_long_outage_backfill_gap_is_explicit(self):
        with patch.object(sources, "_request", return_value="No matching jobs found"):
            _, health = sources.collect({"linkedin": {"enabled": True, "queries": ["a"]}},
                                        {"linkedin:a": {"last_success": (NOW - timedelta(days=10)).isoformat()}}, NOW)
        self.assertTrue(health["linkedin:a"]["backfill_gap"])
        self.assertEqual(health["linkedin:a"]["lookback_hours"], 168)
        self.assertNotEqual(health["linkedin:a"]["status"], "ok")

    def test_bootstrap_window_used_only_without_success(self):
        settings = {"lookback_hours": 6, "bootstrap_hours": 24}
        self.assertEqual(sources._window(settings, {}, NOW)[0], 24)
        self.assertEqual(sources._window(settings, {"last_success": (NOW - timedelta(hours=2)).isoformat()}, NOW)[0], 6)

    def test_html_requirement_boundaries_preserved(self):
        result = sources._text("<p>Qualifications</p><ul><li>5 years required</li><li>MBA preferred</li></ul>")
        self.assertEqual(result.splitlines(), ["Qualifications", "5 years required", "MBA preferred"])

    def test_empty_first_page_is_unconfirmed(self):
        with patch.object(sources, "_request", return_value="  "):
            _, health = sources.collect({"linkedin": {"enabled": True, "queries": ["a"]}}, {}, NOW)
        self.assertNotEqual(health["linkedin"]["status"], "ok")
        self.assertIn("no-results could not be confirmed", health["linkedin"]["error"])

    def test_duplicate_jobs_merge_query_evidence(self):
        with patch.object(sources, "_request", side_effect=[card(), "", card(), ""]):
            jobs, health = sources.collect({"linkedin": {"enabled": True, "queries": ["a", "b"]}}, {}, NOW)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["discovery_queries"], ["a", "b"])
        self.assertEqual(health["linkedin"]["status"], "ok")

    def test_greenhouse_decodes_content_preserves_query_no_fake_posting_date(self):
        board = {"name": "Bio", "type": "greenhouse", "token": "bio", "industry": "Biotechnology"}
        with patch.object(sources, "_json", return_value={"jobs": [gh_job()], "meta": {"total": 1}}):
            jobs, health = sources.collect({"boards": [board]}, {}, NOW)
        self.assertEqual(jobs[0]["description"], "MBA candidates, commercial strategy.")
        self.assertIn("?gh_jid=5", jobs[0]["url"])
        self.assertEqual(jobs[0]["posted_on"], "")
        self.assertEqual(health["greenhouse:bio"]["status"], "ok")

    def test_greenhouse_count_mismatch_is_truncated(self):
        with patch.object(sources, "_json", return_value={"jobs": [gh_job()], "meta": {"total": 3}}):
            jobs, health = sources.collect({"boards": [{"name": "Bio", "type": "greenhouse", "token": "bio"}]}, {}, NOW)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(health["greenhouse:bio"]["truncated"])
        self.assertIsNone(health["greenhouse:bio"]["last_success"])

    def test_lever_pagination_and_partial_network_failure(self):
        batch = [{"id": str(i), "text": "Analyst", "categories": {"location": "US"},
                  "descriptionPlain": "Commercial role", "lists": [{"text": "Requirements", "content": "<li>MBA</li>"}],
                  "hostedUrl": "https://jobs.lever.co/bio/" + str(i)} for i in range(100)]
        with patch.object(sources, "_json", side_effect=[batch, sources.SourceError("HTTP 503")]) as request:
            jobs, health = sources.collect({"boards": [{"name": "Bio", "type": "lever", "token": "bio"}]}, {}, NOW)
        self.assertEqual(len(jobs), 100)
        self.assertIn("skip=100", request.call_args.args[0])
        self.assertIn("Requirements\nMBA", jobs[0]["description"])
        self.assertEqual(health["lever:bio"]["status"], "degraded")
        self.assertIn("503", health["lever:bio"]["error"])

    def test_ashby_compensation_and_hidden_listing(self):
        row = {"id": "j1", "title": "Associate", "location": "US", "jobUrl": "https://jobs.ashbyhq.com/bio/j1",
               "descriptionPlain": "Healthcare strategy", "publishedAt": "2026-09-25T00:00:00Z",
               "compensation": {"scrapeableCompensationSalarySummary": "$90,000–$120,000"}}
        with patch.object(sources, "_json", return_value={"jobs": [row, dict(row, id="j2", isListed=False)]}):
            jobs, health = sources.collect({"boards": [{"name": "Bio", "type": "ashby", "token": "bio"}]}, {}, NOW)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["salary"], "$90,000–$120,000")
        self.assertEqual(health["ashby:bio"]["status"], "ok")

    def test_bad_board_schema_and_records_are_visible(self):
        config = {"boards": [{"name": "Bio", "type": "greenhouse", "token": "bio"}]}
        with patch.object(sources, "_json", return_value={"error": "unknown board"}):
            _, health = sources.collect(config, {}, NOW)
        self.assertEqual(health["greenhouse:bio"]["status"], "failed")
        with patch.object(sources, "_json", return_value={"jobs": [gh_job(), {"id": 3}], "meta": {"total": 2}}):
            jobs, health = sources.collect(config, {}, NOW)
        self.assertEqual(len(jobs), 1)
        self.assertIn("malformed/incomplete", health["greenhouse:bio"]["error"])

    def test_rate_limit_is_not_retried(self):
        error = HTTPError("https://www.linkedin.com/x", 429, "Too many", {}, None)
        with patch.object(sources, "urlopen", side_effect=error) as request, patch.object(sources.time, "sleep"):
            with self.assertRaises(sources.SourceError) as caught:
                sources._request("https://www.linkedin.com/x")
        self.assertTrue(caught.exception.circuit)
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
