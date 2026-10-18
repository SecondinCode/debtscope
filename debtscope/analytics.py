"""Credit metrics and reconciliation of extracted bonds vs XBRL totals."""

from __future__ import annotations

from datetime import date

from .models import SOURCE_TABLE, STATUS_EXHIBIT, DebtInstrument, DebtProfile
from .xbrl import AFTER_FIVE


def _maturity_date(inst: DebtInstrument) -> date | None:
    if not inst.maturity_year:
        return None
    # Unknown month/day -> mid-year, flagged in the Excel as an estimate.
    return date(inst.maturity_year, inst.maturity_month or 7, inst.maturity_day or 1)


def years_to_maturity(inst: DebtInstrument, report_date: str) -> float | None:
    d = _maturity_date(inst)
    if d is None:
        return None
    return (d - date.fromisoformat(report_date)).days / 365.25


def covered_by_aggregate(single: DebtInstrument, aggregates: list[DebtInstrument]) -> bool:
    """True if a single note falls inside an aggregated table row (avoid double count).

    A note with its own priced row in a footnote table is a separate line of the
    same table (e.g. another issuance), so it is never treated as covered.
    """
    if any(e.source == SOURCE_TABLE and e.amount is not None for e in single.evidence):
        return False
    for agg in aggregates:
        if agg.amount is None or not single.maturity_year:
            continue
        if not (agg.maturity_year <= single.maturity_year <= (agg.maturity_year_to or agg.maturity_year)):
            continue
        if agg.floating != single.floating:
            continue
        if agg.floating or (
            single.coupon is not None and agg.coupon is not None and agg.coupon_to is not None
            and agg.coupon <= single.coupon <= agg.coupon_to
        ):
            return True
    return False


def outstanding(instruments: list[DebtInstrument]) -> list[DebtInstrument]:
    return [i for i in instruments if i.status == "outstanding"]


def principal_universe(instruments: list[DebtInstrument]) -> list[DebtInstrument]:
    """Instruments whose amounts can be summed without double counting."""
    live = outstanding(instruments)
    aggregates = [i for i in live if i.is_aggregate and i.amount is not None]
    singles = [i for i in live if not i.is_aggregate and i.amount is not None
               and not covered_by_aggregate(i, aggregates)]
    return aggregates + singles


