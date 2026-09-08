"""Financial formula regressions with small, fully offline statement fixtures."""

from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import quality_screen as qs


class Series:
    def __init__(self, dates, values):
        self.index, self.values = dates, values
        self.iloc = self

    def __len__(self):
        return len(self.values)

    def __getitem__(self, index):
        return self.values[index]

    def tolist(self):
        return self.values


class Statement:
    def __init__(self, rows, dates=None):
        dates = dates or [datetime(2026, 3, 31), datetime(2025, 3, 31)]
        self.index = list(rows)
        self.empty = not rows
        self.loc = {name: Series(dates, values) for name, values in rows.items()}


class QualityFormulaTests(unittest.TestCase):
    def dilution(self, issuance=None, equity=(200, 100), extra_cf=None):
        bs = Statement({"Stockholders Equity": list(equity)})
        rows = dict(extra_cf or {})
        if issuance is not None:
            rows["Common Stock Issuance"] = [issuance, 0]
        result = qs.piotroski_f(bs, Statement({}), Statement(rows))
        return next(item for item in result["tests"] if item["test"] == "no common-equity issuance"), result

    def test_increasing_book_equity_does_not_pass_without_issuance_evidence(self):
        test, result = self.dilution()
        self.assertIsNone(test["result"])
        self.assertEqual(result["max"], 0)
        self.assertIn("gross Common Stock Issuance", test["basis"])

    def test_positive_gross_issuance_fails_even_with_buybacks(self):
        test, _ = self.dilution(20, extra_cf={"Net Common Stock Issuance": [-5, 0]})
        self.assertFalse(test["result"])

    def test_explicit_zero_gross_issuance_passes_independent_of_book_equity(self):
        for equity in ((200, 100), (50, 100)):
            with self.subTest(equity=equity):
                test, _ = self.dilution(0, equity=equity)
                self.assertTrue(test["result"])

    def test_negative_nonfinite_or_only_net_issuance_remains_unavailable(self):
        for issuance in (-1, float("nan"), float("inf")):
            with self.subTest(issuance=issuance):
                test, _ = self.dilution(issuance)
                self.assertIsNone(test["result"])
        test, _ = self.dilution(extra_cf={"Net Common Stock Issuance": [-10, 0]})
        self.assertIsNone(test["result"])

    def growth(self, dates, values):
        return qs.cagr(Statement({"Total Revenue": values}, dates), "Total Revenue")

    def test_missing_middle_year_does_not_inflate_cagr(self):
        rate, years = self.growth(["2026-03-31", "2025-03-31", "2024-03-31"], [121, None, 100])
        self.assertEqual(rate, 10.0)
        self.assertAlmostEqual(years, 2, places=2)

    def test_nonconsecutive_and_unsorted_periods_use_real_elapsed_years(self):
        rate, years = self.growth(["2022-03-31", "2026-03-31", "2025-03-31"], [100, 146.41, 133.1])
        self.assertEqual(rate, 10.0)
        self.assertAlmostEqual(years, 4, places=2)

    def test_missing_endpoint_reports_the_actual_remaining_span(self):
        rate, years = self.growth(["2026-03-31", "2025-03-31", "2023-03-31"], [None, 121, 100])
        self.assertEqual(rate, 10.0)
        self.assertAlmostEqual(years, 2, places=2)

    def test_undated_or_duplicate_periods_are_unavailable(self):
        for dates in ([0, 1], ["TTM", "2025-03-31"], ["2026-03-31", "2026-03-31"]):
            with self.subTest(dates=dates):
                self.assertEqual(self.growth(dates, [121, 100]), (None, None))

    def test_nonpositive_endpoints_and_insufficient_values_remain_unknown(self):
        for values in ([121, 0], [-121, 100]):
            rate, years = self.growth(["2026-03-31", "2024-03-31"], values)
            self.assertIsNone(rate)
            self.assertAlmostEqual(years, 2, places=2)
        self.assertEqual(self.growth(["2026-03-31", "2024-03-31"], [float("inf"), 100]), (None, None))

    def test_different_sales_and_profit_spans_are_labelled_separately(self):
        financials = Statement({"Total Revenue": [121, 110, 100], "Net Income": [None, 110, 100]},
                               ["2026-03-31", "2025-03-31", "2024-03-31"])
        handle = SimpleNamespace(info={}, balance_sheet=Statement({}),
                                 financials=financials, cashflow=Statement({}))
        with patch.object(qs, "_import_yf", return_value=SimpleNamespace(Ticker=lambda ticker: handle)):
            result = qs.evaluate("TEST.NS", "Test", "TEST", qs.DEFAULTS)
        self.assertIsNone(result["growth_window_years"])
        self.assertAlmostEqual(result["growth_windows_years"]["sales"], 2, places=2)
        self.assertAlmostEqual(result["growth_windows_years"]["profit"], 1, places=2)
        self.assertIn("elapsed years", result["checks"]["sales_growth"]["note"])


if __name__ == "__main__":
    unittest.main()
