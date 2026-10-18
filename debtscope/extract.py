"""Debt instrument extraction from a 10-K / 10-Q / 20-F HTML document.

Three independent passes, each leaving an evidence trail:

1. Cover page   - securities registered under Section 12(b). Gives exact
                  coupons/maturities of exchange-listed notes, never amounts.
2. Tables       - debt footnote rows. The only reliable source of principal.
                  Handles single notes ("3.600% Notes due 2042  1,250") and
                  aggregated rows ("Fixed-rate 0.000%-4.850% notes 2026-2062").
3. Narrative    - sentences such as "issued $4.5 billion of 4.000% notes due
                  2028" or "repaid the 1.625% Notes due 2026".

Results are merged on (coupon, floating, maturity); conflicts are flagged,
never silently resolved.
"""

from __future__ import annotations

import re
from collections import Counter

from bs4 import BeautifulSoup

from .models import (
    SOURCE_COVER,
    SOURCE_EXHIBIT,
    STATUS_EXHIBIT,
    SOURCE_PRIORITY,
    SOURCE_TABLE,
    SOURCE_TEXT,
    DebtInstrument,
    Evidence,
)

MONTHS = [
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
]
_MONTH_ALT = "|".join(MONTHS)
_DEBT_NOUN = r"(?:notes?|bonds?|debentures?)"
_DASH = r"(?:-|–|—|to)"

NOTE_RE = re.compile(
    rf"(?:(?P<coupon>\d{{1,2}}(?:\.\d{{1,4}})?)\s?%|(?P<float>floating[\s-]rate))\s+"
    rf"(?P<desc>(?:[A-Za-z][\w\-]*\s+){{0,4}}?)"
    rf"{_DEBT_NOUN}\s+due\s+"
    rf"(?:(?:in\s+)?(?P<month>{_MONTH_ALT})\s+(?:(?P<day>\d{{1,2}}),?\s+)?)?"
    rf"(?P<year>(?:19|20)\d{{2}})(?!\d)",
    re.I,
)
RANGE_RE = re.compile(
    rf"(?P<c1>\d{{1,2}}(?:\.\d{{1,4}})?)\s?%\s?{_DASH}\s?(?P<c2>\d{{1,2}}(?:\.\d{{1,4}})?)\s?%"
    rf"\s+(?:[\w\-]+\s+){{0,3}}?{_DEBT_NOUN}",
    re.I,
)
FLOAT_AGG_RE = re.compile(rf"floating[\s-]rate\s+(?:[\w\-]+\s+){{0,3}}?{_DEBT_NOUN}", re.I)
YEAR_RANGE_RE = re.compile(rf"\b((?:19|20)\d{{2}})\s?{_DASH}\s?((?:19|20)\d{{2}})\b")
MONEY_RE = re.compile(
    r"(?P<cur>US\$|\$|€|£|¥|USD|EUR|GBP|CHF|JPY|AUD|CAD)\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s*(?P<mult>billion|million|thousand))?",
    re.I,
)
NUM_CELL_RE = re.compile(r"^\(?\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*\)?$")
SCALE_RE = re.compile(r"in\s+(billions|millions|thousands)", re.I)
SCALES = {"billions": 1e9, "millions": 1e6, "thousands": 1e3}
MULTS = {"billion": 1e9, "million": 1e6, "thousand": 1e3}
CURRENCY_HINTS = [
    ("EUR", re.compile(r"€|\beuro", re.I)),
    ("GBP", re.compile(r"£|\bsterling|\bpound", re.I)),
    ("JPY", re.compile(r"¥|\byen\b", re.I)),
    ("CHF", re.compile(r"\bswiss franc|\bCHF\b", re.I)),
    ("AUD", re.compile(r"\baustralian dollar|\bA\$", re.I)),
    ("CAD", re.compile(r"\bcanadian dollar|\bC\$", re.I)),
]
CUR_SYMBOLS = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
REPAY_RE = re.compile(r"\b(repaid|redeemed|retired|extinguished|repurchased|matured)\b", re.I)
ISSUE_RE = re.compile(r"\b(issued|issuance|offering|sold|priced)\b", re.I)
SENT_SPLIT_RE = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")
# Exhibit index rows: legal documents, never balances. Numbers there are exhibit
# numbers ("4.1") and dates, so they must not be read as principal.
EXHIBIT_TABLE_RE = re.compile(r"exhibit\s+(?:number|no\.?|description)|incorporated\s+by\s+reference"
                              r"|filing\s+date", re.I)
