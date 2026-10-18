"""Command line entry point.

    python -m debtscope AAPL
    python -m debtscope AAPL MSFT ORCL --out reports
    python -m debtscope AAPL --form 10-Q --json
    python -m debtscope --offline filing.htm companyfacts.json --period 2025-09-27
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .edgar import EdgarClient, EdgarError, Filing
from .excel import write_comparison, write_workbook
from .models import DebtProfile
from .pipeline import build_profile_from_documents, fetch
from .viewer import write_viewer

if os.name == "nt":          # enable ANSI colours in the Windows console
    os.system("")

BOLD, DIM, RED, GREEN, CYAN, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[36m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = DIM = RED = GREEN = CYAN = RESET = ""


def _fmt(v, kind: str) -> str:
    if v is None:
        return "n/a"
    return {
        "mm": lambda: f"{v / 1e6:,.0f}",
        "x": lambda: f"{v:.1f}x",
        "pct": lambda: f"{v:.1%}",
        "pct100": lambda: f"{v:.2f}%",
        "yrs": lambda: f"{v:.1f} yrs",
    }.get(kind, lambda: str(v))()


def print_profile(p: DebtProfile) -> None:
    m = p.metrics
    ccy = p.reporting_currency
    print(f"\n{BOLD}{p.company} ({p.ticker}){RESET}  {DIM}{p.form} period {p.report_date}, "
          f"filed {p.filing_date}{RESET}")
    print(f"{DIM}{p.url}{RESET}\n")
    rows = [
        (f"Total term debt ({ccy} mm)", _fmt(m["xbrl_total_debt"], "mm")),
        (f"Net debt ({ccy} mm)", _fmt(m["net_debt"], "mm")),
        ("Net debt / EBITDA", _fmt(m["net_debt_to_ebitda"], "x")),
        ("Interest coverage", _fmt(m["interest_coverage"], "x")),
        ("Weighted avg coupon", _fmt(m["weighted_avg_coupon_pct"], "pct100")),
        ("Weighted avg maturity", _fmt(m["weighted_avg_maturity_yrs"], "yrs")),
        ("Due within 24 months", _fmt(m["due_within_24m_pct"], "pct100")),
        ("Coverage of total debt", _fmt(m["coverage_ratio"], "pct")),
    ]
    for k, v in rows:
        print(f"  {k:<28}{CYAN}{v:>14}{RESET}")
    print(f"\n  {BOLD}{'Instrument':<48}{'Principal':>12}  {'Ccy':<5}{'Status':<18}Sources{RESET}")
    for i in p.instruments:
        amt = "-" if i.amount is None else f"{i.amount / 1e6:,.0f}"
        colour = GREEN if i.status == "outstanding" else RED
        src = ", ".join(s.replace("_", " ") for s in i.sources)
        print(f"  {i.label:<48}{amt:>12}  {i.currency:<5}{colour}{i.status:<18}{RESET}{DIM}{src}{RESET}")
    for w in p.warnings:
        print(f"  {RED}! {w}{RESET}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="debtscope",
        description="Extract corporate debt structure and maturity wall from SEC EDGAR filings.")
    ap.add_argument("tickers", nargs="*", help="US tickers, e.g. AAPL MSFT")
    ap.add_argument("--form", default="10-K", help="10-K (default), 10-Q or 20-F")
    ap.add_argument("--identity", default=os.environ.get("DEBTSCOPE_IDENTITY"),
                    help='"Name email" required by SEC (or set DEBTSCOPE_IDENTITY)')
    ap.add_argument("--out", default="reports", help="output folder (default: reports)")
    ap.add_argument("--cache", default=str(Path.home() / ".debtscope_cache"),
                    help="cache folder for SEC responses")
    ap.add_argument("--json", action="store_true", help="also write the raw profile as JSON")
    ap.add_argument("--no-viewer", action="store_true", help="skip the visual crawler HTML page")
    ap.add_argument("--offline", nargs=2, metavar=("HTML", "FACTS_JSON"),
                    help="parse local files instead of downloading")
    ap.add_argument("--period", help="period end YYYY-MM-DD (offline mode)")
    ap.add_argument("--accession", default="", help="accession number (offline mode)")
    ap.add_argument("--version", action="version", version=f"debtscope {__version__}")
    args = ap.parse_args(argv)

    out = Path(args.out)
    profiles: list[DebtProfile] = []
    documents: dict[int, str] = {}   # id(profile) -> filing HTML, for the crawler page

    if args.offline:
        if not args.period:
            ap.error("--offline needs --period YYYY-MM-DD")
        html = Path(args.offline[0]).read_text(encoding="utf-8", errors="replace")
        facts = json.loads(Path(args.offline[1]).read_text(encoding="utf-8"))
        ticker = (args.tickers or [""])[0]
        filing = Filing(int(facts.get("cik", 0)), facts.get("entityName", ticker), args.form,
                        args.accession, args.period, args.period, Path(args.offline[0]).name)
        profiles.append(build_profile_from_documents(filing, html, facts, ticker))
        documents[id(profiles[-1])] = html
    else:
        if not args.tickers:
            ap.error("give at least one ticker, e.g. python -m debtscope AAPL")
        try:
            client = EdgarClient(args.identity, cache_dir=args.cache)
        except EdgarError as exc:
            print(f"{RED}{exc}{RESET}")
            return 2
        for t in args.tickers:
            try:
                print(f"{DIM}Fetching {t.upper()} {args.form} from EDGAR...{RESET}")
                filing, html, facts = fetch(client, t, args.form)
                profiles.append(build_profile_from_documents(filing, html, facts, t))
                documents[id(profiles[-1])] = html
            except EdgarError as exc:
                print(f"{RED}{t.upper()}: {exc}{RESET}")

    for p in profiles:
        print_profile(p)
        name = f"DebtScope_{p.ticker or p.cik}_{p.form}_{p.report_date}"
        path = write_workbook(p, out / f"{name}.xlsx")
        print(f"\n  {GREEN}Excel report:{RESET} {path.resolve()}")
        if not args.no_viewer:
            page = write_viewer(documents[id(p)], p, out / f"{name}_crawler.html")
            print(f"  {GREEN}Visual crawler:{RESET} {page.resolve()}")
        if args.json:
            jpath = out / f"{name}.json"
            jpath.write_text(json.dumps(asdict(p), indent=2, default=str), encoding="utf-8")
            print(f"  {GREEN}JSON:{RESET} {jpath.resolve()}")

    if len(profiles) > 1:
        cpath = write_comparison(profiles, out / "DebtScope_comparison.xlsx")
        print(f"\n{GREEN}Comparison:{RESET} {cpath.resolve()}")
    return 0 if profiles else 1


if __name__ == "__main__":
    sys.exit(main())
