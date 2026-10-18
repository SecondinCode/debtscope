import json
import unittest
from pathlib import Path

from debtscope.analytics import principal_universe
from debtscope.edgar import Filing
from debtscope.extract import extract_instruments, parse_number
from debtscope.pipeline import build_profile_from_documents

FIX = Path(__file__).parent / "fixtures"


def wrap(body: str) -> str:
    return f"<html><body>{body}</body></html>"


class CoverPageTests(unittest.TestCase):
    def test_apple_style_cover_rows(self):
        # Rows as printed on a real Section 12(b) cover table.
        rows = ["0.000% Notes due 2025", "1.625% Notes due 2026", "2.000% Notes due 2027",
                "1.375% Notes due 2029", "3.050% Notes due 2029", "0.500% Notes due 2031",
                "3.600% Notes due 2042"]
        html = wrap(
            "<table><tr><td>Title of each class</td><td>Trading symbol(s)</td>"
            "<td>Name of each exchange on which registered</td></tr>"
            "<tr><td>Common Stock, $0.00001 par value per share</td><td>AAPL</td>"
            "<td>The Nasdaq Stock Market LLC</td></tr>"
            + "".join(f"<tr><td>{r}</td><td>&#8212;</td><td>The Nasdaq Stock Market LLC</td></tr>"
                      for r in rows)
            + "</table><p>PART I</p>")
        inst, _, _ = extract_instruments(html, 2025)
        got = sorted((i.coupon, i.maturity_year) for i in inst)
        self.assertEqual(got, sorted([(0.0, 2025), (1.625, 2026), (2.0, 2027), (1.375, 2029),
                                      (3.05, 2029), (0.5, 2031), (3.6, 2042)]))
        self.assertTrue(all(i.exchange == "The Nasdaq Stock Market LLC" for i in inst))
        self.assertTrue(all(i.amount is None for i in inst))  # cover never gives principal


class TableTests(unittest.TestCase):
    def test_amounts_scale_and_dash(self):
        html = wrap(
            "<p>(in thousands)</p><table>"
            "<tr><td>5.250% Senior Notes due 2031</td><td>$</td><td>650,000</td><td>$</td><td>650,000</td></tr>"
            "<tr><td>3.100% Notes due 2025</td><td>&#8212;</td><td>400,000</td></tr>"
            "</table>")
        inst, (scale, word), _ = extract_instruments(html, 2025)
        by = {(i.coupon, i.maturity_year): i for i in inst}
        self.assertEqual(word, "thousands")
        self.assertEqual(by[(5.25, 2031)].amount, 650_000_000)
        self.assertEqual(by[(3.1, 2025)].amount, 0)
        self.assertEqual(by[(3.1, 2025)].status, "redeemed")

    def test_aggregate_row_prevents_double_count(self):
        html = wrap(
            "<p>(dollars in millions)</p><table>"
            "<tr><td>Fixed-rate 0.000% &#8211; 4.850% notes</td><td>2026 &#8211; 2062</td>"
            "<td>$</td><td>90,678</td><td>4.12%</td></tr></table>"
            "<p>In May 2025, the Company issued $1.5 billion of 4.000% Notes due 2028.</p>")
        inst, _, _ = extract_instruments(html, 2025)
        agg = [i for i in inst if i.is_aggregate]
        self.assertEqual(len(agg), 1)
        self.assertEqual((agg[0].coupon, agg[0].coupon_to), (0.0, 4.85))
        self.assertEqual((agg[0].maturity_year, agg[0].maturity_year_to), (2026, 2062))
        self.assertEqual(agg[0].amount, 90_678e6)
        universe = principal_universe(inst)
        self.assertEqual(sum(i.amount for i in universe), 90_678e6)  # 4.000% 2028 is inside

    def test_parse_number(self):
        self.assertEqual(parse_number("$ 1,250"), 1250)
        self.assertEqual(parse_number("(1,250)"), -1250)
        self.assertEqual(parse_number("—"), 0.0)
        self.assertIsNone(parse_number("2042"))
        self.assertIsNone(parse_number("4.12%"))