EXHIBIT_ROW_RE = re.compile(r"officer.?s\s+certificate|indenture|global\s+note|form\s+of\s+(?:\w+\s+){0,3}notes?"
                            r"|supplemental", re.I)
PCT_CELL_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,4})?)\s?%$")
PCT_RANGE_CELL_RE = re.compile(rf"^(\d{{1,2}}(?:\.\d{{1,4}})?)\s?%(?:\s?{_DASH}\s?(\d{{1,2}}(?:\.\d{{1,4}})?)\s?%)?$")
YEAR_IN_CELL_RE = re.compile(r"\b(?:19|20)\d{2}\b")
MATURITY_CELL_RE = re.compile(rf"^[A-Za-z0-9 ,./–—-]{{4,40}}$")
DATE_CELL_RE = re.compile(rf"(?:({_MONTH_ALT})\s+(\d{{1,2}}),?\s+|(\d{{1,2}})/(\d{{1,2}})/)((?:19|20)\d{{2}})", re.I)
COLUMN_TABLE_RE = re.compile(r"maturit", re.I)


# ------------------------------------------------------------------ helpers
_INVISIBLE = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")


def clean(text: str) -> str:
    """Collapse whitespace and drop invisible characters (zero-width spaces,
    soft hyphens) that filing software sprinkles into table cells."""
    return re.sub(r"\s+", " ", _INVISIBLE.sub("", text.replace("\xa0", " "))).strip()


def load_soup(html: str) -> BeautifulSoup:
    try:
        soup = BeautifulSoup(html, "lxml")          # fast C parser, if installed
    except Exception:  # bs4.FeatureNotFound when lxml is missing
        soup = BeautifulSoup(html, "html.parser")   # pure-Python fallback
    # Inline XBRL keeps a hidden copy of every tagged fact; drop it so it is
    # not double-counted as visible text.
    for tag in soup.find_all(["script", "style", "ix:header"]):
        tag.decompose()
    return soup


def detect_currency(context: str) -> str:
    for code, pattern in CURRENCY_HINTS:
        if pattern.search(context):
            return code
    return "n/d"


DASHES = {"-", "–", "—", "−"}


def parse_number(cell: str) -> float | None:
    """Numeric table cell -> float. A lone dash means nil (0.0); years -> None."""
    cell = cell.replace("$", "").replace(" ", "").strip()
    if cell in DASHES:
        return 0.0
    if not cell or "%" in cell:
        return None
    m = NUM_CELL_RE.match(cell)
    if not m:
        return None
    value = float((m.group(1) + (m.group(2) or "")).replace(",", ""))
    if m.group(2) is None and "," not in m.group(1) and 1900 <= value <= 2100:
        return None  # a bare year, i.e. a maturity column
    return -value if cell.startswith("(") else value


def instrument_from_match(m: re.Match) -> DebtInstrument:
    month = m.group("month")
    desc = clean(m.group("desc") or "")
    return DebtInstrument(
        coupon=float(m.group("coupon")) if m.group("coupon") else None,
        floating=bool(m.group("float")),
        maturity_year=int(m.group("year")),
        maturity_month=MONTHS.index(month.lower()) + 1 if month else None,
        maturity_day=int(m.group("day")) if m.group("day") else None,
        description=desc,
    )


