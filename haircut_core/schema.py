"""
Canonical schema for the Haircut Margin Calculator.

Every broker file - whatever its shape - is converted into one of two
standard tables before any calculation happens. This module defines those
tables and the field metadata the profiler uses to recognise columns.
"""
from __future__ import annotations

import pandas as pd

# ===== core/schema.py =====
"""
Canonical schema (the contract)
================================
Every broker file - whatever its shape - is converted into one of two
standard tables before any calculation happens. This module defines those
tables and the field metadata the profiler uses to recognise columns.

Standard Portfolio  (one row per holding)
    user_id        str    required   owner of the holding
    isin           str    optional   primary join key when present
    scheme_name    str    required   fallback join key (fuzzy)
    quantity       float  optional
    holding_value  float  required   gross market value in rupees

Standard Haircut master
    isin           str    optional*  join key
    scheme_name    str    optional*  fallback join key
    haircut_pct    float  required   haircut, normalised to 0..100
        (* at least one of isin / scheme_name must be present)
"""


STD_PORTFOLIO_COLS = ["user_id", "isin", "scheme_name", "quantity",
                      "holding_value"]
STD_HAIRCUT_COLS = ["isin", "scheme_name", "haircut_pct"]

ISIN_RE = r"^IN[A-Za-z0-9]{10}$"

# --------------------------------------------------------------------------- #
# Field definitions used by the profiler.
#   aliases     : header-name substrings (lower-case) that suggest this field
#   kind        : how to score cell *content*  (isin | number | percent | text)
#   keywords    : tokens that, if frequently present, hint a text field's role
# --------------------------------------------------------------------------- #

PORTFOLIO_FIELDS = {
    "isin": {
        "aliases": ["isin", "isin code", "isin no", "isin number"],
        "kind": "isin", "required": False,
    },
    "scheme_name": {
        "aliases": ["scheme name", "scheme", "isin name", "scrip name", "scrip",
                    "fund name", "fund", "security name", "security",
                    "instrument", "particulars", "name", "description"],
        "kind": "text",
        "keywords": ["fund", "growth", "plan", "scheme", "etf", "index",
                     "regular", "direct", "idcw", "gold", "equity", "debt"],
        "required": True,
    },
    "quantity": {
        "aliases": ["qty", "quantity", "units", "unit", "balance", "no of units"],
        "kind": "number", "required": False,
    },
    "holding_value": {
        "aliases": ["holding value", "holding", "market value", "mkt value",
                    "mkt val", "current value", "value", "amount", "amt",
                    "valuation", "market val", "closing value", "net value"],
        "kind": "number", "required": True,
    },
    "user_id": {
        "aliases": ["user id", "userid", "user", "client", "client code",
                    "clientcode", "client id", "clientid", "client name",
                    "ucc", "account", "account id", "boid", "dp id"],
        "kind": "text", "required": False,
    },
    "haircut_pct": {   # some portfolio files embed their own haircut
        "aliases": ["haircut", "hair cut", "collateralhaircut", "collateral haircut",
                    "margin %", "hc", "haircut %", "haircut percent"],
        "kind": "percent", "required": False,
    },
}

HAIRCUT_FIELDS = {
    "isin": {
        "aliases": ["isin", "isin code", "isin no"],
        "kind": "isin", "required": False,
    },
    "scheme_name": {
        "aliases": ["scripname", "scrip name", "scheme name", "scheme", "scrip",
                    "fund name", "fund", "security name", "security",
                    "instrument", "particulars", "name"],
        "kind": "text",
        "keywords": ["fund", "growth", "plan", "scheme", "etf", "index"],
        "required": False,
    },
    "haircut_pct": {
        "aliases": ["collateralhaircut", "collateral haircut", "haircut",
                    "hair cut", "haircut %", "margin %", "hc", "haircut percent"],
        "kind": "percent", "required": True,
    },
}

# Row tokens that mark non-holding rows (totals / cash / notes) to be dropped.
JUNK_ROW_TOKENS = [
    "total", "grand total", "sub total", "subtotal", "net total",
    "cash required", "cash available", "ledger", "opening balance",
    "closing balance", "summary", "balance c/f",
    "performance", "exit load", "entry load", "lock-in", "lock in",
]


def empty_portfolio():
    return pd.DataFrame(columns=STD_PORTFOLIO_COLS)


def empty_haircut():
    return pd.DataFrame(columns=STD_HAIRCUT_COLS)
