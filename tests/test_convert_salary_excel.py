import unittest
from types import SimpleNamespace

from tools.convert_salary_excel import (
    INDEX_PATTERNS,
    detect_column_type,
    header_matches,
    parse_numeric_cell,
    parse_sheet,
)


class FakeWorksheet:
    title = "Sheet1"

    def __init__(self, rows):
        self.rows = rows

    def iter_rows(self, min_row=1, max_row=None, values_only=False):
        rows = self.rows[min_row - 1:max_row]
        for row in rows:
            if values_only:
                yield row
            else:
                yield [SimpleNamespace(value=value) for value in row]

    def __getitem__(self, row_number):
        return [SimpleNamespace(value=value) for value in self.rows[row_number - 1]]


class DetectColumnTypeTests(unittest.TestCase):
    def test_index_headers_are_not_misclassified_as_count(self):
        for header in ("Index", "Salary Index", "Engineering Index", "Median salary"):
            with self.subTest(header=header):
                self.assertEqual(detect_column_type(header), "index")

    def test_single_letter_n_only_matches_as_a_token(self):
        self.assertEqual(detect_column_type("Employee n"), "count")
        self.assertEqual(detect_column_type("Engineering"), None)

    def test_count_headers_still_match_common_labels(self):
        for header in ("Count", "Engineering Count", "員工人數", "Engineering 人數"):
            with self.subTest(header=header):
                self.assertEqual(detect_column_type(header), "count")

    def test_count_inside_word_does_not_make_count_header(self):
        self.assertIsNone(detect_column_type("Accounting Total"))
        self.assertEqual(detect_column_type("Accounting Index"), "index")

    def test_chinese_glued_headers_still_match(self):
        self.assertEqual(detect_column_type("薪金指數"), "index")
        self.assertEqual(detect_column_type("工程師數量"), "count")

    def test_compound_patterns_match_as_substring_but_others_do_not(self):
        # A compound token ("薪金") matches inside a glued header word.
        self.assertTrue(header_matches("薪金指數", INDEX_PATTERNS))
        # A pattern that is not a compound token ("salary") only matches as a
        # whole token, so it must not match inside an unrelated glued word.
        self.assertFalse(header_matches("salaryindex", INDEX_PATTERNS))
        self.assertTrue(header_matches("salary index", INDEX_PATTERNS))

    def test_parse_sheet_preserves_category_name_with_letter_n(self):
        ws = FakeWorksheet([
            ("Company", "Engineering Count", "Engineering Index"),
            ("Example Corp", 12, 105.5),
        ])

        companies = parse_sheet(ws)

        self.assertEqual(companies[0]["categories"]["engineering"], {"count": 12, "index": 105.5})

    def test_parse_sheet_groups_accounting_count_index_pair(self):
        ws = FakeWorksheet([
            ("Company", "Accounting Count", "Accounting Index"),
            ("Example Corp", 12, 105.5),
        ])

        companies = parse_sheet(ws)

        self.assertEqual(companies[0]["categories"]["accounting"], {"count": 12, "index": 105.5})

    def test_parse_sheet_normalizes_paired_category_name_with_underscores(self):
        ws = FakeWorksheet([
            ("Company", "Software Engineering Count", "Software Engineering Index"),
            ("Example Corp", 8, 110.0),
        ])

        companies = parse_sheet(ws)

        self.assertEqual(companies[0]["categories"]["software_engineering"], {"count": 8, "index": 110.0})

    def test_parse_sheet_accepts_decimal_comma_and_localized_thousands(self):
        ws = FakeWorksheet([
            ("Company", "Engineering Count", "Engineering Index", "Overall Salary"),
            ("Example Corp", "12,0", "1.234,5", "1 234,5"),
        ])

        companies = parse_sheet(ws)

        assert companies[0]["categories"]["engineering"] == {
            "count": 12,
            "index": 1234.5,
        }
        assert companies[0]["categories"]["overall_salary"] == {"index": 1234.5}

    def test_ambiguous_single_separator_is_rejected_for_numeric_cells(self):
        with self.assertRaises(ValueError):
            parse_numeric_cell("1,234")

    def test_single_dot_decimal_keeps_excel_v13_compatibility(self):
        self.assertEqual(parse_numeric_cell("1.234"), 1.234)


if __name__ == "__main__":
    unittest.main()
