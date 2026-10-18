"""Data model shared by every stage of the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field

# Where a piece of evidence came from. Order = trust when amounts conflict.
SOURCE_TABLE = "footnote_table"     # debt footnote table row (has principal)
SOURCE_TEXT = "narrative"           # sentence in MD&A / notes ("issued $X of ...")
SOURCE_COVER = "cover_page"         # Section 12(b) listed securities
SOURCE_EXHIBIT = "exhibit_index"    # Item 15 exhibit list (indentures, officer's certificates)
SOURCE_PRIORITY = {SOURCE_TABLE: 0, SOURCE_TEXT: 1, SOURCE_COVER: 2, SOURCE_EXHIBIT: 3}
STATUS_EXHIBIT = "listed in exhibits"   # issued at some point; current balance not disclosed


@dataclass
class Evidence:
    source: str
    snippet: str
    amount: float | None = None     # absolute units of `currency`
    note: str = ""
    currency: str = ""              # currency of `amount` (tables: reporting ccy)


@dataclass
class DebtInstrument:
    coupon: float | None            # percent, e.g. 3.6; None for floating
    floating: bool
    maturity_year: int | None
    maturity_year_to: int | None = None   # set for aggregated rows ("2026-2062")
    coupon_to: float | None = None        # set for aggregated rows ("0.000%-4.850%")
    maturity_month: int | None = None
    maturity_day: int | None = None
    description: str = ""
    currency: str = "n/d"           # denomination of the bond; "USD*" = assumed
    amount: float | None = None     # principal in reporting currency, absolute units
    symbol: str = ""
    exchange: str = ""
    status: str = "outstanding"     # outstanding | redeemed | matured | listed in exhibits
    effective_rate: float | None = None   # percent, from aggregated footnote rows
    effective_rate_text: str = ""         # raw text when the filing gives a range
    evidence: list[Evidence] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def is_aggregate(self) -> bool:
        return self.maturity_year_to is not None or self.coupon_to is not None

    @property
    def key(self) -> tuple:
        return (
            None if self.coupon is None else round(self.coupon, 3),
            self.floating,
            self.maturity_year,
            self.coupon_to,
            self.maturity_year_to,
            # Two issuances can share the same rate and maturity ranges
            # ("2023 issuance" and "2024 issuance"): keep them apart.
            self.description if self.is_aggregate else "",
        )

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for ev in sorted(self.evidence, key=lambda e: SOURCE_PRIORITY.get(e.source, 9)):
            if ev.source not in seen:
                seen.append(ev.source)
        return seen

    @property
    def label(self) -> str:
        if self.floating:
            rate = "Floating-rate"
        elif self.coupon_to is not None:
            rate = f"{self.coupon:.3f}%–{self.coupon_to:.3f}%"
        elif self.coupon is not None:
            rate = f"{self.coupon:.3f}%"
        else:
            rate = "n/d"
        if self.maturity_year_to:
            due = f"due {self.maturity_year}–{self.maturity_year_to}"
        elif self.maturity_year:
            due = f"due {self.maturity_year}"
        else:
            due = ""
        kind = "notes (aggregate)" if self.is_aggregate else "Notes"
        label = f"{rate} {kind} {due}".strip()
        if self.is_aggregate and self.description and self.description != "aggregate row":
            label = f"{self.description}: {rate} notes {due}".strip()
        return label


@dataclass
class XbrlFact:
    tag: str
    value: float
    period_end: str
    accession: str
    form: str
    unit: str = "USD"


@dataclass
class DebtProfile:
    ticker: str
    company: str
    cik: int
    form: str
    accession: str
    filing_date: str
    report_date: str
    url: str
    scale_label: str                        # e.g. "USD millions" (detected)
    scale: float                            # multiplier to reach units
    reporting_currency: str                 # currency of the financial statements
    instruments: list[DebtInstrument]
    xbrl: dict[str, XbrlFact]               # snapshot: key -> fact
    maturity_schedule: dict[int, XbrlFact]  # bucket (1..5, 6=thereafter) -> fact
    metrics: dict[str, float | None] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
