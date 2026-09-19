"""Tests for salary_lookup.py — format_entry, match_score, and search_company."""

import unittest

from salary_lookup import (
    format_entry,
    normalize,
    anglicize,
    extract_core_words,
    match_score,
    search_company,
)


# ---------------------------------------------------------------------------
# format_entry tests (from #75 / #98)
# ---------------------------------------------------------------------------

class FormatEntryTests(unittest.TestCase):
    def test_zero_count_is_displayed_as_zero(self):
        entry = {
            "company": "Example Corp",
            "city": "",
            "categories": {
                "public_data": {
                    "count": 0,
                    "index": 100.0,
                },
            },
        }

        rendered = format_entry(entry, {"index_baseline": 100, "index_label": "Index"})

        self.assertRegex(rendered, r"Public Data\s+0\s+100\.0")

    def test_text_index_does_not_crash(self):
        entry = {
            "company": "Example Corp",
            "city": "",
            "categories": {
                "sample": {
                    "count": 3,
                    "index": "private",
                },
            },
        }

        rendered = format_entry(entry, {"index_baseline": 100, "index_label": "Index"})

        self.assertIn("private", rendered)

    def test_format_entry_with_zero_baseline(self):
        entry = {
            "company": "Example Corp",
            "city": "",
            "categories": {
                "it": {
                    "count": None,
                    "index": 45000.0,
                },
            },
        }
        rendered = format_entry(entry, {"index_baseline": 0, "index_label": "Salary"})
        self.assertIn("45000.0", rendered)
        self.assertNotIn("%", rendered)

    def test_format_entry_with_custom_baseline(self):
        entry = {
            "company": "Example Corp",
            "city": "",
            "categories": {
                "it": {
                    "count": None,
                    "index": 45000.0,
                },
            },
        }
        rendered = format_entry(entry, {"index_baseline": 40000, "index_label": "Salary"})
        self.assertIn("45000.0", rendered)
        self.assertIn("+12.5%", rendered)


# ---------------------------------------------------------------------------
# match_score tests (from #106)
# ---------------------------------------------------------------------------

class TestMatchScoreExactMatch(unittest.TestCase):
    def test_exact_match_returns_100(self):
        self.assertEqual(match_score("Cheung Kong", "Cheung Kong"), 100)

    def test_exact_match_case_insensitive(self):
        self.assertEqual(match_score("CHEUNG KONG", "Cheung Kong"), 100)

    def test_exact_match_after_suffix_stripping(self):
        self.assertEqual(match_score("MTR", "MTR Corporation Limited"), 100)


class TestMatchScoreSubstring(unittest.TestCase):
    def test_query_contained_in_entry_gives_high_score(self):
        score = match_score("Cheung", "Cheung Kong Holdings Limited")
        self.assertGreaterEqual(score, 80)

    def test_entry_contained_in_query_gives_high_score(self):
        score = match_score("Kong Holdings", "Cheung Kong")
        self.assertGreaterEqual(score, 80)


class TestMatchScoreShortQuery(unittest.TestCase):
    def test_short_query_no_word_overlap_returns_zero(self):
        score = match_score("ab", "Something Unrelated Company")
        self.assertEqual(score, 0)

    def test_short_query_with_word_overlap_scores(self):
        score = match_score("IBM", "IBM Corporation")
        self.assertGreater(score, 0)


class TestMatchScoreDiacritics(unittest.TestCase):
    def test_folded_variant_matches_diacritic_entry(self):
        # ä folds to ae, so the ASCII query spells it "Haeagen".
        score = match_score("Haeagen-Dazs", "Häagen-Dazs")
        self.assertGreater(score, 0)

    def test_folded_pair_scores_folded_tier(self):
        self.assertEqual(match_score("Haeagen-Dazs", "Häagen-Dazs"), 85)


class TestMatchScoreNoOverlap(unittest.TestCase):
    def test_completely_unrelated_names_return_zero(self):
        self.assertEqual(match_score("Apple", "Swire Pacific"), 0)

    def test_empty_query_returns_zero(self):
        self.assertEqual(match_score("", "Sun Hung Kai"), 0)

    def test_empty_entry_returns_zero(self):
        self.assertEqual(match_score("Sun Hung Kai", ""), 0)


# ---------------------------------------------------------------------------
# search_company tests (from #75 / #98 and #106)
# ---------------------------------------------------------------------------

def _make_data(*entries):
    return {"companies": list(entries)}


def _entry(company, city=""):
    return {"company": company, "city": city}


class SearchCompanyTests(unittest.TestCase):
    def test_search_company_with_none_city(self):
        data = {
            "companies": [
                {
                    "company": "Acme",
                    "city": None,
                }
            ]
        }
        results = search_company(data, "Acme", city="Shenzhen")
        self.assertEqual(results, [])