def iter_note_matches(text: str):
    """NOTE_RE matches, minus false positives like '101% of the notes due'."""
    for m in NOTE_RE.finditer(text):
        desc = (m.group("desc") or "").lower().split()
        if desc and desc[0] in {"of", "the", "for", "on", "and", "plus"}:
            continue
        yield m


REPORTING_CCY_RE = re.compile(
    r"in\s+(?:millions|thousands|billions)\s+of\s+(euros|pounds|yen|swiss francs|canadian dollars)"
    r"|\((€|£|¥)\s+in\s+(?:millions|thousands|billions)\)", re.I)
REPORTING_CCY_MAP = {"euros": "EUR", "pounds": "GBP", "yen": "JPY", "swiss francs": "CHF",
                     "canadian dollars": "CAD", "€": "EUR", "£": "GBP", "¥": "JPY"}


def reporting_currency(text: str) -> str:
    """Currency the financial statements are presented in (default USD)."""
    counts = Counter((m.group(1) or m.group(2)).lower() for m in REPORTING_CCY_RE.finditer(text))
    if counts:
        return REPORTING_CCY_MAP[counts.most_common(1)[0][0]]
    return "USD"


def document_scale(text: str) -> tuple[float, str]:
    counts = Counter(m.group(1).lower() for m in SCALE_RE.finditer(text))
    if not counts:
        return 1e6, "millions (assumed)"
    word = counts.most_common(1)[0][0]
    return SCALES[word], word


# ------------------------------------------------------------- pass 1: cover
def cover_text(full_text: str) -> str:
    m = re.search(r"\bPART\s+I\b", full_text)
    end = m.start() if m and m.start() > 500 else 20_000
    return full_text[:end]


