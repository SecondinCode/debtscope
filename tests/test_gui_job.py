import json
import tempfile
import unittest
from pathlib import Path

from debtscope import gui
from debtscope.edgar import EdgarError, Filing

FIX = Path(__file__).parent / "fixtures"


class RunJobTests(unittest.TestCase):
    """The desktop window's worker, with the SEC download replaced by fixtures."""

    def test_reports_crawler_and_errors(self):
        html = (FIX / "demo_10k.html").read_text(encoding="utf-8")
        facts = json.loads((FIX / "demo_companyfacts.json").read_text(encoding="utf-8"))

        def fake_fetch(client, ticker, form):
            if ticker == "BAD":
                raise EdgarError("Ticker 'BAD' not found on EDGAR")
            return (Filing(1, "Demo", "10-K", "0000000000-26-000001", "2026-02-15",
                           "2025-12-31", "d.htm"), html, facts)

        original = gui.fetch
        gui.fetch = fake_fetch
        log = []
        try:
            with tempfile.TemporaryDirectory() as d:
                gui.CACHE = Path(d) / "cache"
                files, pages = gui.run_job("Test t@x.com", ["DEMO", "BAD", "DEMO2"], "10-K",
                                           Path(d), lambda t, tag="": log.append((tag, t)))
                self.assertEqual([f.name for f in files][-1], "DebtScope_comparison.xlsx")
                self.assertEqual(len(pages), 2)
                self.assertTrue(all(p.exists() for p in files + pages))
        finally:
            gui.fetch = original
        self.assertTrue(any(tag == "err" and "BAD" in t for tag, t in log))
        self.assertEqual(gui.parse_tickers(" aapl, msft;brk.b "), ["AAPL", "MSFT", "BRK.B"])


if __name__ == "__main__":
    unittest.main()