class NarrativeTests(unittest.TestCase):
    def test_two_issuances_in_one_sentence(self):
        html = wrap("<p>In June 2025, the Company issued $1.0 billion of 4.000% Notes due 2028 "
                    "and $1.5 billion of 4.300% Notes due 2032.</p>")
        inst, _, _ = extract_instruments(html, 2025)
        by = {i.coupon: i.amount for i in inst}
        self.assertEqual(by[4.0], 1.0e9)
        self.assertEqual(by[4.3], 1.5e9)

    def test_redemption_price_is_not_a_bond(self):
        html = wrap("<p>The redemption price equals 101% of the notes due on the date.</p>"
                    "<p>Redeemable at 100% of the principal amount of the Notes due 2030.</p>")
        inst, _, _ = extract_instruments(html, 2025)
        self.assertEqual(inst, [])

    def test_hidden_inline_xbrl_ignored(self):
        html = wrap("<div style='display:none'><ix:header>9.999% Notes due 2099</ix:header></div>")
        inst, _, _ = extract_instruments(html, 2025)
        self.assertEqual(inst, [])

    def test_foreign_currency_issue_not_mixed_with_reported_amount(self):
        html = wrap("<p>(in millions)</p><table><tr><td>0.875% Notes due 2029</td><td>868</td></tr></table>"
                    "<p>The Company issued &#8364;800 million of 0.875% Notes due 2029.</p>")
        (inst,), _, _ = extract_instruments(html, 2025)
        self.assertEqual(inst.amount, 868e6)
        self.assertEqual(inst.currency, "EUR")
        self.assertTrue(any("EUR 800m" in f for f in inst.flags))


class EndToEndTests(unittest.TestCase):
    def test_synthetic_10k(self):
        filing = Filing(9999999, "Demo", "10-K", "0000000000-26-000001",
                        "2026-02-15", "2025-12-31", "demo.htm")
        html = (FIX / "demo_10k.html").read_text(encoding="utf-8")
        facts = json.loads((FIX / "demo_companyfacts.json").read_text(encoding="utf-8"))
        p = build_profile_from_documents(filing, html, facts, "DEMO")
        m = p.metrics
        self.assertEqual(m["xbrl_total_debt"], 7_201e6)
        self.assertAlmostEqual(m["coverage_ratio"], 1.0)
        self.assertEqual(m["instruments_outstanding"], 6)
        self.assertAlmostEqual(m["net_debt"], 7_601e6 - 3_000e6)
        self.assertAlmostEqual(m["interest_coverage"], 3_200 / 260)
        # WAC: fixed, outstanding, principal-weighted (floating excluded)
        fixed = {1.625: 1083, 4.0: 1500, 0.875: 868, 2.95: 2000, 3.6: 1250}
        wac = sum(c * a for c, a in fixed.items()) / sum(fixed.values())
        self.assertAlmostEqual(m["weighted_avg_coupon_pct"], wac)
        dated = [i for i in p.instruments if i.coupon == 2.95][0]
        self.assertEqual((dated.maturity_month, dated.maturity_day), (3, 15))


if __name__ == "__main__":
    unittest.main()


class AggregateIssuerTests(unittest.TestCase):
    """Layout where term debt is only aggregated and notes are listed as exhibits."""

    def setUp(self):
        html = (FIX / "aggregate_style_10k.html").read_text(encoding="utf-8")
        self.inst, _, _ = extract_instruments(html, 2025)
        self.by = {(i.coupon, i.maturity_year): i for i in self.inst if not i.is_aggregate}

    def test_exhibit_numbers_are_not_amounts(self):
        self.assertIsNone(self.by[(3.0, 2027)].amount)            # was "4.1" before the fix
        self.assertEqual(self.by[(3.0, 2027)].status, "listed in exhibits")
        self.assertEqual(self.by[(0.45, 2016)].status, "matured")

    def test_cover_listed_note_stays_outstanding(self):
        self.assertEqual(self.by[(3.6, 2042)].status, "outstanding")
        self.assertIn("exhibit_index", self.by[(3.6, 2042)].sources)

    def test_effective_rates(self):
        aggs = sorted((i for i in self.inst if i.is_aggregate), key=lambda i: i.coupon)
        self.assertEqual([a.amount for a in aggs], [80_000e6, 4_500e6])
        self.assertIsNone(aggs[0].effective_rate)                  # a range: no invented midpoint
        self.assertIn("4.78", aggs[0].effective_rate_text)
        self.assertEqual(aggs[1].effective_rate, 4.38)

    def test_no_double_count(self):
        self.assertEqual(sum(i.amount for i in principal_universe(self.inst)), 84_500e6)


