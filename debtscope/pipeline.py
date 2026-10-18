"""Orchestration: ticker -> DebtProfile."""

from __future__ import annotations

from .analytics import compute_metrics
from .edgar import EdgarClient, EdgarError, Filing
from .extract import extract_instruments
from .models import DebtProfile
from .xbrl import maturity_schedule, snapshot


def build_profile_from_documents(filing: Filing, html: str, facts: dict | None,
                                 ticker: str = "") -> DebtProfile:
    report_year = int(filing.report_date[:4]) if filing.report_date else None
    instruments, (scale, scale_word), ccy = extract_instruments(html, report_year)
    xbrl = snapshot(facts, filing.accession, filing.report_date) if facts else {}
    sched = maturity_schedule(facts, filing.accession, filing.report_date) if facts else {}
    profile = DebtProfile(
        ticker=ticker.upper(),
        company=filing.company,
        cik=filing.cik,
        form=filing.form,
        accession=filing.accession,
        filing_date=filing.filing_date,
        report_date=filing.report_date,
        url=filing.url,
        scale_label=scale_word,
        reporting_currency=ccy,
        scale=scale,
        instruments=instruments,
        xbrl=xbrl,
        maturity_schedule=sched,
    )
    compute_metrics(profile)
    return profile


def fetch(client: EdgarClient, ticker: str, form: str = "10-K") -> tuple[Filing, str, dict | None]:
    """Download the latest filing document and the company's XBRL facts."""
    cik, _title = client.lookup(ticker)
    filing = client.latest_filing(cik, form)
    html = client.document(filing)
    try:
        facts = client.company_facts(cik)
    except EdgarError:
        facts = None  # some filers (e.g. small/foreign) have no companyfacts
    return filing, html, facts


def build_profile(client: EdgarClient, ticker: str, form: str = "10-K") -> DebtProfile:
    filing, html, facts = fetch(client, ticker, form)
    return build_profile_from_documents(filing, html, facts, ticker)
