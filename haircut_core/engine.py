"""
Haircut margin engine: join holdings to a haircut master and compute margin.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

from .ingest import normalize_scheme

# ===== core/haircut_engine.py =====
"""
Haircut Margin Engine
=====================
Calculation core. Consumes the *Standard Format* produced by the ingestion
pipeline (so it is broker-agnostic) and computes haircut + available margin.

Holdings are matched to haircut records by:
    1. ISIN          (exact, primary)
    2. scheme name   (exact-normalised, then fuzzy)   <- for files without ISIN
Unmatched holdings follow `missing_policy` ('zero' or 'fallback').
"""






@dataclass
class CalcResult:
    detail: pd.DataFrame
    user_summary: pd.DataFrame
    totals: dict
    missing_isin: pd.DataFrame       # holdings matched to nothing
    duplicate_isin: pd.DataFrame     # conflicting duplicate master records
    scheme_matches: pd.DataFrame     # holdings matched by name (for review)
    n_dup_rows: int = 0


# --------------------------------------------------------------------------- #
# Calculation
# --------------------------------------------------------------------------- #

# Peak memory for the score matrix is BLOCK x len(master) x 4 bytes, so it
# stays flat (~18 MB against a 9k-row master) however large the portfolio.
_CDIST_BLOCK = 512


def _best_fuzzy(queries: list[str], choices: list[str]):
    """Yield (query, best_choice, best_score) for every query.

    One vectorised `process.cdist` per block - the comparison runs in C
    across all available threads - instead of a Python-level `extractOne`
    per holding. Ties resolve to the first match, as `extractOne` did.
    """
    for start in range(0, len(queries), _CDIST_BLOCK):
        block = queries[start:start + _CDIST_BLOCK]
        mat = process.cdist(block, choices, scorer=fuzz.token_set_ratio,
                            dtype=np.float32, workers=-1)
        best_idx = mat.argmax(axis=1)
        for row, (query, j) in enumerate(zip(block, best_idx)):
            yield query, choices[j], float(mat[row, j])


def compute(holdings: pd.DataFrame, haircut: pd.DataFrame,
            missing_policy: str = "zero",
            scheme_threshold: float = 88.0) -> CalcResult:
    if holdings is None or holdings.empty:
        empty = pd.DataFrame()
        return CalcResult(empty, empty, _empty_totals(), empty, empty, empty)

    h = holdings.copy().reset_index(drop=True)
    h["isin"] = h["isin"].astype("string").fillna("").str.strip()
    h["isin"] = h["isin"].mask(h["isin"].str.lower().isin(["nan", "none"]), "")
    h["scheme_name"] = h["scheme_name"].astype("string").fillna("").str.strip()
    if "pf_haircut_pct" not in h.columns:
        h["pf_haircut_pct"] = np.nan

    m = haircut.copy()
    m["isin"] = m["isin"].astype("string").fillna("").str.strip()
    m["scheme_name"] = m["scheme_name"].astype("string").fillna("").str.strip()
    m["haircut_pct"] = pd.to_numeric(m["haircut_pct"], errors="coerce")
    m = m[m["haircut_pct"].notna()]

    # ----- ISIN lookup (dedup, detect conflicts) -----
    m_isin = m[m["isin"] != ""]
    dup_rows = m_isin[m_isin["isin"].duplicated(keep=False)]
    n_dup_rows = int(m_isin["isin"].duplicated(keep="first").sum())
    duplicate_isin = pd.DataFrame()
    if not dup_rows.empty:
        conflict = dup_rows.groupby("isin")["haircut_pct"].nunique()
        conflict_isins = conflict[conflict > 1].index
        if len(conflict_isins):
            duplicate_isin = (dup_rows[dup_rows["isin"].isin(conflict_isins)]
                              .sort_values("isin").reset_index(drop=True))
    m_isin = m_isin.drop_duplicates("isin", keep="first")
    isin_pct = dict(zip(m_isin["isin"], m_isin["haircut_pct"]))
    isin_name = dict(zip(m_isin["isin"], m_isin["scheme_name"]))

    # ----- scheme lookup (normalised) -----
    scheme_pct, scheme_orig = {}, {}
    for nm, pct in zip(m["scheme_name"], m["haircut_pct"]):
        key = normalize_scheme(nm)
        if key and key not in scheme_pct:
            scheme_pct[key] = pct
            scheme_orig[key] = nm
    scheme_keys = list(scheme_pct.keys())

    # ----- vectorised ISIN match (writable copies) -----
    pct = np.array(h["isin"].map(isin_pct), dtype=float)
    src = np.where(~np.isnan(pct), "isin", "missing").astype(object)
    score = np.where(~np.isnan(pct), 100.0, np.nan).astype(float)
    mname = np.array(h["isin"].map(isin_name).fillna(""), dtype=object)

    # ----- scheme match for the rest -----
    # Exact (normalised) hits are a dict lookup. What is left goes through
    # ONE batched comparison per *unique* scheme name, rather than a
    # per-holding process.extractOne against the whole master - portfolios
    # repeat the same funds across clients, so the unique set is far
    # smaller than the row count.
    need = np.where(np.isnan(pct) & (h["scheme_name"] != ""))[0]

    fuzzy_rows: dict[str, list[int]] = {}
    for i in need:
        key = normalize_scheme(h.at[i, "scheme_name"])
        if not key:
            continue
        if key in scheme_pct:
            pct[i] = scheme_pct[key]; src[i] = "scheme_exact"
            score[i] = 100.0; mname[i] = scheme_orig[key]
        else:
            fuzzy_rows.setdefault(key, []).append(i)

    if fuzzy_rows and scheme_keys:
        for key, best_key, best_score in _best_fuzzy(list(fuzzy_rows),
                                                     scheme_keys):
            rows = fuzzy_rows[key]
            if best_score >= scheme_threshold:
                for i in rows:
                    pct[i] = scheme_pct[best_key]; src[i] = "scheme_fuzzy"
                    score[i] = best_score; mname[i] = scheme_orig[best_key]
            else:
                for i in rows:
                    score[i] = best_score

    # ----- missing policy -----
    missing_mask = np.isnan(pct)
    if missing_policy == "fallback":
        pf = pd.to_numeric(h["pf_haircut_pct"], errors="coerce").to_numpy(float)
        use_pf = missing_mask & ~np.isnan(pf)
        pct = np.where(use_pf, pf, pct)
        src = np.where(use_pf, "portfolio_fallback", src)
        missing_mask = np.isnan(pct)
    pct = np.where(np.isnan(pct), 0.0, pct)

    d = h.copy()
    d["scrip_name"] = mname
    d["haircut_pct"] = pct
    d["match_score"] = score
    d["haircut_source"] = src
    d["haircut_amount"] = d["holding_value"] * d["haircut_pct"] / 100.0
    d["available_margin"] = d["holding_value"] - d["haircut_amount"]

    detail = d[[
        "user_id", "isin", "scheme_name", "scrip_name", "quantity",
        "holding_value", "haircut_pct", "haircut_amount", "available_margin",
        "haircut_source", "match_score",
    ]].rename(columns={"scheme_name": "scheme", "quantity": "qty"})

    # ----- aggregate -----
    grp = detail.groupby("user_id", sort=False)
    user_summary = grp.agg(
        holdings=("isin", "size"),
        portfolio_value=("holding_value", "sum"),
        haircut_value=("haircut_amount", "sum"),
        available_margin=("available_margin", "sum"),
    ).reset_index()
    user_summary["haircut_pct"] = np.where(
        user_summary["portfolio_value"] > 0,
        user_summary["haircut_value"] / user_summary["portfolio_value"] * 100, 0.0)
    user_summary = user_summary.sort_values(
        "available_margin", ascending=False).reset_index(drop=True)

    totals = {
        "n_users": int(user_summary["user_id"].nunique()),
        "n_holdings": int(len(detail)),
        "portfolio_value": float(detail["holding_value"].sum()),
        "haircut_value": float(detail["haircut_amount"].sum()),
        "available_margin": float(detail["available_margin"].sum()),
        "n_missing": int((detail["haircut_source"] == "missing").sum()),
        "n_scheme_matched": int(detail["haircut_source"]
                                .isin(["scheme_exact", "scheme_fuzzy"]).sum()),
    }

    missing = detail[detail["haircut_source"] == "missing"][
        ["user_id", "isin", "scheme", "holding_value"]].reset_index(drop=True)
    scheme_matches = detail[detail["haircut_source"]
                            .isin(["scheme_exact", "scheme_fuzzy"])][
        ["user_id", "scheme", "scrip_name", "match_score", "haircut_pct",
         "haircut_source"]].reset_index(drop=True)

    return CalcResult(detail=detail, user_summary=user_summary, totals=totals,
                      missing_isin=missing, duplicate_isin=duplicate_isin,
                      scheme_matches=scheme_matches, n_dup_rows=n_dup_rows)


def _empty_totals() -> dict:
    return {"n_users": 0, "n_holdings": 0, "portfolio_value": 0.0,
            "haircut_value": 0.0, "available_margin": 0.0, "n_missing": 0,
            "n_scheme_matched": 0}


# --------------------------------------------------------------------------- #
# Validation (on Standard Format + ingest reports)
# --------------------------------------------------------------------------- #

def validate_portfolio(df: pd.DataFrame, n_dropped: int = 0) -> list:
    checks = []
    ok = (not df.empty)
    checks.append(("User ID present", ok and (df["user_id"].astype(str)
                   .str.len() > 0).all(), f"{df['user_id'].nunique() if ok else 0} users"))
    has_key = ok and ((df["isin"].astype(str).str.len() > 0) |
                      (df["scheme_name"].astype(str).str.len() > 0)).all()
    checks.append(("ISIN or scheme present", has_key,
                   f"{(df['isin'].astype(str).str.len()>0).sum() if ok else 0} with ISIN"))
    checks.append(("Holding Value present",
                   ok and df["holding_value"].notna().all(),
                   f"{len(df)} holdings"))
    checks.append(("No blank/junk rows kept", n_dropped == 0,
                   f"{n_dropped} non-holding rows dropped"))
    return checks


def validate_haircut(df: pd.DataFrame, n_dup_rows: int = 0,
                     n_conflict: int = 0) -> list:
    checks = []
    ok = (not df.empty)
    n_isin = int((df["isin"].astype(str).str.len() > 0).sum()) if ok else 0
    checks.append(("ISIN or scheme present", ok and
                   ((df["isin"].astype(str).str.len() > 0) |
                    (df["scheme_name"].astype(str).str.len() > 0)).all(),
                   f"{n_isin:,} rows with ISIN"))
    checks.append(("Haircut % present",
                   ok and df["haircut_pct"].notna().all(),
                   "all rows have a haircut %"))
    checks.append(("Duplicate ISINs", n_conflict == 0,
                   f"{n_dup_rows} duplicate row(s) kept & calculated normally"
                   + (f"; {n_conflict} conflicting" if n_conflict else "")))
    return checks


# --------------------------------------------------------------------------- #
# Indian-format currency helpers
# --------------------------------------------------------------------------- #

def inr_compact(x: float) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e7:
        return f"{sign}{x / 1e7:,.2f} Cr"
    if x >= 1e5:
        return f"{sign}{x / 1e5:,.2f} L"
    return f"{sign}{x:,.0f}"


def inr_full(x: float) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if x < 0 else ""
    x = abs(x)
    whole = int(round(x))
    s = str(whole)
    if len(s) <= 3:
        grouped = s
    else:
        last3 = s[-3:]
        rest = s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts) + "," + last3
    return f"{sign}{grouped}"
