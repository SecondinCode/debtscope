"""Visual crawler: the original filing with every finding highlighted, and a
spider that walks the document spinning threads to each hit.

The filing keeps its own layout. Python does the hard part (finding and
wrapping matches in <mark> tags, linking bond mentions to the extracted debt
stack); the injected JavaScript only draws.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bs4 import NavigableString

from .extract import (
    FLOAT_AGG_RE,
    MONTHS,
    NOTE_RE,
    RANGE_RE,
    load_soup,
)
from .models import DebtInstrument, DebtProfile

_M = "|".join(m.capitalize() for m in MONTHS)

# Order = priority when two categories overlap on the same characters.
CATEGORIES: list[tuple[str, str, str, list[re.Pattern]]] = [
    ("debt", "Debt & financing", "#D6336C", [
        NOTE_RE, RANGE_RE, FLOAT_AGG_RE,
        re.compile(r"\b(?:commercial paper|term debt|credit facilit(?:y|ies)|revolving credit"
                   r"(?: facility)?|senior (?:unsecured )?notes|indentures?|debt securities)\b", re.I),
    ]),
    ("shares", "Shares & equity", "#7048E8", [
        re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\s+shares\b", re.I),
        re.compile(r"\$\s?0\.\d+\s+par value", re.I),
        re.compile(r"\b(?:share repurchases?|repurchased? \w+ shares|dividends? per share|common stock)\b", re.I),
    ]),
    ("money", "Amounts", "#2B8A3E", [
        re.compile(r"(?:US\$|\$|€|£)\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?(?:\s*(?:billion|million|thousand))?"),
    ]),
    ("rates", "Rates & percentages", "#E8590C", [
        re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%"),
    ]),
    ("dates", "Dates & periods", "#1971C2", [
        re.compile(rf"\b(?:{_M})\s+\d{{1,2}},\s+\d{{4}}\b"),
        re.compile(r"\bfiscal (?:year|quarter)s?\s+\d{4}\b", re.I),
    ]),
    ("legal", "Filing references", "#0B7285", [
        re.compile(r"\bSection\s+\d+[A-Z]?(?:\([a-z0-9]+\))*"),
        re.compile(r"\bRule\s+\d+[a-z]?-\d+"),
        re.compile(r"\bForm\s+(?:10-K|10-Q|8-K|20-F|S-\d)\b"),
        re.compile(r"\b(?:The Nasdaq Stock Market LLC|New York Stock Exchange)\b"),
        re.compile(r"\b\d{2}-\d{7}\b"),
    ]),
    ("risk", "Risk signals", "#C92A2A", [
        re.compile(r"\b(?:covenants?|default(?:s|ed)?|downgrades?|credit ratings?|refinanc\w*|"
                   r"liquidity|impairments?|going concern|material weakness(?:es)?|litigation)\b", re.I),
    ]),
]
SKIP_PARENTS = {"script", "style", "title", "head", "mark", "noscript"}

# Inline XBRL: every reported number sits in <ix:nonFraction name="us-gaap:...">.
# The concept name says exactly what the number is, so we classify on it.
_IX_DEBT = re.compile(r"Debt|NotesPayable|CommercialPaper|Borrowing|LineOfCredit|InterestExpense"
                      r"|InterestPaid|LongTermDebt|DebtInstrument|SeniorNotes", re.I)
_IX_SHARES = re.compile(r"Shares|StockRepurchase|StockIssued|Dividend|EarningsPerShare|TreasuryStock", re.I)
_IX_RATE = re.compile(r"Rate|Percentage|Percent|Ratio|Yield", re.I)


def classify_concept(name: str, unit: str) -> str:
    local = name.split(":")[-1]
    if _IX_DEBT.search(local):
        return "debt"
    if _IX_SHARES.search(local) or unit.lower() in {"shares", "usdpershare"}:
        return "shares"
    if _IX_RATE.search(local) or unit.lower() in {"pure", "number", "percent"}:
        return "rates"
    return "money"


def annotate_xbrl(soup, counts: dict[str, int]) -> None:
    """Highlight every tagged number, keeping its XBRL concept as a tooltip."""
    for tag in soup.find_all(re.compile(r"^ix:nonfraction$", re.I)):
        name = tag.get("name", "")
        if not name or not tag.get_text(strip=True):
            continue
        cat = classify_concept(name, tag.get("unitref", ""))
        mark = soup.new_tag("mark", attrs={"class": "ds", "data-c": cat, "data-x": name,
                                           "title": f"{name} (XBRL)"})
        for child in list(tag.contents):
            mark.append(child.extract())
        tag.append(mark)
        counts[cat] += 1


def bond_key(coupon: float | None, floating: bool, year: int | None) -> str:
    rate = "FRN" if floating or coupon is None else f"{coupon:.3f}"
    return f"{rate}|{year}"


def instrument_key(i: DebtInstrument) -> str:
    return "" if i.is_aggregate else bond_key(i.coupon, i.floating, i.maturity_year)


def _spans(text: str) -> list[tuple[int, int, str, str]]:
    """Non-overlapping (start, end, category, bond_key) spans in one text node."""
    taken: list[tuple[int, int, str, str]] = []
    for cat, _label, _colour, patterns in CATEGORIES:
        for pat in patterns:
            for m in pat.finditer(text):
                s, e = m.span()
                if e - s < 2 or any(s < te and ts < e for ts, te, _, _ in taken):
                    continue
                key = ""
                if pat is NOTE_RE:
                    c = m.group("coupon")
                    key = bond_key(float(c) if c else None, bool(m.group("float")), int(m.group("year")))
                taken.append((s, e, cat, key))
    return sorted(taken)


def annotate(soup) -> dict[str, int]:
    """Wrap every finding in <mark class="ds" data-c=... data-k=...>. Returns counts."""
    counts = {c[0]: 0 for c in CATEGORIES}
    annotate_xbrl(soup, counts)
    nodes = [s for s in soup.find_all(string=True)
             if type(s) is NavigableString and s.strip()
             and not any(p.name in SKIP_PARENTS for p in s.parents if p.name)]
    for node in nodes:
        text = str(node)
        spans = _spans(text)
        if not spans:
            continue
        pieces = []
        pos = 0
        for s, e, cat, key in spans:
            if s > pos:
                pieces.append(NavigableString(text[pos:s]))
            mark = soup.new_tag("mark", attrs={"class": "ds", "data-c": cat})
            if key:
                mark["data-k"] = key
            mark.string = text[s:e]
            pieces.append(mark)
            counts[cat] += 1
            pos = e
        if pos < len(text):
            pieces.append(NavigableString(text[pos:]))
        node.replace_with(*pieces)
    return counts


def _payload(p: DebtProfile, counts: dict[str, int]) -> dict:
    m = p.metrics
    names = {0: "Rest of FY", 1: "Year 1", 2: "Year 2", 3: "Year 3", 4: "Year 4", 5: "Year 5",
             6: "Later"}
    fy = int(p.report_date[:4])
    schedule = [{"label": f"FY{fy + b}" if 0 < b < 6 else names[b], "mm": f.value / 1e6, "bucket": b}
                for b, f in sorted(p.maturity_schedule.items())]
    insts = [{
        "label": i.label, "key": instrument_key(i), "status": i.status,
        "mm": None if i.amount is None else i.amount / 1e6, "ccy": i.currency,
        "year": i.maturity_year, "aggregate": i.is_aggregate,
        "sources": i.sources, "flags": i.flags,
    } for i in p.instruments if i.status != "matured"]
    return {
        "company": p.company, "ticker": p.ticker, "form": p.form, "period": p.report_date, "fy": fy,
        "url": p.url, "ccy": p.reporting_currency,
        "categories": [{"id": c, "label": label, "color": col, "count": counts[c]}
                       for c, label, col, _ in CATEGORIES],
        "metrics": {k: m.get(k) for k in (
            "xbrl_total_debt", "net_debt", "net_debt_to_ebitda", "interest_coverage",
            "coverage_ratio", "weighted_avg_coupon_pct", "weighted_avg_effective_rate_pct",
            "due_within_24m_pct")},
        "schedule": schedule,
        "instruments": insts,
        "warnings": p.warnings,
    }


WEB = Path(__file__).parent / "web"


def _asset(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def write_viewer(html: str, profile: DebtProfile, path: str | Path) -> Path:
    soup = load_soup(html)
    counts = annotate(soup)
    if soup.html is None:
        wrapper = soup.new_tag("html")
        wrapper.append(soup.new_tag("head"))
        body = soup.new_tag("body")
        for child in list(soup.contents):
            body.append(child.extract())
        wrapper.append(body)
        soup.append(wrapper)
    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        soup.html.insert(0, head)
    if soup.body is None:
        body = soup.new_tag("body")
        for child in [c for c in soup.html.contents if c is not head]:
            body.append(child.extract())
        soup.html.append(body)

    base_dir = profile.url.rsplit("/", 1)[0] + "/"
    head.insert(0, soup.new_tag("meta", attrs={"charset": "utf-8"}))
    # Make relative image paths absolute (a <base> tag would break the filing's
    # internal #anchor links, e.g. the table of contents).
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and not re.match(r"^(?:https?:|data:|//)", src):
            img["src"] = base_dir + src.lstrip("./")
    head.append(soup.new_tag("link", attrs={
        "rel": "stylesheet",
        "href": "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600"
                "&family=IBM+Plex+Sans:wght@400;500;600&display=swap"}))
    style = soup.new_tag("style")
    style.string = _asset("crawler.css")
    head.append(style)
    for old in head.find_all("title"):
        old.decompose()                       # keep a single, descriptive tab title
    title = soup.new_tag("title")
    title.string = f"{profile.ticker or profile.company} {profile.form} - DebtScope crawler"
    head.append(title)

    data = json.dumps(_payload(profile, counts), default=str).replace("</", "<\\/")
    from bs4 import BeautifulSoup
    panel = BeautifulSoup(_asset("panel.html"), "html.parser")
    soup.body.append(panel)
    s1 = soup.new_tag("script", attrs={"id": "ds-data", "type": "application/json"})
    s1.string = data
    soup.body.append(s1)
    # Presentation layer only: spider animation, dashboard strip, panel logic.
    for name in ("spider.js", "dashboard.js", "crawler.js"):
        script = soup.new_tag("script")
        script.string = _asset(name)
        soup.body.append(script)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<!DOCTYPE html>\n" + str(soup), encoding="utf-8")
    return path


