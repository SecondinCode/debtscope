import json
import tempfile
import time
import unittest
from pathlib import Path

from debtscope.edgar import EdgarClient, EdgarError, RateLimiter
from debtscope.xbrl import maturity_schedule, snapshot

FIX = Path(__file__).parent / "fixtures"
ID = "Test User test@example.com"


class FakeResponse:
    def __init__(self, status, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = 0

    def get(self, url, timeout):
        self.calls += 1
        return self.responses.pop(0)


TICKERS = json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                      "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire"}})


class EdgarClientTests(unittest.TestCase):
    def test_identity_required(self):
        with self.assertRaises(EdgarError):
            EdgarClient("no email here")

    def test_user_agent_set(self):
        s = FakeSession([FakeResponse(200, TICKERS)])
        EdgarClient(ID, session=s)
        self.assertEqual(s.headers["User-Agent"], ID)

    def test_lookup_handles_class_shares(self):
        c = EdgarClient(ID, session=FakeSession([FakeResponse(200, TICKERS)]))
        self.assertEqual(c.lookup("brk.b"), (1067983, "Berkshire"))

    def test_retries_on_429(self):
        s = FakeSession([FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, TICKERS)])
        c = EdgarClient(ID, session=s)
        self.assertEqual(c.lookup("AAPL")[0], 320193)
        self.assertEqual(s.calls, 2)

    def test_disk_cache(self):
        with tempfile.TemporaryDirectory() as d:
            s = FakeSession([FakeResponse(200, TICKERS)])
            c = EdgarClient(ID, cache_dir=d, session=s)
            c.lookup("AAPL")
            c.lookup("AAPL")
            self.assertEqual(s.calls, 1)

    def test_rate_limiter_spacing(self):
        rl = RateLimiter(10)
        t0 = time.monotonic()
        for _ in range(4):
            rl.wait()
        self.assertGreaterEqual(time.monotonic() - t0, 0.29)
        with self.assertRaises(ValueError):
            RateLimiter(11)

    def test_latest_filing(self):
        subs = json.dumps({"name": "Apple Inc.", "filings": {"recent": {
            "form": ["8-K", "10-K", "10-K"], "accessionNumber": ["a", "0000320193-25-000079", "old"],
            "filingDate": ["x", "2025-10-31", "y"], "reportDate": ["", "2025-09-27", ""],
            "primaryDocument": ["", "aapl-20250927.htm", ""]}}})
        c = EdgarClient(ID, session=FakeSession([FakeResponse(200, subs)]))
        f = c.latest_filing(320193)
        self.assertEqual(f.accession, "0000320193-25-000079")
        self.assertTrue(f.url.endswith("/320193/000032019325000079/aapl-20250927.htm"))


class XbrlTests(unittest.TestCase):
    def setUp(self):
        self.facts = json.loads((FIX / "demo_companyfacts.json").read_text(encoding="utf-8"))

    def test_snapshot_picks_this_filing_and_annual_duration(self):
        s = snapshot(self.facts, "0000000000-26-000001", "2025-12-31")
        self.assertEqual(s["total_debt"].value, 7_201e6)           # not the 2024 comparative
        self.assertEqual(s["operating_income"].value, 3_200e6)     # not the Q4 3-month figure
        self.assertEqual(s["shares_outstanding"].value, 1.2e9)     # dei, cover-page date

    def test_schedule(self):
        sch = maturity_schedule(self.facts, "0000000000-26-000001", "2025-12-31")
        self.assertEqual(sum(f.value for f in sch.values()), 7_201e6)
        self.assertEqual(sch[6].value, 1_250e6)


if __name__ == "__main__":
    unittest.main()
