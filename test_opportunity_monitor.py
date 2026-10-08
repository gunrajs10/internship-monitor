import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import opportunity_monitor as m


NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)
GOOD = {"include": True, "score": 90, "priority": "HIGH", "track": "program",
        "reasons": ["Commercial strategy"], "cautions": [], "california": True,
        "eligibility": "Check graduation window", "salary_text": "$40/hour"}


def job(number=1, company="Example Biotech"):
    return {"source": "greenhouse", "id": str(number), "company": company,
            "title": "Commercial Strategy MBA Intern " + str(number),
            "location": "San Francisco, California", "url": f"https://example.org/jobs/{number}",
            "description": "Biotechnology MBA commercial strategy internship", "posted_on": "2026-09-26"}


class QueueTests(unittest.TestCase):
    def test_location_policy_prunes_old_queue_before_detail_budget(self):
        state = m.new_state()
        ca, other = job(1), job(2)
        other.update(location="Boston, MA",description="")
        m.ingest(state,[ca,other],{},NOW)
        key=m.identity(other)
        state["pending"][key]["detail_pending"]=True
        with patch.object(m,"fetch_detail") as fetch:
            m.enrich_pending(state,{"geography":{"california_focus":True}},NOW,fetch)
        fetch.assert_not_called()
        self.assertNotIn(key,state["pending"])
        self.assertEqual(len(state["pending"]),1)

    def test_california_first_sorting_while_preserving_fit_within_location(self):
        ca=job(1); ca["assessment"]={"score":60,"california":True,"location_priority":0}
        other=job(2); other["assessment"]={"score":100,"california":False,"location_priority":1}
        self.assertEqual(sorted([other,ca],key=m.rank_key),[ca,other])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def populated(self, count=1):
        state = m.new_state()
        with patch.object(m, "assess", return_value=GOOD):
            m.ingest(state, [job(i) for i in range(count)], {}, NOW)
        return state

    def test_delivery_failure_retains_every_candidate_across_restart(self):
        state = self.populated(3)
        def broken(_):
            raise m.DeliveryError("mail unavailable")
        with self.assertRaises(m.DeliveryError):
            m.send_pending(state, {}, self.path, NOW, broken)
        restored = m.load_state(self.path)
        self.assertEqual(len(restored["pending"]), 3)
        self.assertEqual(restored["delivered"], {})
        self.assertIn("inflight", restored)

    def test_ambiguous_batch_replays_exact_payload_before_new_high_priority_job(self):
        state = self.populated(2)
        attempted = []
        def broken(payload):
            attempted.append(payload)
            raise m.DeliveryError("mail acknowledgement timed out")
        with self.assertRaises(m.DeliveryError):
            m.send_pending(state, {"delivery": {"batch_size": 2}}, self.path, NOW, broken)
        state = m.load_state(self.path)
        with patch.object(m, "assess", return_value={**GOOD, "score": 100}):
            m.ingest(state, [job(3)], {}, NOW)
        with self.assertRaises(m.DeliveryError):
            m.send_pending(state, {"delivery": {"batch_size": 2}}, self.path, NOW, broken)
        self.assertEqual(attempted[0], attempted[1])

    def test_delivery_limits_defer_without_losing_or_starving_company(self):
        state = self.populated(7)
        config = {"delivery": {"batch_size": 2, "max_batches_per_run": 2}}
        sent = []
        def sender(payload):
            sent.extend(payload["items"])
            return True
        self.assertEqual(m.send_pending(state, config, self.path, NOW, sender), 4)
        self.assertEqual(len(state["pending"]), 3)
        self.assertEqual(m.send_pending(state, config, self.path, NOW, sender), 3)
        self.assertEqual(len(sent), 7)
        self.assertEqual(len(state["delivered"]), 7)

    def test_already_delivered_cannot_consume_notification_slots(self):
        state = self.populated(4)
        m.send_pending(state, {}, self.path, NOW, lambda p: True)
        with patch.object(m, "assess", return_value=GOOD):
            added = m.ingest(state, [job(i) for i in range(6)], {}, NOW)
        self.assertEqual(added, 2)
        self.assertEqual(len(state["pending"]), 2)

    def test_corrupt_state_fails_closed_instead_of_resetting(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            m.load_state(self.path)
        self.assertEqual(self.path.read_text(), "{broken")

    def test_missing_acknowledgement_never_marks_delivered(self):
        state = self.populated()
        with self.assertRaises(m.DeliveryError):
            m.send_pending(state, {}, self.path, NOW, lambda payload: None)
        self.assertEqual(len(m.load_state(self.path)["pending"]), 1)

    def test_detail_budget_preserves_unprocessed_candidates(self):
        state = self.populated(4)
        for j in state["pending"].values():
            j["detail_pending"] = True
            j["description"] = ""
        with patch.object(m, "assess", return_value=GOOD):
            attempts = m.enrich_pending(state, {"detail_limit": 1}, NOW,
                lambda j: {**j, "description": "Verified public posting content"})
        self.assertEqual(attempts, 1)
        self.assertEqual(sum(j["detail_pending"] for j in state["pending"].values()), 3)

    def test_source_failure_does_not_erase_pending(self):
        state = self.populated(2)
        with patch.object(m, "assess", return_value=GOOD):
            m.ingest(state, [], {}, NOW)
        self.assertEqual(len(state["pending"]), 2)

    def test_identical_cross_source_sighting_not_two_alerts(self):
        state = self.populated()
        alternate = job(0)
        alternate.update(source="linkedin", id="different")
        with patch.object(m, "assess", return_value=GOOD):
            self.assertEqual(m.ingest(state, [alternate], {}, NOW), 0)

    def test_distinct_requisitions_with_same_title_are_not_lost(self):
        state = self.populated()
        alternate = job(0)
        alternate["id"] = "new-requisition"
        with patch.object(m, "assess", return_value=GOOD):
            self.assertEqual(m.ingest(state, [alternate], {}, NOW), 1)

    def test_official_detail_wins_over_earlier_linkedin_card(self):
        state = m.new_state()
        card = job(0)
        card.update(source="linkedin", description="")
        official = job(0)
        with patch.object(m, "assess", return_value=GOOD), patch.object(m, "needs_detail", return_value=True):
            m.ingest(state, [card, official], {}, NOW)
        self.assertEqual(len(state["pending"]), 1)
        saved = next(iter(state["pending"].values()))
        self.assertEqual(saved["source"], "greenhouse")
        self.assertFalse(saved["detail_pending"])

    def test_detail_rate_block_stops_remaining_linkedin_calls(self):
        from opportunity_sources import SourceError
        state = self.populated(3)
        for j in state["pending"].values():
            j.update(source="linkedin", detail_pending=True, description="")
        calls = []
        def blocked(j):
            calls.append(j)
            raise SourceError("429", circuit=True)
        with patch.object(m, "assess", return_value=GOOD):
            m.enrich_pending(state, {}, NOW, blocked)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(j["detail_pending"] for j in state["pending"].values()))

    def test_preview_escapes_untrusted_titles(self):
        state = self.populated()
        next(iter(state["pending"].values()))["title"] = "<script>alert(1)</script>"
        page = m.preview_document(state, NOW)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_existing_public_data_refresh_does_not_reset_first_seen(self):
        state = self.populated()
        first = next(iter(state["pending"].values()))["first_seen"]
        with patch.object(m, "assess", return_value=GOOD):
            m.ingest(state, [job(0)], {}, datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(next(iter(state["pending"].values()))["first_seen"], first)


class WebhookTests(unittest.TestCase):
    class Response:
        status = 200
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return self.body

    def test_apps_script_replay_keeps_event_content_with_fresh_transport_url(self):
        payload = {"type": "new_roles", "event_id": "expected", "items": [{"title": "California strategy", "company": "Example"}]}
        endpoint = "https://script.google.com/macros/s/example/exec?existing=keep"
        with patch.object(m.urllib.request, "urlopen", return_value=self.Response(b'{"ok":true,"event_id":"expected"}')) as send, patch.object(m.time, "time_ns", side_effect=[10, 11]):
            self.assertTrue(m.post_webhook(payload, endpoint))
            self.assertTrue(m.post_webhook(payload, endpoint))
        requests = [call.args[0] for call in send.call_args_list]
        self.assertNotEqual(requests[0].full_url, requests[1].full_url)
        for request in requests:
            self.assertEqual(json.loads(request.data), payload)
            self.assertIn("existing=keep", request.full_url)
            self.assertEqual(request.get_header("Cache-control"), "no-cache")

    def test_other_delivery_endpoints_keep_exact_signed_url(self):
        endpoint = "https://example.org/hook?signature=keep"
        with patch.object(m.urllib.request, "urlopen", return_value=self.Response(b'{"ok":true}')) as send:
            self.assertTrue(m.post_webhook({}, endpoint))
        self.assertEqual(send.call_args.args[0].full_url, endpoint)

    def test_lost_job_response_uses_read_only_completed_receipt(self):
        payload = {"type": "new_roles", "event_id": "opportunity-recovery", "items": [{"title": "MBA Intern"}]}
        verified = self.Response(b'{"ok":true,"event_id":"opportunity-recovery","receipt_verified":true}')
        with patch.object(m.urllib.request, "urlopen", side_effect=[self.Response(b'<html>Error</html>'), verified]) as send:
            self.assertTrue(m.post_webhook(payload, "https://example.org/hook"))
        requests = [json.loads(call.args[0].data) for call in send.call_args_list]
        self.assertEqual(requests[0], payload)
        self.assertEqual(requests[1]["type"], "receipt_check")
        self.assertNotIn("items", requests[1])
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(requests[1]["payload_hash"], m.hashlib.sha256(canonical.encode()).hexdigest())

    def test_receipt_fallback_rejects_legacy_success_or_uncertain_response(self):
        payload = {"type": "new_roles", "event_id": "opportunity-recovery", "items": [{"title": "MBA Intern"}]}
        for reply in [b'{"ok":true}', b'{"ok":false,"error":"delivery_uncertain"}',
                      b'{"ok":true,"event_id":"other","receipt_verified":true}']:
            with self.subTest(reply=reply), patch.object(m.urllib.request, "urlopen", side_effect=[self.Response(b'<html>Error</html>'), self.Response(reply)]):
                with self.assertRaises(m.DeliveryError):
                    m.post_webhook(payload, "https://example.org/hook")

    def test_html_error_with_http_200_is_not_success(self):
        with patch.object(m.urllib.request, "urlopen", return_value=self.Response(b"<html>Script error</html>")):
            with self.assertRaises(m.DeliveryError):
                m.post_webhook({}, "https://example.org/hook")

    def test_explicit_error_overrides_success_flag(self):
        with patch.object(m.urllib.request, "urlopen", return_value=self.Response(b'{"ok":true,"error":"mail failed"}')):
            with self.assertRaises(m.DeliveryError):
                m.post_webhook({}, "https://example.org/hook")

    def test_json_success_is_accepted(self):
        with patch.object(m.urllib.request, "urlopen", return_value=self.Response(b'{"ok":true}')):
            self.assertTrue(m.post_webhook({}, "https://example.org/hook"))

    def test_receipt_for_another_batch_is_rejected(self):
        with patch.object(m.urllib.request, "urlopen", return_value=self.Response(b'{"ok":true,"event_id":"other"}')):
            with self.assertRaises(m.DeliveryError):
                m.post_webhook({"event_id": "expected"}, "https://example.org/hook")

    def test_matching_duplicate_receipt_is_accepted(self):
        with patch.object(m.urllib.request, "urlopen", return_value=self.Response(b'{"ok":true,"event_id":"expected","duplicate":true}')):
            self.assertTrue(m.post_webhook({"event_id": "expected"}, "https://example.org/hook"))


if __name__ == "__main__":
    unittest.main()