class ColumnTableTests(unittest.TestCase):
    """Debt table organised by issuance with maturity / rate / amount columns."""

    def setUp(self):
        html = (FIX / "column_style_10k.html").read_text(encoding="utf-8")
        self.inst, _, _ = extract_instruments(html, 2026)

    def test_single_issue_row_merges_with_exhibit(self):
        note = next(i for i in self.inst if i.coupon == 5.2)
        self.assertEqual((note.maturity_year, note.amount, note.effective_rate), (2039, 520e6, 5.24))
        self.assertEqual(note.status, "outstanding")
        self.assertEqual(set(note.sources), {"footnote_table", "exhibit_index"})

    def test_range_row_is_aggregate(self):
        agg = next(i for i in self.inst if i.is_aggregate and i.coupon == 2.4)
        self.assertEqual((agg.coupon, agg.coupon_to), (2.4, 3.95))
        self.assertEqual((agg.maturity_year, agg.maturity_year_to, agg.amount), (2026, 2056, 6_000e6))

    def test_split_percent_cells_and_full_date(self):
        note = next(i for i in self.inst if i.coupon == 4.5)
        self.assertEqual((note.maturity_year, note.maturity_month, note.maturity_day), (2033, 8, 8))
        self.assertEqual(note.amount, 5_000e6)
        self.assertEqual(note.effective_rate, 4.61)

    def test_range_split_across_cells(self):
        agg = next(i for i in self.inst if i.is_aggregate and i.coupon == 3.3)
        self.assertEqual((agg.coupon_to, agg.maturity_year, agg.maturity_year_to), (4.5, 2027, 2057))
        self.assertEqual((agg.amount, agg.status), (4_000e6, "outstanding"))
        self.assertIn("4.53", agg.effective_rate_text)

    def test_rounded_coupon_merges_with_exhibit(self):
        note = next(i for i in self.inst if i.maturity_year == 2052)
        self.assertEqual((note.coupon, note.amount), (2.921, 3_000e6))
        self.assertEqual(set(note.sources), {"footnote_table", "exhibit_index"})

    def test_totals_match(self):
        self.assertEqual(len(self.inst), 5)
        self.assertEqual(sum(i.amount for i in principal_universe(self.inst)), 18_520e6)


class SplitRangeTests(unittest.TestCase):
    """Ranges split across cells and two issuances with identical ranges."""

    def setUp(self):
        html = (FIX / "split_range_10k.html").read_text(encoding="utf-8")
        self.inst, _, _ = extract_instruments(html, 2026)

    def test_total_matches_face_value(self):
        self.assertEqual(sum(i.amount for i in principal_universe(self.inst)), 14_794e6)

    def test_same_range_issuances_stay_separate(self):
        twins = [i for i in self.inst if i.coupon == 1.35]
        self.assertEqual(sorted(i.amount for i in twins), [56e6, 3_344e6])
        self.assertTrue(twins[0].label.startswith("2023 issuance") or twins[0].label.startswith("2024 issuance"))

    def test_euro_issuance_denomination(self):
        eur = next(i for i in self.inst if i.coupon == 2.63)
        self.assertEqual((eur.currency, eur.coupon_to, eur.maturity_year_to), ("EUR", 3.13, 2033))


class InlineXbrlViewerTests(unittest.TestCase):
    def test_tagged_numbers_are_highlighted_with_concept(self):
        from debtscope.extract import load_soup
        from debtscope.viewer import annotate
        html = ('<html><body><p>Term debt was $<ix:nonFraction name="us-gaap:LongTermDebt" '
                'unitRef="usd" scale="9">90.7</ix:nonFraction> billion and net sales '
                '$<ix:nonFraction name="us-gaap:Revenues" unitRef="usd">416.2</ix:nonFraction> billion; '
                'effective tax rate <ix:nonFraction name="us-gaap:EffectiveIncomeTaxRateContinuingOperations" '
                'unitRef="number">15.6</ix:nonFraction>%.</p></body></html>')
        soup = load_soup(html)
        counts = annotate(soup)
        marks = {m["data-x"]: m["data-c"] for m in soup.find_all("mark") if m.get("data-x")}
        self.assertEqual(marks["us-gaap:LongTermDebt"], "debt")
        self.assertEqual(marks["us-gaap:Revenues"], "money")
        self.assertEqual(marks["us-gaap:EffectiveIncomeTaxRateContinuingOperations"], "rates")
        self.assertGreaterEqual(counts["debt"], 2)   # tagged number + the words "term debt"


class TableFigureHighlightTests(unittest.TestCase):
    def test_untagged_table_figures_are_highlighted_by_row(self):
        from debtscope.extract import load_soup
        from debtscope.viewer import annotate
        html = ("<html><body><table><tr><td>Item 1A.</td><td>Risk Factors</td><td>5</td></tr></table>"
                "<p>(dollars in millions)</p><table>"
                "<tr><td>Term debt</td><td>$</td><td>12,350</td></tr>"
                "<tr><td>Effective tax rate</td><td>15.6</td><td>%</td></tr>"
                "<tr><td>Total net sales</td><td>$</td><td>416,161</td></tr></table></body></html>")
        soup = load_soup(html)
        annotate(soup)
        marks = {m.get_text(strip=True): m["data-c"] for m in soup.find_all("mark") if m.get("data-r")}
        self.assertEqual(marks, {"12,350": "debt", "15.6": "rates", "416,161": "money"})  # page "5" skipped
