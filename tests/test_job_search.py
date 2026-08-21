import unittest
from unittest.mock import patch

from jobtrends.sources.ats_job_search import (
    city_search_terms,
    fetch_smartrecruiters_posting_url,
    matches_city,
    matches_city_location,
    normalize_smartrecruiters,
    parse_job_url,
)
from jobtrends.sources.google_job_search import DEFAULT_ATS_SITES, build_site_queries
from jobtrends.analysis.google_language_trends import build_city_keyword_filter, NO_DATE_FILTER_SITES


class MetroCityTests(unittest.TestCase):
    def test_dallas_query_includes_metro_aliases_in_one_clause(self):
        query = build_city_keyword_filter("dallas", "2026-08-01")

        self.assertIn('("dallas" | "fort worth" | "plano" | "irving" | "richardson")', query)
        self.assertIn("after:2026-08-01", query)

    def test_no_date_filter_sites_omit_the_after_clause(self):
        self.assertIn("careerpuck.com", NO_DATE_FILTER_SITES)
        query = build_city_keyword_filter("dallas", "2026-08-01", include_date=False)

        self.assertNotIn("after:", query)
        self.assertIn('("dallas" | "fort worth" | "plano" | "irving" | "richardson")', query)


    def test_houston_aliases_match_structured_location(self):
        posting = {"location": "The Woodlands, Texas", "description": ""}

        self.assertTrue(matches_city_location(posting, "houston"))

    def test_description_does_not_create_false_city_match(self):
        posting = {
            "location": "Bozeman, Montana",
            "description": "Our company was founded in Dallas.",
        }

        self.assertFalse(matches_city_location(posting, "dallas"))
        self.assertFalse(matches_city(posting, "dallas"))

    def test_unconfigured_city_uses_its_literal_name(self):
        self.assertEqual(city_search_terms("seattle"), ("seattle",))
        self.assertTrue(matches_city_location({"location": "Seattle, WA"}, "seattle"))

    def test_allowlisted_company_canada_wide_remote_counts_as_toronto(self):
        posting = {"location": "Remote, Canada", "company": "felix"}

        self.assertTrue(matches_city_location(posting, "toronto"))

    def test_non_allowlisted_company_canada_wide_remote_is_not_toronto(self):
        posting = {"location": "Canada", "company": "jobgether"}

        self.assertFalse(matches_city_location(posting, "toronto"))

    def test_canada_wide_remote_does_not_count_for_other_cities(self):
        posting = {"location": "Remote, Canada", "company": "felix"}

        self.assertFalse(matches_city_location(posting, "dallas"))

    def test_specific_other_city_in_canada_is_not_treated_as_country_wide(self):
        posting = {"location": "Vancouver, BC, Canada", "company": "felix"}

        self.assertFalse(matches_city_location(posting, "toronto"))


class SmartRecruitersTests(unittest.TestCase):
    def test_parse_public_job_url(self):
        self.assertEqual(
            parse_job_url(
                "https://jobs.smartrecruiters.com/Acme/744000123456789-software-engineer?trid=abc"
            ),
            ("smartrecruiters", "acme", "744000123456789"),
        )

    def test_parse_careerpuck_job_url_maps_to_greenhouse(self):
        self.assertEqual(
            parse_job_url("https://app.careerpuck.com/job-board/lyft/job/8648043002?gh_jid=8648043002"),
            ("greenhouse", "lyft", "8648043002"),
        )

    def test_normalize_detail_posting(self):
        posting = normalize_smartrecruiters(
            {
                "name": "Software Engineer",
                "location": {"fullLocation": "Plano, TX, United States"},
                "postingUrl": "https://jobs.smartrecruiters.com/Acme/123-software-engineer",
                "releasedDate": "2026-08-01T12:00:00Z",
                "jobAd": {
                    "sections": {
                        "jobDescription": {"text": "<p>Build services with Python.</p>"},
                        "qualifications": {"text": "SQL experience"},
                    }
                },
            },
            "Acme",
        )

        self.assertEqual(posting["platform"], "smartrecruiters")
        self.assertEqual(posting["location"], "Plano, TX, United States")
        self.assertEqual(posting["description"], "Build services with Python. SQL experience")

    @patch("jobtrends.sources.ats_job_search.fetch_json")
    def test_fetch_single_posting_uses_public_api(self, fetch_json):
        fetch_json.return_value = {
            "name": "Software Engineer",
            "location": {"fullLocation": "Dallas, TX"},
            "postingUrl": "https://jobs.smartrecruiters.com/Acme/123-software-engineer",
            "releasedDate": "2026-08-01T12:00:00Z",
        }

        posting = fetch_smartrecruiters_posting_url(
            "https://jobs.smartrecruiters.com/Acme/123-software-engineer",
            verify=True,
        )

        fetch_json.assert_called_once_with(
            "https://api.smartrecruiters.com/v1/companies/acme/postings/123",
            verify=True,
        )
        self.assertEqual(posting["title"], "Software Engineer")

    def test_fifth_domain_stays_in_one_query_batch(self):
        self.assertIn("jobs.smartrecruiters.com", DEFAULT_ATS_SITES)
        self.assertEqual(build_site_queries(DEFAULT_ATS_SITES[:5], '"dallas"'), [
            "site:lever.co | site:greenhouse.io | site:jobs.ashbyhq.com | "
            "site:myworkdayjobs.com | site:jobs.smartrecruiters.com \"dallas\""
        ])

    def test_sixth_domain_spills_into_a_second_query_batch(self):
        self.assertIn("careerpuck.com", DEFAULT_ATS_SITES)
        self.assertEqual(build_site_queries(DEFAULT_ATS_SITES, '"dallas"'), [
            "site:lever.co | site:greenhouse.io | site:jobs.ashbyhq.com | "
            "site:myworkdayjobs.com | site:jobs.smartrecruiters.com \"dallas\"",
            "site:careerpuck.com \"dallas\"",
        ])


if __name__ == "__main__":
    unittest.main()