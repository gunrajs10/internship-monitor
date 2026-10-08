import unittest

from opportunity_rank import assess, needs_detail


class OpportunityRankTests(unittest.TestCase):
    def test_california_policy_employer_exception_does_not_waive_role_rules(self):
        config = {"geography": {"california_focus": True, "outside_california_employers": ["Amgen", "Bristol Myers Squibb"]}}
        self.assertTrue(assess(self.job(), config)["include"])
        self.assertFalse(assess(self.job(location="Boston, MA"), config)["include"])
        self.assertFalse(needs_detail(self.job(location="Boston, MA"), config))
        self.assertTrue(assess(self.job(company="Amgen Inc.", location="Boston, MA"), config)["include"])
        self.assertTrue(assess(self.job(company="Bristol-Myers Squibb", location="Princeton, NJ"), config)["include"])
        for changes in [dict(company="Amgen Recruiting Partner",location="Boston, MA"), dict(company="Amgen",location="Cambridge, United Kingdom"), dict(company="Amgen",location="Remote"), dict(company="Amgen",location="Boston, MA",title="Director, Commercial Strategy"), dict(company="Amgen",location="Boston, MA",description="Minimum 8 years of experience required.")]:
            self.assertFalse(assess(self.job(**changes),config)["include"],changes)

    def test_california_policy_does_not_confuse_canada_or_shared_city_names(self):
        config = {"geography": {"california_focus": True}}
        for location in ["Vancouver, CA", "Fremont, NE", "Berkeley Heights, NJ", "Remote - United States"]:
            self.assertFalse(assess(self.job(location=location),config)["include"],location)
        self.assertTrue(assess(self.job(location="Remote - California, United States"),config)["include"])

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

    def test_explicit_other_state_overrides_shared_california_city_names(self):
        for location in ["Berkeley Heights, NJ", "Fremont, NE", "Hayward, Wisconsin"]:
            with self.subTest(location=location):
                result = assess(self.job(location=location), {})
                self.assertTrue(result["include"])
                self.assertFalse(result["california"])
        self.assertTrue(assess(self.job(location="Fremont, CA"), {})["california"])
        self.assertTrue(assess(self.job(location="San Francisco, CA; Boston, MA"), {})["california"])

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

    def test_clinical_training_programs_are_not_business_programs(self):
        for title in ["DVM/VMD Mentorship Program", "Hospital Medicine AP Fellowship Program", "Advanced Practice Fellowship"]:
            with self.subTest(title=title):
                self.assertFalse(assess(self.job(title=title), {})["include"])
        self.assertTrue(assess(self.job(title="Clinical Program Coordinator"), {})["include"])
        self.assertTrue(assess(self.job(title="Physician Partnerships Intern"), {})["include"])

    def test_veterinary_and_advanced_practice_credentials_require_evidence(self):
        for credential in ["DVM", "VMD", "PA-C"]:
            with self.subTest(credential=credential):
                self.assertFalse(assess(self.job(description=credential + " required. Healthcare internship."), {})["include"])
                self.assertTrue(assess(self.job(description=credential + " preferred. MBA candidates eligible."), {})["include"])

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
                  "Territory Account Manager", "Sales Associate", "Field Service Engineer",
                  "Associate Marketing Designer", "Lab Waste Technician"]
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

    def test_work_years_without_experience_word_are_enforced(self):
        for description in [
            "- 4+ years in marketing analytics or marketing operations at a B2B SaaS company.",
            "- 8+ years in Sales Strategy & Operations, Revenue Operations, or related analytical/GTM roles,",
            "5+ years in clinical operations, clinical project management, or CRO/vendor management",
            "Minimum 6 years working in biotechnology strategy.",
            "You have 7 years building commercial teams.",
            "4 or more years of relevant experience required.",
        ]:
            with self.subTest(description=description):
                result = assess(self.job(description=description), {})
                self.assertFalse(result["include"])
                self.assertTrue(any("years of experience" in reason for reason in result["reasons"]))

    def test_company_age_and_education_are_not_candidate_work_years(self):
        for description in [
            "We have 80 years in healthcare. MBA internship with no prior experience required.",
            "Must be at least 18 years of age. MBA internship in biotechnology.",
            "A 4 year undergraduate degree is required. MBA candidates encouraged.",
            "MBA internship. 5+ years in marketing preferred.",
        ]:
            with self.subTest(description=description):
                self.assertTrue(assess(self.job(description=description), {})["include"])

    def test_or_more_years_are_detected_without_dropping_entry_level(self):
        result = assess(self.job(description="3 or more years of experience in Operations or Supply Chain."), {})
        self.assertTrue(result["include"])
        self.assertTrue(any("3+ years" in c for c in result["cautions"]))

    def test_local_language_foreign_locations_are_excluded(self):
        for location in ["千葉県　柏市", "東京都", "大阪府", "日本", "中国", "Россия"]:
            with self.subTest(location=location):
                self.assertFalse(assess(self.job(location=location), {})["include"])
                self.assertFalse(needs_detail(self.job(location=location), {}))
        self.assertTrue(assess(self.job(location="Remote"), {})["include"])
        self.assertTrue(assess(self.job(location="Remote - United States"), {})["include"])

    def test_required_nursing_degree_and_listed_rn_are_excluded(self):
        for description in [
            "BSN required. Healthcare clinical program management.",
            "BSN is required. Healthcare management.",
            "Minimum Requirements\nLicenses & Certifications\nRN\nPreferred Requirements\nMaster's Degree",
        ]:
            with self.subTest(description=description):
                self.assertFalse(assess(self.job(title="Clinical Program Manager", description=description), {})["include"])
        self.assertTrue(assess(self.job(description="BSN preferred. MBA candidates eligible."), {})["include"])
        self.assertTrue(assess(self.job(description="Preferred Requirements\nBSN preferred\nMBA candidates eligible."), {})["include"])

    def test_current_cls_cgmbs_license_is_a_requirement(self):
        job = self.job(title="Clinical Lab Scientist I", description="Qualifications\nCurrent California CLS license (Clinical Laboratory Science - Generalist) or CGMBS license (Clinical Genetics Molecular Biology Scientist)")
        self.assertFalse(assess(job, {})["include"])
        self.assertTrue(assess(self.job(description="CLS license preferred. MBA internship."), {})["include"])

    def test_vie_citizenship_and_age_are_flagged_without_assumption(self):
        result = assess(self.job(title="Global Oncology Market Access Junior Project Specialist VIE Contract", description="VIE Program is available to citizens of the European Economic Area (EU + Norway, Liechtenstein and Iceland) aged between 18 and 28. Candidates cannot apply to an assignment in their own country of citizenship."), {})
        self.assertTrue(result["include"])
        self.assertTrue(any("EEA citizenship" in caution and "age" in caution and "unverified" in caution for caution in result["cautions"]))
        self.assertNotEqual(result["eligibility"], "Eligible")

    def test_explicit_operations_function_overrides_brand_title(self):
        job = self.job(title="Associate Brand Manager II", description="This is a role in Operations, not in Commercial or Marketing. MBA desired. Biotechnology operations.")
        result = assess(job, {})
        self.assertTrue(result["include"])
        self.assertTrue(any("operations role" in reason for reason in result["reasons"]))
        self.assertFalse(any("function (+37)" in reason for reason in result["reasons"]))

    def test_current_bachelor_enrollment_is_different_from_completed_degree(self):
        self.assertFalse(assess(self.job(description="Minimum Qualifications\nCurrent enrollment in a Bachelor's degree program."), {})["include"])
        self.assertTrue(assess(self.job(description="Current enrollment in a Bachelor's or Master's degree program."), {})["include"])

    def test_program_description_can_restrict_to_finishing_undergraduates(self):
        result = assess(self.job(title="Digital Leadership Development Program", description="Undergraduate student completing a Bachelor’s degree between December 2026 and June 2027 in Computer Science or related field. Healthcare technology."), {})
        self.assertFalse(result["include"])
        self.assertTrue(assess(self.job(description="Undergraduate students completing a Bachelor's or Master's degree may apply. MBA biotechnology internship."), {})["include"])

    def test_clinical_titles_do_not_conceal_field_sales(self):
        for title, description in [
            ("Associate Clinical Oncology Specialist", "Responsible for contributing to account sales. Healthcare oncology. Uncapped commission."),
            ("Clinical Account Associate", "Healthcare role. Monitor performance of sales. Plans include territory management and travel."),
        ]:
            with self.subTest(title=title):
                self.assertFalse(assess(self.job(title=title, description=description), {})["include"])
        self.assertTrue(assess(self.job(title="Clinical Research Associate", description="Life sciences research. Partner with the sales organization."), {})["include"])
        self.assertTrue(assess(self.job(title="Commercial Strategy Intern", description="Biotechnology strategy. Analyze account sales and territory management practices."), {})["include"])

    def test_nurse_residency_and_active_clinical_licenses_are_excluded(self):
        self.assertFalse(assess(self.job(title="Residency Program - New Nurse Graduates", description="Healthcare program. Current valid nursing license in U.S. and graduation from a qualified nursing program."), {})["include"])
        self.assertFalse(assess(self.job(title="Manager, Sterile Operations", description="You Have:\nDoctor of Pharmacy (Pharm D), with active license in the State of Ohio. Healthcare operations."), {})["include"])
        self.assertFalse(assess(self.job(title="Clinical Operations Associate", description="Requirements\nCurrent valid nursing license in U.S."), {})["include"])
        self.assertTrue(assess(self.job(description="Nursing license preferred. MBA healthcare strategy."), {})["include"])

    def test_qualification_headings_supply_required_credential_context(self):
        self.assertFalse(assess(self.job(title="Scientist I", description="Qualifications\nPhD in neuroscience. Biotechnology research."), {})["include"])
        self.assertTrue(assess(self.job(description="Preferred Qualifications\nPhD preferred. MBA candidates eligible."), {})["include"])


if __name__ == "__main__":
    unittest.main()
