"""DebtScope - corporate debt structure extraction from SEC EDGAR filings.

Pipeline:
    EDGAR (filing + XBRL facts) -> extraction (cover page, footnote tables,
    narrative text) -> reconciliation against XBRL -> credit metrics -> Excel.
"""

__version__ = "0.1.0"
