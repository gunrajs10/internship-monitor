import unittest

from opportunity_rank import assess, needs_detail


class OpportunityRankTests(unittest.TestCase):
    def job(self, **changes):
        data = {"company": "Example Therapeutics", "title": "Commercial Strategy Intern",
                "location": "San Francisco, CA", "description": "MBA students. Biotechnology strategy internship.",
                "source": "test", "id": "1", "url": "https://example.test/1"}
        data.update(changes)
        return data

    def test_california_is_highlighted_but_boston_is_included(self):
        ca = assess(self.job(), {})
        ma = assess(self.job(location="Boston, MA"), {})
        self.assertTrue(ca["include"] and ma["include"])
        self.assertTrue(ca["california"])
        self.assertFalse(ma["california"])
        self.assertEqual(ca["score"], ma["score"])

    def test_remote_alone_is_not_claimed_to_be_us(self):
        result = assess(self.job(location="Remote"), {})
        self.assertTrue(result["include"])
        self.assertEqual(result["eligibility"], "Check eligibility")
        self.assertTrue(any("US work location" in c for c in result["cautions"]))

    def test_remote_foreign_excluded_and_remote_us_allowed(self):
        self.assertFalse(assess(self.job(location="Remote, Canada"), {})["include"])
        self.assertTrue(assess(self.job(location="Remote - United States"), {})["include"])
        self.assertTrue(assess(self.job(location="Remote", description="Must reside in the United States. MBA biotech internship."), {})["include"])

    def test_city_names_do_not_overrule_explicit_foreign_country(self):
        result = assess(self.job(location="London, United Kingdom"), {})
        self.assertFalse(result["include"])
        self.assertFalse(assess(self.job(location="Vancouver, CA"), {})["include"])
        self.assertTrue(assess(self.job(location="London, OH"), {})["include"])

    def test_all_three_tracks_and_science_internship_remain(self):
        for title, track in [("Research Intern", "program"), ("Finance Leadership Development Program", "program"), ("Clinical Research Associate", "early-stage")]:
            result = assess(self.job(title=title), {})
            self.assertTrue(result["include"], title)
            self.assertEqual(result["track"], track)

    def test_mba_preferred_does_not_create_rotational_program(self):
        result = assess(self.job(title="Finance Analyst", description="Healthcare finance. MBA preferred."), {})
        self.assertEqual(result["track"], "early-stage")

    def test_commercial_priority_over_science_without_dropping_science(self):
        commercial = assess(self.job(), {})
        science = assess(self.job(title="Research Intern"), {})
        self.assertTrue(science["include"])
        self.assertGreater(commercial["score"], science["score"])

    def test_seniority_cannot_be_rescued_by_pay_or_mba(self):
        result = assess(self.job(title="Senior Director, Commercial Strategy", salary="$250,000 per year"), {})
        self.assertFalse(result["include"])

    def test_preferred_experience_does_not_exclude(self):
        self.assertTrue(assess(self.job(description="MBA internship. Five years preferred. 5 years of experience preferred."), {})["include"])
        self.assertFalse(assess(self.job(description="Minimum 5 years of commercial experience required."), {})["include"])

    def test_html_requirements_have_separate_mandatory_and_preferred_clauses(self):
        result = assess(self.job(description="<ul><li>MBA preferred</li><li>Minimum 8 years of experience required</li></ul>"), {})
        self.assertFalse(result["include"])

    def test_undergraduate_degree_is_not_undergraduate_enrollment(self):
        self.assertTrue(assess(self.job(description="Bachelor's degree required; MBA preferred."), {})["include"])
        self.assertFalse(assess(self.job(description="Must be currently enrolled in a bachelor's program."), {})["include"])
        self.assertTrue(assess(self.job(description="Must be pursuing a bachelor's or master's degree."), {})["include"])

    def test_required_clinical_credentials_differ_from_preferred(self):
        self.assertFalse(assess(self.job(description="MD required for this healthcare internship."), {})["include"])
        self.assertTrue(assess(self.job(description="MD preferred. MBA candidates eligible."), {})["include"])
        self.assertTrue(assess(self.job(description="MD or MBA required."), {})["include"])

    def test_citizenship_not_inferred_from_sponsorship(self):
        result = assess(self.job(description="US citizenship required. No sponsorship available."), {})
        self.assertTrue(result["include"])
        self.assertTrue(any("citizenship" in c.lower() for c in result["cautions"]))

    def test_unrelated_company_not_rescued_by_health_benefits(self):
        result = assess(self.job(company="Widget Corp", title="Marketing Intern", description="We sell furniture. Benefits include healthcare insurance."), {})
        self.assertFalse(result["include"])

    def test_known_employer_can_supply_sector_evidence(self):
        job = self.job(company="Example Corp", title="Finance Analyst", description="MBA preferred")
        self.assertFalse(assess(job, {})["include"])
        self.assertTrue(assess(job, {"healthcare_companies": ["Example Corp"]})["include"])

    def test_uncertain_strong_business_opportunity_preserved(self):
        result = assess(self.job(title="Product Manager", description="MBA preferred. Healthcare product strategy.", salary="$110,000 - $140,000 per year"), {})
        self.assertTrue(result["include"])
        self.assertEqual(result["track"], "early-stage")
        self.assertTrue(any("Career level is unclear" in c for c in result["cautions"]))
        self.assertIn("110,000", result["salary_text"])

    def test_salary_is_not_invented_and_hourly_is_not_annualized(self):
        missing = assess(self.job(), {})
        hourly = assess(self.job(salary="$55 per hour"), {})
        self.assertEqual(missing["salary_text"], "")
        self.assertFalse(any("strong-pay" in r for r in hourly["reasons"]))

    def test_no_eligibility_confirmation_or_dates_are_invented(self):
        result = assess(self.job(description="Internship graduating class of 2028 only."), {})
        self.assertTrue(result["include"])
        self.assertNotEqual(result["eligibility"], "Eligible")
        self.assertTrue(any("graduation" in c for c in result["cautions"]))
        self.assertEqual(result, assess(self.job(description="Internship graduating class of 2028 only."), {}))

    def test_detail_queue_does_not_require_sector_in_title(self):
        generic = self.job(company="Example Corp", title="Marketing Intern", description="")
        self.assertTrue(needs_detail(generic, {}))
        self.assertTrue(needs_detail(self.job(title="Clinical Research Associate", description=""), {}))
        self.assertFalse(needs_detail(self.job(title="Director, Marketing"), {}))
        self.assertFalse(needs_detail(self.job(location="Remote, Germany"), {}))

    def test_scientist_one_is_plausibly_entry_level(self):
        job = self.job(title="Scientist I", description="Biotechnology research. Bachelor's degree required.")
        self.assertTrue(needs_detail(job, {}))
        self.assertTrue(assess(job, {})["include"])

    def test_physician_business_role_is_not_a_clinical_license_requirement(self):
        job = self.job(title="Physician Marketing Intern", description="MBA internship in healthcare marketing.")
        self.assertTrue(needs_detail(job, {}))
        self.assertTrue(assess(job, {})["include"])
        self.assertFalse(assess(self.job(title="Registered Nurse"), {})["include"])

    def test_live_noise_is_excluded_despite_healthcare_employer_and_pay(self):
        titles = ["HVAC Technician", "Inventory Control Analyst", "Product Counsel",
                  "Software Engineer I", "Caregiver", "Recovery Coach",
                  "Business Development Representative", "SDR", "Sales Development Representative",
                  "Territory Account Manager", "Sales Associate", "Field Service Engineer"]
        for title in titles:
            with self.subTest(title=title):
                job = self.job(title=title, description="Entry-level healthcare opportunity. MBA preferred.", salary="$150,000 per year")
                self.assertFalse(assess(job, {})["include"])
                self.assertFalse(needs_detail(job, {}))

    def test_industry_and_entry_level_alone_do_not_establish_relevance(self):
        result = assess(self.job(title="Office Assistant", description="Entry-level healthcare role."), {})
        self.assertFalse(result["include"])

    def test_research_and_operations_programs_survive_relevance_floor(self):
        for title, description in [
            ("Research Associate", "Neurobiology research. Bachelor's degree required."),
            ("Research Intern", "Laboratory internship in biotechnology."),
            ("Operations Leadership Development Program", "MBA rotational program in pharmaceutical operations."),
            ("MBA Manufacturing Operations Leadership Development Program", "MBA rotational program in biotechnology.")]:
            with self.subTest(title=title):
                self.assertTrue(assess(self.job(title=title, description=description), {})["include"])

    def test_strategic_bd_and_sales_operations_differ_from_quota_sales(self):
        self.assertTrue(assess(self.job(title="Business Development Associate", description="Healthcare partnerships and market strategy."), {})["include"])
        self.assertTrue(assess(self.job(title="Sales Operations Analyst", description="Healthcare commercial operations."), {})["include"])
        self.assertFalse(assess(self.job(title="Business Development Representative"), {})["include"])

    def test_senior_business_analysts_need_review_and_experience_limit(self):
        for title in ["Senior Commercial Analyst", "Sr. Finance Associate"]:
            with self.subTest(title=title):
                result = assess(self.job(title=title, description="MBA preferred. 2 years of relevant experience required."), {})
                self.assertTrue(result["include"])
                self.assertTrue(any("responsibility level" in c for c in result["cautions"]))
                self.assertFalse(assess(self.job(title=title, description="Minimum 6 years of relevant experience required."), {})["include"])
        self.assertFalse(assess(self.job(title="Senior Finance Manager"), {})["include"])

    def test_salary_extraction_ignores_wellness_allowance(self):
        perk_only = assess(self.job(description="Biotech strategy. We offer a $350 wellness allowance and benefits."), {})
        self.assertEqual(perk_only["salary_text"], "")
        actual = assess(self.job(description="Biotech strategy. We offer a $350 wellness allowance. Base salary range: $110,000 - $145,000 per year."), {})
        self.assertEqual(actual["salary_text"], "$110,000 - $145,000 per year")
        self.assertTrue(any("strong-pay" in reason for reason in actual["reasons"]))

    def test_hourly_compensation_is_retained_but_never_annualized(self):
        result = assess(self.job(description="Biotech internship. Hourly pay rate: $28 - $35 per hour."), {})
        self.assertEqual(result["salary_text"], "$28 - $35 per hour")
        self.assertFalse(any("strong-pay" in reason for reason in result["reasons"]))

    def test_independent_experience_requirements_do_not_erase_higher_minimum(self):
        for description in [
            "Minimum 6 years of overall experience. 2 years of analytics experience required.",
            "Minimum 6 years of overall experience and 2 years of analytics experience required.",
            "Minimum 6 years of overall experience; 2 years of analytics experience preferred."]:
            with self.subTest(description=description):
                result = assess(self.job(description=description), {})
                self.assertFalse(result["include"])
                self.assertTrue(any("6 years" in reason for reason in result["reasons"]))

    def test_alternative_degree_experience_path_is_preserved_with_caution(self):
        for description in [
            "BS with 5 years of experience or MS with 2 years of experience required.",
            "Bachelor's degree with 5 years or MBA with 2 years of relevant experience required."]:
            with self.subTest(description=description):
                result = assess(self.job(description=description), {})
                self.assertTrue(result["include"])
                self.assertTrue(any("2+ years" in caution for caution in result["cautions"]))
                self.assertNotEqual(result["eligibility"], "Eligible")

    def test_four_year_degree_and_graduation_dates_are_not_work_experience(self):
        for description in [
            "A 4 year degree is required. No prior experience required. Graduation class of 2027.",
            "A 4 year college degree and 2 years of analytics experience required."]:
            with self.subTest(description=description):
                result = assess(self.job(description=description), {})
                self.assertTrue(result["include"])
                self.assertFalse(any("4+ years" in caution for caution in result["cautions"]))


if __name__ == "__main__":
    unittest.main()