class UtilityTests(unittest.TestCase):
    def test_normalize_strips_suffix_and_noise(self):
        self.assertEqual(normalize("Sun Hung Kai Properties Limited"), "sunhungkaiproperties")
        self.assertEqual(normalize("MTR Corporation (Hong Kong)"), "mtr")
        self.assertEqual(normalize("Li & Fung (Hong Kong)"), "lifung")
        self.assertEqual(normalize("Simple Corp Limited"), "simple")

    def test_normalize_preserves_chinese_characters(self):
        self.assertEqual(normalize("長江實業集團有限公司"), "長江實業集團")
        self.assertEqual(normalize("Cheung Kong 長江實業"), "cheungkong長江實業")

    def test_anglicize_folds_latin_diacritics(self):
        self.assertEqual(anglicize("aøæåöäü"), "aoaeaaoaeu")
        self.assertEqual(anglicize("Häagen"), "haeagen")

    def test_extract_core_words(self):
        self.assertEqual(
            extract_core_words("Sun Hung Kai Properties Limited"),
            ["sun", "hung", "kai", "properties"],
        )
        self.assertEqual(extract_core_words("Ltd"), [])
        self.assertEqual(extract_core_words("Test Company (Sub-entity)"), ["test", "company"])
        self.assertEqual(extract_core_words("長江實業集團有限公司"), ["長江實業集團"])


class MatchScoreTests(unittest.TestCase):
    def test_exact_match_score(self):
        self.assertEqual(match_score("Sun Hung Kai", "Sun Hung Kai"), 100)
        self.assertEqual(match_score("sun hung kai", "Sun Hung Kai Limited"), 100)

    def test_partial_match_score(self):
        self.assertGreater(match_score("Sun", "Sun Hung Kai Properties Limited"), 80)
        self.assertEqual(match_score("Sun Hung Kai", "Sun"), 75)

    def test_anglicized_match_score(self):
        self.assertEqual(match_score("Haeagen-Dazs", "Häagen-Dazs"), 85)

    def test_overlap_match_score(self):
        # Overlap of multiple words
        self.assertGreater(match_score("Sun Kai Properties", "Sun Hung Kai Properties Limited"), 30)

    def test_no_match_score(self):
        self.assertEqual(match_score("Google", "Microsoft"), 0)


class SearchCompanyRefactoredTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "companies": [
                {"company": "Sun Hung Kai Properties Limited", "city": "Hong Kong"},
                {"company": "MTR Corporation Limited", "city": "Hong Kong"},
                {"company": "Swire Pacific Limited", "city": "Hong Kong"},
            ]
        }

    def test_search_by_name(self):
        results = search_company(self.data, "Sun")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["company"], "Sun Hung Kai Properties Limited")

    def test_search_with_city_filter(self):
        results = search_company(self.data, "MTR", city="Hong Kong")
        self.assertEqual(len(results), 1)

        # Mismatching city
        results_wrong_city = search_company(self.data, "MTR", city="Kowloon")
        self.assertEqual(len(results_wrong_city), 0)


class TestSearchCompanyBasicMatch(unittest.TestCase):
    def test_exact_name_returns_match(self):
        data = _make_data(_entry("Sun Hung Kai", "Hong Kong"))
        results = search_company(data, "Sun Hung Kai")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["company"], "Sun Hung Kai")

    def test_no_match_returns_empty_list(self):
        data = _make_data(_entry("Swire Pacific", "Hong Kong"))
        results = search_company(data, "Apple")
        self.assertEqual(results, [])

    def test_multiple_candidates_all_returned(self):
        data = _make_data(
            _entry("Harbour Centre", "Hong Kong"),
            _entry("Harbour Grand", "Kowloon"),
            _entry("Unrelated Corp", "Hong Kong"),
        )
        results = search_company(data, "Harbour")
        companies = [r["company"] for r in results]
        self.assertIn("Harbour Centre", companies)
        self.assertIn("Harbour Grand", companies)
        self.assertNotIn("Unrelated Corp", companies)


class TestSearchCompanyCityFilter(unittest.TestCase):
    def test_matching_city_is_included(self):
        data = _make_data(
            _entry("Sun Hung Kai", "Hong Kong"),
            _entry("Sun Hung Kai", "Kowloon"),
        )
        results = search_company(data, "Sun Hung Kai", city="Kowloon")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["city"], "Kowloon")

    def test_non_matching_city_is_excluded(self):
        data = _make_data(_entry("Sun Hung Kai", "Hong Kong"))
        results = search_company(data, "Sun Hung Kai", city="Shenzhen")
        self.assertEqual(results, [])

    def test_no_city_filter_returns_all_cities(self):
        data = _make_data(
            _entry("Sun Hung Kai", "Hong Kong"),
            _entry("Sun Hung Kai", "Kowloon"),
        )
        results = search_company(data, "Sun Hung Kai")
        self.assertEqual(len(results), 2)

    def test_city_filter_case_insensitive(self):
        data = _make_data(_entry("Sun Hung Kai", "Hong Kong"))
        results = search_company(data, "Sun Hung Kai", city="HONG KONG")
        self.assertEqual(len(results), 1)

    def test_partial_city_matches_city(self):
        data = _make_data(_entry("Sun Hung Kai", "Hong Kong"))
        results = search_company(data, "Sun Hung Kai", city="hong")
        self.assertEqual(len(results), 1)


class TestSearchCompanyScoreThreshold(unittest.TestCase):
    def test_low_score_matches_excluded(self):
        data = _make_data(_entry("Sun Hung Kai", "Hong Kong"))
        results = search_company(data, "xyz")
        self.assertEqual(results, [])

    def test_results_sorted_by_relevance_descending(self):
        data = _make_data(
            _entry("Henderson Land Development Company Limited", "Hong Kong"),
            _entry("Henderson Land", "Hong Kong"),
        )
        results = search_company(data, "Henderson Land")
        self.assertEqual(results[0]["company"], "Henderson Land")


if __name__ == "__main__":
    unittest.main()
