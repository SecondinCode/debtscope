"""Pull the debt-relevant numbers out of the SEC companyfacts XBRL JSON.

companyfacts has no dimensions, so it cannot list individual bonds. It does
give audited totals and the 5-year contractual maturity schedule, which is
what we reconcile the extracted instruments against.
"""

from __future__ import annotations

from datetime import date

from .models import XbrlFact

# Snapshot key -> candidate tags, in priority order (companies differ).
SNAPSHOT_TAGS: dict[str, list[tuple[str, str]]] = {
    "total_debt": [("us-gaap", "LongTermDebt"), ("us-gaap", "DebtInstrumentCarryingAmount"),
                   ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities")],
    "debt_noncurrent": [("us-gaap", "LongTermDebtNoncurrent"),
                        ("us-gaap", "LongTermDebtAndCapitalLeaseObligations")],
    "debt_current": [("us-gaap", "LongTermDebtCurrent"),
                     ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsCurrent")],
    "commercial_paper": [("us-gaap", "CommercialPaper")],
    "face_amount": [("us-gaap", "DebtInstrumentFaceAmount")],
    "cash": [("us-gaap", "CashAndCashEquivalentsAtCarryingValue")],
    "marketable_securities_current": [("us-gaap", "MarketableSecuritiesCurrent"),
                                      ("us-gaap", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
                                      ("us-gaap", "ShortTermInvestments")],
    "operating_income": [("us-gaap", "OperatingIncomeLoss")],
    "interest_expense": [("us-gaap", "InterestExpense"), ("us-gaap", "InterestExpenseNonoperating"),
                         ("us-gaap", "InterestExpenseDebt"), ("us-gaap", "InterestAndDebtExpense")],
    # Fallback when interest expense is netted in "other income" (e.g. Apple).
    "interest_paid": [("us-gaap", "InterestPaidNet"), ("us-gaap", "InterestPaid")],
    "depreciation": [("us-gaap", "DepreciationDepletionAndAmortization"),
                     ("us-gaap", "DepreciationAndAmortization"),
                     ("us-gaap", "DepreciationAmortizationAndAccretionNet")],
    "shares_outstanding": [("dei", "EntityCommonStockSharesOutstanding")],
}

# Maturity schedule tag -> years after fiscal year end (0 = remainder of FY, 10-Q).
SCHEDULE_TAGS: dict[str, int] = {
    "LongTermDebtMaturitiesRepaymentsOfPrincipalRemainderOfFiscalYear": 0,
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths": 1,
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo": 2,
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree": 3,
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour": 4,
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive": 5,
    "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive": 6,  # bucket "6+"
}
AFTER_FIVE = 6

DURATION_KEYS = {"operating_income", "interest_expense", "interest_paid", "depreciation"}


def _candidates(facts: dict, taxonomy: str, tag: str) -> tuple[str, list[dict]]:
    node = facts.get("facts", {}).get(taxonomy, {}).get(tag)
    if not node:
        return "", []
    for unit in ("USD", "shares"):
        if unit in node.get("units", {}):
            return unit, node["units"][unit]
    unit, rows = next(iter(node["units"].items()))
    return unit, rows


def _duration_days(row: dict) -> int | None:
    if "start" not in row:
        return None
    return (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days


def pick(facts: dict, taxonomy: str, tag: str, accession: str, report_date: str,
         annual: bool = False) -> XbrlFact | None:
    """Value reported in *this* filing for *this* period end.

    A 10-K also restates prior years, so we filter on accession and period end.
    For flow items (annual=True) we keep ~12-month durations only.
    Falls back to the same period end reported in any later 10-K/10-Q.
    """
    unit, rows = _candidates(facts, taxonomy, tag)
    if not rows:
        return None

    def ok(r: dict) -> bool:
        if annual:
            d = _duration_days(r)
            return d is not None and 350 <= d <= 380
        return True

    exact = [r for r in rows if r.get("accn") == accession and r.get("end") == report_date and ok(r)]
    if taxonomy == "dei" and not exact:
        # Share counts are dated at the cover-page date, not the period end.
        exact = sorted((r for r in rows if r.get("accn") == accession), key=lambda r: r["end"])[-1:]
    if not exact:
        exact = [r for r in rows if r.get("end") == report_date and ok(r)]
    if not exact:
        return None
    r = exact[-1]
    return XbrlFact(tag=f"{taxonomy}:{tag}", value=float(r["val"]), period_end=r["end"],
                    accession=r.get("accn", ""), form=r.get("form", ""), unit=unit)


def snapshot(facts: dict, accession: str, report_date: str) -> dict[str, XbrlFact]:
    out: dict[str, XbrlFact] = {}
    for key, tags in SNAPSHOT_TAGS.items():
        for taxonomy, tag in tags:
            fact = pick(facts, taxonomy, tag, accession, report_date, annual=key in DURATION_KEYS)
            if fact:
                out[key] = fact
                break
    # Total debt fallback: current + noncurrent (+ commercial paper is NOT
    # added - it is short-term funding, reported separately).
    if "total_debt" not in out and "debt_noncurrent" in out:
        cur = out.get("debt_current")
        val = out["debt_noncurrent"].value + (cur.value if cur else 0.0)
        out["total_debt"] = XbrlFact("derived:current+noncurrent", val, report_date, accession, "")
    return out


def maturity_schedule(facts: dict, accession: str, report_date: str) -> dict[int, XbrlFact]:
    """{bucket: fact} where bucket 1..5 = years after FY end, 6 = thereafter."""
    out: dict[int, XbrlFact] = {}
    for tag, bucket in SCHEDULE_TAGS.items():
        fact = pick(facts, "us-gaap", tag, accession, report_date)
        if fact:
            out[bucket] = fact
    return out
