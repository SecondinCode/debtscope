"""Minimal, polite SEC EDGAR client.

Endpoints used (all public, no API key):
    https://www.sec.gov/files/company_tickers.json            ticker -> CIK
    https://data.sec.gov/submissions/CIK##########.json       filing index
    https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json   XBRL facts
    https://www.sec.gov/Archives/edgar/data/<cik>/<acc>/<doc> filing document

SEC fair-access rules: declare a User-Agent with a contact email and stay at or
below 10 requests/second. This client enforces both, retries on 429/5xx with
exponential backoff and caches responses on disk so reruns hit SEC once.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

DAY = 86_400
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


class EdgarError(RuntimeError):
    """Raised for anything that stops us from getting a filing."""


@dataclass(frozen=True)
class Filing:
    cik: int
    company: str
    form: str
    accession: str          # 0000320193-25-000079
    filing_date: str        # YYYY-MM-DD
    report_date: str        # period of report, YYYY-MM-DD
    primary_document: str

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(
            cik=self.cik, acc=self.accession.replace("-", ""), doc=self.primary_document
        )


class RateLimiter:
    """Spaces calls at least 1/rate seconds apart (thread-safe)."""

    def __init__(self, rate: float):
        if rate <= 0 or rate > 10:
            raise ValueError("SEC allows at most 10 requests/second")
        self.interval = 1.0 / rate
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


class EdgarClient:
    def __init__(
        self,
        identity: str,
        cache_dir: str | Path | None = None,
        rate: float = 8.0,
        session: requests.Session | None = None,
        max_retries: int = 4,
    ):
        if not identity or not EMAIL_RE.search(identity):
            raise EdgarError(
                "SEC requires an identity with a contact email, "
                'e.g. --identity "Mario Rossi mario.rossi@email.com"'
            )
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": identity, "Accept-Encoding": "gzip, deflate"}
        )
        self.limiter = RateLimiter(rate)
        self.max_retries = max_retries
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- transport
    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / hashlib.sha256(url.encode()).hexdigest()[:32]

    def _get(self, url: str, ttl: float | None) -> str:
        """GET with disk cache (ttl=None means cache forever)."""
        path = self._cache_path(url)
        if path and path.exists():
            age = time.time() - path.stat().st_mtime
            if ttl is None or age < ttl:
                return path.read_text(encoding="utf-8")

        delay = 1.0
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                resp = self.session.get(url, timeout=30)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise EdgarError(f"Network error on {url}: {exc}") from exc
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 200:
                text = resp.text
                if path:
                    path.write_text(text, encoding="utf-8")
                return text
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay *= 2
                continue
            if resp.status_code == 403:
                raise EdgarError(
                    "SEC returned 403: check that --identity contains your name and a real email"
                )
            if resp.status_code == 404:
                raise EdgarError(f"Not found on EDGAR: {url}")
            raise EdgarError(f"HTTP {resp.status_code} on {url}")
        raise EdgarError(f"Gave up on {url}")

    def _json(self, url: str, ttl: float | None) -> dict:
        return json.loads(self._get(url, ttl))

    # --------------------------------------------------------------- endpoints
    def lookup(self, ticker: str) -> tuple[int, str]:
        """Return (CIK, company name) for a ticker."""
        data = self._json(TICKERS_URL, ttl=7 * DAY)
        wanted = ticker.upper().replace(".", "-")
        for row in data.values():
            if row["ticker"].upper() == wanted:
                return int(row["cik_str"]), row["title"]
        raise EdgarError(f"Ticker {ticker!r} not found on EDGAR (US-registered issuers only)")

    def submissions(self, cik: int) -> dict:
        return self._json(SUBMISSIONS_URL.format(cik=cik), ttl=DAY)

    def latest_filing(self, cik: int, form: str = "10-K") -> Filing:
        subs = self.submissions(cik)
        recent = subs["filings"]["recent"]
        for i, f in enumerate(recent["form"]):
            if f == form:
                return Filing(
                    cik=cik,
                    company=subs.get("name", ""),
                    form=f,
                    accession=recent["accessionNumber"][i],
                    filing_date=recent["filingDate"][i],
                    report_date=recent["reportDate"][i],
                    primary_document=recent["primaryDocument"][i],
                )
        raise EdgarError(f"No {form} found in recent filings for CIK {cik}")

    def document(self, filing: Filing) -> str:
        return self._get(filing.url, ttl=None)  # archived filings never change

    def company_facts(self, cik: int) -> dict:
        return self._json(FACTS_URL.format(cik=cik), ttl=DAY)