def _ratio(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def compute_metrics(p: DebtProfile) -> None:
    x = {k: f.value for k, f in p.xbrl.items()}
    live = outstanding(p.instruments)
    universe = principal_universe(p.instruments)
    extracted = sum(i.amount for i in universe) if universe else None

    priced_fixed = [i for i in live if not i.is_aggregate and not i.floating
                    and i.coupon is not None and i.amount]
    wac = (sum(i.coupon * i.amount for i in priced_fixed) / sum(i.amount for i in priced_fixed)
           if priced_fixed else None)
    # Fallback for issuers that only give aggregated rows (Apple): weight the
    # effective rate of each aggregated row by its principal.
    agg_rated = [i for i in live if i.is_aggregate and i.amount and i.effective_rate is not None]
    eff = (sum(i.effective_rate * i.amount for i in agg_rated) / sum(i.amount for i in agg_rated)
           if agg_rated else None)
    exhibit_listed = [i for i in p.instruments if i.status == STATUS_EXHIBIT]
    timed = [(years_to_maturity(i, p.report_date), i.amount) for i in live
             if not i.is_aggregate and i.amount and i.maturity_year]
    timed = [(y, a) for y, a in timed if y is not None and y > 0]
    wam = sum(y * a for y, a in timed) / sum(a for _, a in timed) if timed else None

    sched = {b: f.value for b, f in p.maturity_schedule.items()}
    sched_total = sum(sched.values()) if sched else None
    near_term = (sched.get(0, 0) + sched.get(1, 0) + sched.get(2, 0)) if sched else None
    yearly = {b: v for b, v in sched.items() if b != AFTER_FIVE}

    total_debt = x.get("total_debt")
    gross_debt = (total_debt or 0) + (x.get("commercial_paper") or 0) if total_debt else None
    liquid = (x.get("cash") or 0) + (x.get("marketable_securities_current") or 0)
    ebitda = (x["operating_income"] + x["depreciation"]
              if "operating_income" in x and "depreciation" in x else None)
    net_debt = gross_debt - liquid if gross_debt is not None else None

    interest = x.get("interest_expense", x.get("interest_paid"))
    interest_basis = ("expense" if "interest_expense" in x
                      else "cash paid" if "interest_paid" in x else None)

    p.metrics = {
        "instruments_identified": len(p.instruments),
        "instruments_outstanding": len(live),
        "instruments_with_principal": sum(1 for i in live if i.amount),
        "extracted_principal": extracted,
        "xbrl_total_debt": total_debt,
        # Principal vs principal when the XBRL schedule exists (carrying value
        # differs from face by discounts and hedge adjustments).
        "coverage_ratio": _ratio(extracted, sched_total) if sched_total else _ratio(extracted, total_debt),
        "coverage_basis": "scheduled principal (XBRL)" if sched_total else "carrying value (XBRL)",
        "weighted_avg_coupon_pct": wac,
        "weighted_avg_effective_rate_pct": eff,
        "notes_listed_in_exhibits": len(exhibit_listed),
        "weighted_avg_maturity_yrs": wam,
        "commercial_paper": x.get("commercial_paper"),
        "gross_debt": gross_debt,
        "cash_and_st_investments": liquid or None,
        "net_debt": net_debt,
        "ebitda": ebitda,
        "net_debt_to_ebitda": _ratio(net_debt, ebitda),
        "interest_coverage": _ratio(x.get("operating_income"), interest),
        "interest_basis": interest_basis,
        "implied_cost_of_debt_pct": (_ratio(interest, gross_debt) or 0) * 100
                                    if interest and gross_debt else None,
        "scheduled_principal_total": sched_total,
        "due_within_24m_pct": _ratio(near_term, sched_total) * 100 if sched_total else None,
        "peak_year_share_pct": (max(yearly.values()) / sched_total * 100
                                if yearly and sched_total else None),
    }

    # ---- data-quality warnings: say what we could NOT verify
    w = p.warnings
    if not p.xbrl:
        w.append("No XBRL facts for this filing: totals and credit ratios unavailable.")
    cov = p.metrics["coverage_ratio"]
    if cov is not None and cov < 0.6:
        w.append(f"Extracted principal explains only {cov:.0%} of reported debt principal. "
                 "The rest is aggregated in the footnote or disclosed only in exhibits/prospectuses.")
    if cov is not None and cov > 1.15:
        w.append(f"Extracted principal is {cov:.0%} of reported debt principal: possible double count "
                 "or notes repaid during the year still being picked up.")
    base = sched_total or total_debt
    wac_share = (sum(i.amount for i in priced_fixed) / base) if priced_fixed and base else None
    p.metrics["weighted_avg_coupon_share"] = wac_share
    if wac_share is not None and wac_share < 0.5:
        w.append(f"Weighted avg coupon and maturity rest on notes worth only {wac_share:.0%} of "
                 "principal (the rest is reported in aggregated rows): not representative of the whole stack.")
    if (wac is not None and p.metrics["implied_cost_of_debt_pct"] is not None
            and (wac_share or 0) >= 0.5):
        gap = p.metrics["implied_cost_of_debt_pct"] - wac
        if abs(gap) > 1.5:
            w.append(f"Implied cost of debt differs from weighted coupon by {gap:+.1f}pp "
                     "(swaps, FX, floating debt or capitalized interest - check the footnote).")
    if "operating_income" in x and "depreciation" not in x:
        w.append("D&A is not tagged under a standard XBRL element (often a company-specific "
                 "tag): EBITDA and net debt / EBITDA not computed.")
    if p.xbrl and interest is None:
        w.append("Interest expense is not tagged separately in XBRL (often netted inside "
                 "'other income/expense'): interest coverage and cost of debt not computed.")
    elif interest_basis == "cash paid":
        w.append("Interest expense not tagged: coverage and cost of debt use cash interest paid.")
    if exhibit_listed and not priced_fixed:
        w.append(f"{len(exhibit_listed)} notes appear only in the exhibit list (legal documents): "
                 "issued at some point, but current balances are not disclosed bond by bond. "
                 "Some may have been repurchased early.")
    if any(i.currency == "n/d" or i.currency.endswith("*") for i in live):
        w.append("Some bond denominations are assumed (*) or unknown: US issuers often list "
                 "EUR notes on Nasdaq/NYSE. Amounts are still in the reporting currency.")
    if p.scale_label.endswith("(assumed)"):
        w.append("Reporting scale not stated in the document: millions assumed.")
