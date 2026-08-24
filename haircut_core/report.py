"""
Export: a calculation result -> a multi-sheet Excel workbook.
"""
from __future__ import annotations

import io

import pandas as pd

XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")

_SUMMARY_LABELS = {
    "user_id": "User ID", "holdings": "Holdings",
    "portfolio_value": "Portfolio Value", "haircut_value": "Haircut Value",
    "available_margin": "Available Margin", "haircut_pct": "Haircut %",
}
_DETAIL_LABELS = {
    "user_id": "User ID", "isin": "ISIN", "scheme": "Scheme",
    "scrip_name": "Matched Scrip", "qty": "Qty",
    "holding_value": "Holding Value", "haircut_pct": "Haircut %",
    "haircut_amount": "Haircut Amount", "available_margin": "Available Margin",
    "haircut_source": "Match Source", "match_score": "Match Score",
}


def build_excel(calc) -> bytes:
    """The full result set as an .xlsx workbook, one sheet per view."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        calc.user_summary.rename(columns=_SUMMARY_LABELS).to_excel(
            xw, sheet_name="User Summary", index=False)
        calc.detail.rename(columns=_DETAIL_LABELS).to_excel(
            xw, sheet_name="Detailed Holdings", index=False)
        if not calc.scheme_matches.empty:
            calc.scheme_matches.to_excel(
                xw, sheet_name="Scheme Matches", index=False)
        if not calc.missing_isin.empty:
            calc.missing_isin.to_excel(xw, sheet_name="Unmatched", index=False)
        if not calc.duplicate_isin.empty:
            calc.duplicate_isin.to_excel(
                xw, sheet_name="Duplicate ISIN", index=False)
    return buf.getvalue()