def extract_cover(soup: BeautifulSoup, full_text: str) -> list[DebtInstrument]:
    found: list[DebtInstrument] = []
    for table in soup.find_all("table"):
        ttext = clean(table.get_text(" "))
        if not re.search(r"trading\s+symbol|name of each exchange", ttext, re.I):
            continue
        for row in table.find_all("tr"):
            cells = [clean(td.get_text(" ")) for td in row.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            row_text = " ".join(cells)
            for m in iter_note_matches(row_text):
                inst = instrument_from_match(m)
                for c in cells[1:]:
                    if re.fullmatch(r"[A-Z][A-Z0-9.\-/]{0,14}", c):
                        inst.symbol = c
                    elif re.search(r"exchange|market|nyse|nasdaq|bourse", c, re.I):
                        inst.exchange = c
                inst.evidence.append(Evidence(SOURCE_COVER, row_text[:300]))
                found.append(inst)
    if found:
        return found
    # No structured table: fall back to the text right after "Section 12(b)",
    # never the whole document.
    anchor = re.search(r"Section\s+12\(b\)", full_text)
    if not anchor or anchor.start() > 30_000:
        return found
    cover = cover_text(full_text)[anchor.start(): anchor.start() + 3_000]
    for m in iter_note_matches(cover):
        inst = instrument_from_match(m)
        snippet = cover[max(0, m.start() - 40): m.end() + 60]
        inst.evidence.append(Evidence(SOURCE_COVER, snippet))
        found.append(inst)
    return found


# ------------------------------------------------------------ pass 2: tables
def _table_scale(table, default: tuple[float, str]) -> tuple[float, str]:
    nearby = " ".join(table.find_all_previous(string=True, limit=60))
    for source in (table.get_text(" "), nearby):
        m = SCALE_RE.search(source)
        if m:
            word = m.group(1).lower()
            return SCALES[word], word
    return default


def extract_tables(soup: BeautifulSoup, default_scale: tuple[float, str],
                   reporting_ccy: str = "USD") -> list[DebtInstrument]:
    found: list[DebtInstrument] = []
    for table in soup.find_all("table"):
        ttext = clean(table.get_text(" "))
        if re.search(r"trading\s+symbol|name of each exchange", ttext, re.I):
            continue  # cover page, handled in pass 1
        if not re.search(rf"%|floating", ttext, re.I) or not re.search(
                rf"{_DEBT_NOUN}|issuance|face value|\bdebt\b", ttext, re.I):
            continue
        column_table = bool(COLUMN_TABLE_RE.search(ttext)) and "rate" in ttext.lower()
        exhibit_table = bool(EXHIBIT_TABLE_RE.search(ttext))
        scale, scale_word = _table_scale(table, default_scale)
        # Only the line right above the table counts as its caption
        # (e.g. "Euro-denominated notes:"); wider context leaks other currencies.
        prev = [t for t in table.find_all_previous(string=True, limit=4) if t.strip()]
        caption = clean(prev[0])[-200:] if prev else ""
        table_denom = detect_currency(caption)
        for row in table.find_all("tr"):
            cells = [clean(td.get_text(" ")) for td in row.find_all(["td", "th"])]
            cells = [c for c in cells if c and c not in {"$", "€", "£"}]
            cells = glue_cells(cells)
            if not cells:
                continue
            row_text = " ".join(cells)
            # Keep dashes as 0 so column positions survive ("—" = nil this year).
            numbers = [n for n in (parse_number(c) for c in cells[1:]) if n is not None]
            amount = numbers[0] * scale if numbers else None
            note = f"scale: {scale_word}; first numeric column taken as current period"
            row_denom = detect_currency(row_text)
            denom = row_denom if row_denom != "n/d" else (
                table_denom if table_denom != "n/d" else reporting_ccy + "*")

            singles = list(iter_note_matches(row_text))
            if singles and (exhibit_table or EXHIBIT_ROW_RE.search(row_text)):
                for m in singles:
                    inst = instrument_from_match(m)
                    inst.currency = row_denom if row_denom != "n/d" else reporting_ccy + "*"
                    inst.evidence.append(Evidence(SOURCE_EXHIBIT, row_text[:300], None,
                                                  "legal document listed as exhibit"))
                    found.append(inst)
                continue
            if singles:
                for m in singles:
                    inst = instrument_from_match(m)
                    inst.currency = denom
                    # Several notes in one row: the amount is not attributable.
                    amt = amount if len(singles) == 1 else None
                    inst.evidence.append(Evidence(SOURCE_TABLE, row_text[:300], amt, note, reporting_ccy))
                    found.append(inst)
                continue

            rng = RANGE_RE.search(row_text)
            flt = FLOAT_AGG_RE.search(row_text)
            years = YEAR_RANGE_RE.search(row_text)
            if (rng or flt) and years:
                inst = DebtInstrument(
                    coupon=float(rng.group("c1")) if rng else None,
                    coupon_to=float(rng.group("c2")) if rng else None,
                    floating=bool(flt and not rng),
                    maturity_year=int(years.group(1)),
                    maturity_year_to=int(years.group(2)),
                    description="aggregate row",
                    currency=denom,
                )
                # Effective rate column: a lone percentage after the description.
                pcts = [c for c in cells[1:] if "%" in c]
                if pcts:
                    m_pct = PCT_CELL_RE.match(pcts[0].replace(" ", ""))
                    if m_pct:
                        inst.effective_rate = float(m_pct.group(1))
                    inst.effective_rate_text = pcts[0]
                inst.evidence.append(Evidence(SOURCE_TABLE, row_text[:300], amount, note, reporting_ccy))
                found.append(inst)
                continue

            if column_table:
                inst = _column_row(cells, scale)
                if inst:
                    inst.currency = denom
                    inst.evidence.append(Evidence(
                        SOURCE_TABLE, row_text[:300], inst.amount,
                        f"scale: {scale_word}; columns read as maturity / stated rate / effective rate / amount",
                        reporting_ccy))
                    inst.amount = None  # set by merge() from the evidence
                    found.append(inst)
    return found


_PCT_ONLY = re.compile(r"^\d{1,2}(?:\.\d{1,4})?\s?%$")
_PCT_SUFFIX = re.compile(r"^%\)?(?:\s?\([a-z0-9]\))?$")   # "%", "%)", "% (a)"
_FOOTNOTE = re.compile(r"\s?\([a-z]\)$")
_YEAR_ONLY = re.compile(r"^(?:19|20)\d{2}$")


def glue_cells(cells: list[str]) -> list[str]:
    """Rebuild values that filings split across cells.

    "2.40" "%"           -> "2.40%"
    "2.40%" "-" "3.95%"  -> "2.40%-3.95%"   (rate range)
    "2026" "-" "2056"    -> "2026-2056"     (maturity range)
    A dash between two amounts is a nil value, not a range, and is left alone.
    """
    out: list[str] = []
    for c in cells:
        if out and (_PCT_SUFFIX.match(c) or c == ")"):
            out[-1] += "%" if c.startswith("%") else ""
        else:
            # "4.50%(c)" -> "4.50%": footnote letters are not part of the value
            out.append(_FOOTNOTE.sub("", c) if "%" in c else c)
    glued: list[str] = []
    i = 0
    while i < len(out):
        if (glued and out[i] in DASHES and i + 1 < len(out)
                and ((_PCT_ONLY.match(glued[-1]) and _PCT_ONLY.match(out[i + 1]))
                     or (_YEAR_ONLY.match(glued[-1]) and _YEAR_ONLY.match(out[i + 1])))):
            glued[-1] = f"{glued[-1]}–{out[i + 1]}"
            i += 2
            continue
        glued.append(out[i])
        i += 1
    return glued


_RATE_TXT = re.compile(
    rf"(\d{{1,2}}(?:\.\d{{1,4}})?)\s?%\)?(?:\s?\([a-z]\))?"
    rf"(?:\s?{_DASH}\s?(\d{{1,2}}(?:\.\d{{1,4}})?)\s?%\)?(?:\s?\([a-z]\))?)?")
_MAT_TXT = re.compile(
    rf"(?:(?P<mon>{_MONTH_ALT})\s+(?P<day>\d{{1,2}}),?\s+|(?P<m>\d{{1,2}})/(?P<d>\d{{1,2}})/)?"
    rf"(?P<y1>(?:19|20)\d{{2}})(?:\s?{_DASH}\s?(?:(?:{_MONTH_ALT})\s+\d{{1,2}},?\s+|\d{{1,2}}/\d{{1,2}}/)?"
    rf"(?P<y2>(?:19|20)\d{{2}}))?", re.I)


def _column_row(cells: list[str], scale: float) -> DebtInstrument | None:
    """Row of a debt table laid out in columns (issuance label, maturity, stated
    rate, effective rate, amount...), e.g. "2016 issuance | 2026-2056 |
    2.40%-3.95% | 2.46%-4.03% | 6,000".

    Parsed on the joined text of the value cells, so it does not matter how the
    filing splits "2.40", "%", "-", "3.95", "%" across cells.
    """
    desc = cells[0]
    if re.match(r"total|less|unamortized|hedge|current portion|long-term debt", desc, re.I):
        return None
    text = " ".join(cells[1:])
    rates = list(_RATE_TXT.finditer(text))
    if not rates:
        return None
    mat = _MAT_TXT.search(text[: rates[0].start()])
    if not mat:
        return None
    tail = text[rates[-1].end():].split()
    amounts = [a for a in (parse_number(t) for t in tail) if a is not None]
    if not amounts:
        return None

    month = day = None
    if mat.group("mon"):
        month, day = MONTHS.index(mat.group("mon").lower()) + 1, int(mat.group("day"))
    elif mat.group("m"):
        month, day = int(mat.group("m")), int(mat.group("d"))
    y2 = int(mat.group("y2")) if mat.group("y2") else None
    stated = rates[0]
    inst = DebtInstrument(
        coupon=float(stated.group(1)),
        coupon_to=float(stated.group(2)) if stated.group(2) else None,
        floating=False,
        maturity_year=int(mat.group("y1")),
        maturity_year_to=y2 if y2 and y2 != int(mat.group("y1")) else None,
        maturity_month=month if not y2 else None,
        maturity_day=day if not y2 else None,
        description=desc[:80],
        amount=amounts[0] * scale,
    )
    if len(rates) > 1:
        eff = rates[1]
        inst.effective_rate = None if eff.group(2) else float(eff.group(1))
        inst.effective_rate_text = eff.group(0)
    return inst


# --------------------------------------------------------- pass 3: narrative
def extract_narrative(full_text: str) -> list[DebtInstrument]:
    found: list[DebtInstrument] = []
    for sentence in SENT_SPLIT_RE.split(full_text):
        if "%" not in sentence and "floating" not in sentence.lower():
            continue
        notes = list(iter_note_matches(sentence))
        if not notes:
            continue
        monies = list(MONEY_RE.finditer(sentence))
        repay = bool(REPAY_RE.search(sentence))
        issue = bool(ISSUE_RE.search(sentence))
        prev_end = 0
        for m in notes:
            # Closest amount between the previous note and this one, within 80 chars.
            cand = [x for x in monies if prev_end <= x.start() < m.start() and m.start() - x.end() <= 80]
            amount, currency = None, "n/d"
            if cand:
                x = cand[-1]
                amount = float(x.group("num").replace(",", "")) * MULTS.get((x.group("mult") or "").lower(), 1)
                cur = x.group("cur").upper()
                currency = CUR_SYMBOLS.get(x.group("cur"), cur if cur.isalpha() else "n/d")
            prev_end = m.end()
            inst = instrument_from_match(m)
            inst.currency = currency if currency != "n/d" else detect_currency(sentence)
            tag = "repayment" if repay and not issue else "issuance" if issue else "mention"
            inst.evidence.append(Evidence(SOURCE_TEXT, clean(sentence)[:400], amount, tag,
                                          currency if amount is not None else ""))
            found.append(inst)
    return found


# -------------------------------------------------------------------- merge
def _denomination_rank(code: str) -> int:
    if code == "n/d":
        return 2
    return 1 if code.endswith("*") else 0   # explicit beats assumed beats unknown


def merge(candidates: list[DebtInstrument], report_year: int | None,
          reporting_ccy: str = "USD") -> list[DebtInstrument]:
    merged: dict[tuple, DebtInstrument] = {}
    for c in candidates:
        cur = merged.get(c.key)
        if cur is None:
            merged[c.key] = c
            continue
        cur.evidence.extend(c.evidence)
        cur.symbol = cur.symbol or c.symbol
        cur.exchange = cur.exchange or c.exchange
        cur.maturity_month = cur.maturity_month or c.maturity_month
        cur.maturity_day = cur.maturity_day or c.maturity_day
        if _denomination_rank(c.currency) < _denomination_rank(cur.currency):
            cur.currency = c.currency
        cur.effective_rate = cur.effective_rate if cur.effective_rate is not None else c.effective_rate
        cur.effective_rate_text = cur.effective_rate_text or c.effective_rate_text
        if len(c.description) > len(cur.description):
            cur.description = c.description

    # Tables often round coupons to 2 decimals (2.92%) while the cover page and
    # exhibits give 3 (2.921%): treat them as the same note.
    def precision(x: float) -> int:
        return len(f"{x:.4f}".rstrip("0").split(".")[1])

    items = list(merged.values())
    for a in items:
        if a.is_aggregate or a.coupon is None or a.key not in merged:
            continue
        for b in items:
            if (b is a or b.is_aggregate or b.coupon is None or b.key not in merged
                    or b.maturity_year != a.maturity_year or b.floating != a.floating):
                continue
            if abs(a.coupon - b.coupon) <= 0.0051 and min(precision(a.coupon), precision(b.coupon)) <= 2:
                keep, drop = (a, b) if precision(a.coupon) >= precision(b.coupon) else (b, a)
                keep.evidence.extend(drop.evidence)
                keep.symbol = keep.symbol or drop.symbol
                keep.exchange = keep.exchange or drop.exchange
                if keep.effective_rate is None:
                    keep.effective_rate = drop.effective_rate
                if _denomination_rank(drop.currency) < _denomination_rank(keep.currency):
                    keep.currency = drop.currency
                del merged[drop.key]
                if drop is a:
                    break

    for inst in merged.values():
        # Principal is reported in the statements' currency; amounts quoted in
        # another currency (e.g. EUR 800m at issuance) are kept as evidence only.
        priced = sorted(
            (e for e in inst.evidence
             if e.amount is not None and e.note != "repayment" and e.currency == reporting_ccy),
            key=lambda e: SOURCE_PRIORITY.get(e.source, 9),
        )
        if priced:
            best = priced[0]
            inst.amount = best.amount
            same = {round(e.amount, -3) for e in priced if e.source == best.source}
            if len(same) > 1:
                inst.flags.append("different amounts in the same source type - first kept")
            if best.amount and any(e.source != best.source and abs(e.amount - best.amount) > 0.01 * abs(best.amount)
                                   for e in priced):
                inst.flags.append("table vs narrative amount differ (>1%)")
        elif inst.sources and inst.sources[0] == SOURCE_COVER:
            inst.flags.append("exchange-listed (cover page); principal not in 10-K text")
        elif set(inst.sources) != {SOURCE_EXHIBIT}:
            inst.flags.append("principal not disclosed in filing")
        foreign = [e for e in inst.evidence if e.amount is not None and e.currency
                   and e.currency != reporting_ccy]
        if foreign:
            e = foreign[0]
            inst.flags.append(f"issued as {e.currency} {e.amount / 1e6:,.0f}m (FX vs reported amount)")

        if inst.amount is None and set(inst.sources) == {SOURCE_EXHIBIT}:
            inst.status = STATUS_EXHIBIT
            inst.flags.append("only in the exhibit list: issued, current balance not disclosed")
        if inst.amount == 0:
            inst.status = "redeemed"
            inst.flags.append("nil balance in current period")
        elif any(e.note == "repayment" for e in inst.evidence):
            inst.status = "possibly redeemed"
            inst.flags.append("mentioned as repaid/redeemed - verify")
        if (report_year and inst.maturity_year and not inst.is_aggregate
                and inst.maturity_year < report_year and inst.status in ("outstanding", STATUS_EXHIBIT)):
            inst.status = "matured"
            inst.flags.append("maturity before fiscal year end")
        elif (report_year and inst.maturity_year == report_year and inst.status == STATUS_EXHIBIT):
            # Due in the fiscal-year-end year, month unknown: may already be repaid.
            inst.status = "possibly matured"
            inst.flags.append("matures in the fiscal-year-end year; may already be repaid")
        if inst.currency == "n/d":
            inst.flags.append("currency not stated near the instrument")

    def sort_key(i: DebtInstrument):
        return (i.is_aggregate, i.maturity_year or 9999, i.coupon or 0)

    return sorted(merged.values(), key=sort_key)


def extract_instruments(html: str, report_year: int | None = None):
    """Run all passes. Returns (instruments, (scale, scale_word), reporting_currency)."""
    soup = load_soup(html)
    full_text = clean(soup.get_text(" "))
    scale = document_scale(full_text)
    ccy = reporting_currency(full_text)
    candidates = extract_cover(soup, full_text) + extract_tables(soup, scale, ccy)
    # Narrative pass must not see table cells, otherwise the amount of one row
    # would be paired with the note on the next row.
    for table in soup.find_all("table"):
        table.decompose()
    prose = clean(soup.get_text(" "))
    candidates += extract_narrative(prose)
    return merge(candidates, report_year, ccy), scale, ccy
