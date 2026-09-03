"""
Ingestion: read any broker file into grids, profile its columns, and
normalise it into the canonical schema.

Combines what were four modules in the original build (parse, scheme_match,
reader, profiler, normalizer) into one cohesive unit - they are always used
together and share the same helpers.
"""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

from . import schema
from .schema import ISIN_RE

# ===== core/ingest/parse.py =====
"""
Value parsing helpers
=====================
Robust parsing of the messy numbers brokers use: Indian digit grouping
(20,00,000), crore/lakh suffixes (2.50cr, 22.5L), rupee symbols, brackets
for negatives, and percentages written many ways.
"""





class UnsupportedFile(Exception):
    """A file the app cannot read, with a message meant for the end user."""


_ISIN = re.compile(ISIN_RE)
_NUM = re.compile(r"[-+]?\d*\.?\d+")


def clean_text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, float) and pd.isna(cell):
        return ""
    return str(cell).strip()


def looks_like_isin(cell) -> bool:
    s = clean_text(cell).replace(" ", "")
    return bool(_ISIN.match(s))


def parse_amount(cell):
    """Parse a rupee amount to float. Returns None if not a number."""
    if cell is None:
        return None
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return None if pd.isna(cell) else float(cell)
    s = clean_text(cell)
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    s = s.replace("₹", "").replace("rs.", "").replace("rs", "").replace(",", "")
    s = s.replace("inr", "").strip().lower()
    mult = 1.0
    if s.endswith("cr") or s.endswith("crore") or s.endswith("crores"):
        mult = 1e7
        s = re.sub(r"(cr|crore|crores)$", "", s)
    elif s.endswith("l") or s.endswith("lakh") or s.endswith("lac") or s.endswith("lakhs"):
        mult = 1e5
        s = re.sub(r"(lakhs|lakh|lac|l)$", "", s)
    elif s.endswith("k"):
        mult = 1e3
        s = s[:-1]
    s = s.strip()
    m = _NUM.search(s)
    if not m:
        return None
    try:
        val = float(m.group()) * mult
    except ValueError:
        return None
    return -val if neg else val


def parse_percent_raw(cell):
    """
    Parse a haircut/percent cell to its raw numeric value (NOT normalised).
    '9%' -> 9.0 ; '0.09' -> 0.09 ; 9 -> 9.0 ; returns None if not numeric.
    Column-level fraction-vs-percent normalisation happens in the normalizer.
    """
    if cell is None:
        return None
    if isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        return None if pd.isna(cell) else float(cell)
    s = clean_text(cell)
    if not s:
        return None
    s = s.replace("%", "").replace(",", "").strip()
    m = _NUM.search(s)
    if not m:
        return None
    try:
        v = float(m.group())
    except ValueError:
        return None
    return v

# ===== core/ingest/scheme_match.py =====
"""
Scheme-name matching
=====================
When a portfolio has no ISIN (or an ISIN that isn't in the master), we match
holdings to haircut records by *scheme name*. Names are messy and broker-
specific ("ABSL MF-A GR" vs "Aditya Birla Sun Life Multi Cap Fund - Growth"),
so we normalise aggressively, expand AMC abbreviations, then fuzzy-match.
"""




# Common AMC / fund-house abbreviations -> canonical token.
_AMC_ALIASES = {
    "absl": "aditya birla sun life", "abslf": "aditya birla sun life",
    "hdfc": "hdfc", "icici": "icici prudential", "icicipru": "icici prudential",
    "sbi": "sbi", "uti": "uti", "kotak": "kotak", "axis": "axis",
    "dsp": "dsp", "idfc": "bandhan", "bandhan": "bandhan", "nippon": "nippon india",
    "mirae": "mirae asset", "franklin": "franklin", "invesco": "invesco india",
    "edelweiss": "edelweiss", "hsbc": "hsbc", "canara": "canara robeco",
    "canararobeco": "canara robeco", "tata": "tata", "ppfas": "parag parikh",
    "motilal": "motilal oswal", "quant": "quant", "sundaram": "sundaram",
}

# Noise tokens stripped before matching (plan/option words, not identity).
_NOISE = {
    "growth", "regular", "reg", "plan", "direct", "dir", "option", "opt",
    "idcw", "dividend", "payout", "reinvestment", "reinvest", "fund", "scheme",
    "the", "of", "and", "mf", "gr", "g", "-", "wholesale", "retail",
}

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_MULTISPACE = re.compile(r"\s+")


def normalize_scheme(name) -> str:
    """Lower-case, drop punctuation, expand AMC, strip plan/option noise."""
    if name is None:
        return ""
    return _normalize_scheme_cached(str(name))


@lru_cache(maxsize=200_000)
def _normalize_scheme_cached(name: str) -> str:
    """Memoised core of `normalize_scheme`.

    Portfolios repeat the same fund across many clients and the haircut master
    is re-normalised on every calculation, so the same strings arrive over and
    over. Caching turns that into a dict lookup.
    """
    s = name.lower().strip()
    if not s:
        return ""
    s = _NON_ALNUM.sub(" ", s)
    s = _MULTISPACE.sub(" ", s).strip()
    tokens = []
    for tok in s.split():
        tok = _AMC_ALIASES.get(tok, tok)
        tokens.append(tok)
    s = " ".join(tokens)
    # second pass: remove noise tokens but keep at least something
    kept = [t for t in s.split() if t not in _NOISE]
    return " ".join(kept) if kept else s


def build_choices(names) -> dict[str, str]:
    """Map normalised-name -> first original name (dedup, drop blanks)."""
    choices: dict[str, str] = {}
    for n in names:
        key = normalize_scheme(n)
        if key and key not in choices:
            choices[key] = n
    return choices


def match_one(query, choices: dict[str, str], threshold: float = 88.0):
    """
    Fuzzy-match a single scheme name against the choice keys.

    Returns (matched_original_name, score) or (None, best_score).
    """
    q = normalize_scheme(query)
    if not q or not choices:
        return None, 0.0
    if q in choices:                       # exact normalised hit
        return choices[q], 100.0
    best = process.extractOne(q, list(choices.keys()),
                              scorer=fuzz.token_set_ratio)
    if best is None:
        return None, 0.0
    key, score, _ = best
    if score >= threshold:
        return choices[key], float(score)
    return None, float(score)

# ===== core/ingest/reader.py =====
"""
Reader
======
Turns ANY input file (xlsx / xls / csv / pdf) into a uniform list of
`Grid` objects - a Grid is just a header-less rectangular table of raw cell
values (a pandas DataFrame with integer columns) plus a little metadata.

Everything downstream (profiler, normalizer) works on Grids, so it never
needs to know whether the data came from Excel, CSV or a PDF.
"""





@dataclass
class Grid:
    data: pd.DataFrame      # header=None style: integer columns, raw values
    name: str               # sheet name / page label / file stem
    source_kind: str        # 'excel' | 'csv' | 'pdf'
    origin: str = ""        # original filename


def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower().lstrip(".")


def read_grids(src, filename: str = "") -> list[Grid]:
    """
    Parameters
    ----------
    src : path str | bytes | file-like
    filename : original name (used to pick the parser and as a fallback label)
    """
    kind = _ext(filename)
    if not kind and isinstance(src, str):
        kind = _ext(src)

    raw = _to_bytes(src)
    stem = os.path.splitext(os.path.basename(filename or
                                             (src if isinstance(src, str) else "upload")))[0]

    if kind in ("xlsx", "xls", "xlsm", "xlsb"):
        return _read_excel(raw, stem, filename)
    if kind == "csv":
        return _read_csv(raw, stem, filename)
    if kind == "pdf":
        return _read_pdf(raw, stem, filename)

    # Unknown extension: sniff. PDFs start with %PDF.
    if raw[:5] == b"%PDF-":
        return _read_pdf(raw, stem, filename)
    try:
        return _read_excel(raw, stem, filename)
    except Exception:
        return _read_csv(raw, stem, filename)


def _to_bytes(src) -> bytes:
    if isinstance(src, bytes):
        return src
    if isinstance(src, str):
        with open(src, "rb") as fh:
            return fh.read()
    if hasattr(src, "getvalue"):
        return src.getvalue()
    if hasattr(src, "read"):
        return src.read()
    raise TypeError(f"Unsupported source type: {type(src)!r}")


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #

_ENGINE_PACKAGES = {"xls": "xlrd", "xlsb": "pyxlsb"}


def _read_excel(raw: bytes, stem: str, filename: str) -> list[Grid]:
    try:
        xl = pd.ExcelFile(io.BytesIO(raw))
    except (ImportError, ValueError) as e:
        # pandas reports a missing reader engine as ValueError ("format cannot
        # be determined"), not only as ImportError, so both land here.
        kind = _ext(filename)
        pkg = _ENGINE_PACKAGES.get(kind)
        if pkg:
            raise UnsupportedFile(
                f"Reading a {'.' + kind} file needs the '{pkg}' package. It is "
                f"listed in requirements.txt - if this appears on a deployed "
                f"app, the install did not pick it up. Re-saving the file as "
                f".xlsx also works."
            ) from e
        raise UnsupportedFile(
            f"This does not look like a spreadsheet this app can read "
            f"({type(e).__name__}). Re-save it as .xlsx or .csv."
        ) from e
    grids = []
    for sheet in xl.sheet_names:
        df = xl.parse(sheet_name=sheet, header=None, dtype=object)
        if df.dropna(how="all").empty:
            continue
        grids.append(Grid(_reset(df), str(sheet), "excel", filename))
    return grids


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #

def _read_csv(raw: bytes, stem: str, filename: str) -> list[Grid]:
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    # Sniff the delimiter (comma / semicolon / tab / pipe).
    sep = _sniff_sep(text)
    df = pd.read_csv(io.StringIO(text), header=None, dtype=object,
                     sep=sep, engine="python", skip_blank_lines=False)
    return [Grid(_reset(df), stem or "csv", "csv", filename)]


def _sniff_sep(text: str) -> str:
    head = "\n".join(text.splitlines()[:20])
    candidates = {",": head.count(","), ";": head.count(";"),
                  "\t": head.count("\t"), "|": head.count("|")}
    sep = max(candidates, key=candidates.get)
    return sep if candidates[sep] > 0 else ","


# --------------------------------------------------------------------------- #
# PDF  (pdfplumber - pip-only, no system dependencies)
# --------------------------------------------------------------------------- #

def _read_pdf(raw: bytes, stem: str, filename: str) -> list[Grid]:
    try:
        import pdfplumber
    except ImportError as e:
        raise UnsupportedFile(
            "PDF files need the 'pdfplumber' package, which is not installed. "
            "Add 'pdfplumber' to requirements.txt, or upload the statement as "
            "an Excel or CSV file instead."
        ) from e

    grids: list[Grid] = []
    try:
        pdf_doc = pdfplumber.open(io.BytesIO(raw))
    except Exception as e:
        raise UnsupportedFile(
            "This PDF could not be opened - it may be damaged, encrypted or "
            "password protected. Try re-exporting it, or upload the statement "
            "as Excel or CSV."
        ) from e
    with pdf_doc as pdf:
        # A PDF with no text at all is a scan - a picture of a statement.
        # Nothing can read it, and guessing produces numbers that look real,
        # so it is refused rather than parsed.
        if not any(page.chars for page in pdf.pages):
            raise UnsupportedFile(
                "This PDF has no text in it - it is a scan or a photograph of "
                "a statement, so the figures cannot be read. Ask for the "
                "original statement file, or upload it as Excel or CSV.")

        # Holdings-aware extraction: anchors rows on ISINs and identifies the
        # value column arithmetically, which handles layouts nothing knows in
        # advance. Falls through to the generic reader if it finds nothing.
        try:
            from . import pdf_reader
            holdings = pdf_reader.extract_holdings(pdf)
        except Exception:
            holdings = None
        if holdings is not None and len(holdings) > 1:
            return [Grid(_reset(holdings), "holdings", "pdf", filename)]

        for pno, page in enumerate(pdf.pages, start=1):
            page_rows: list[list] = []
            # 1) try ruled / lattice tables first
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            for tbl in tables:
                page_rows.extend(tbl)
            # 2) fall back to text lines split on runs of whitespace
            if not page_rows:
                page_rows = _pdf_text_rows(page)
            if not page_rows:
                continue
            width = max(len(r) for r in page_rows)
            norm = [list(r) + [None] * (width - len(r)) for r in page_rows]
            df = pd.DataFrame(norm)
            if df.dropna(how="all").empty:
                continue
            grids.append(Grid(_reset(df), f"page {pno}", "pdf", filename))
    return grids


def _pdf_text_rows(page) -> list[list]:
    """Group words into rows by their y-position, then into cells by x-gaps."""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    if not words:
        return []
    # bucket words into lines by rounded top coordinate
    lines: dict[int, list] = {}
    for w in words:
        key = round(w["top"] / 3.0)
        lines.setdefault(key, []).append(w)
    rows = []
    for key in sorted(lines):
        ws = sorted(lines[key], key=lambda x: x["x0"])
        cells, cur, last_x1 = [], [], None
        for w in ws:
            if last_x1 is not None and (w["x0"] - last_x1) > 18:  # column gap
                cells.append(" ".join(cur))
                cur = []
            cur.append(w["text"])
            last_x1 = w["x1"]
        if cur:
            cells.append(" ".join(cur))
        rows.append(cells)
    return rows


def _reset(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reset_index(drop=True)
    df.columns = range(df.shape[1])
    # Treat empty / whitespace-only strings as missing so that structural
    # detection (single-cell title/marker rows, blank rows) works uniformly
    # across Excel (NaN) and PDF/CSV (often "").
    df = df.map(lambda v: np.nan if isinstance(v, str) and v.strip() == "" else v)
    return df

# ===== core/ingest/profiler.py =====
"""
Profiler
========
Looks at a raw Grid and works out *what it is*:
  - which row is the header (if any),
  - which column is which canonical field (with a confidence score),
  - how each row should be treated (header / data / section / junk / marker /
    title / blank),
  - where the User ID comes from (a column, stacked block markers, a title,
    or a single owner).

Output is a `FieldMap` that the normalizer consumes. Nothing here mutates
values - it only *describes* the grid, so a human can review/override it.
"""





_CATEGORY_RE = re.compile(r"(funds?|name|category|section|scrips?|holdings?)\s*$",
                          re.I)


@dataclass
class FieldMap:
    columns: dict          # field -> column index
    confidence: dict       # field -> 0..1
    header_row: int | None
    row_class: list        # per-row label
    user_mode: str         # 'column' | 'marker' | 'title' | 'single'
    user_col: int | None = None
    title_user: str | None = None
    headerless: bool = False
    notes: list = field(default_factory=list)


def _norm_header(s) -> str:
    return re.sub(r"\s+", " ", clean_text(s).lower()).strip()


# --------------------------------------------------------------------------- #
# Header detection
# --------------------------------------------------------------------------- #

def _alias_hits(row_vals, fields) -> int:
    hits = 0
    for v in row_vals:
        n = _norm_header(v)
        if not n:
            continue
        for spec in fields.values():
            if any(n == a or a in n.split() or n in a for a in spec["aliases"]):
                hits += 1
                break
    return hits


def _find_header_row(df: pd.DataFrame, fields, max_scan=30):
    best_row, best_hits = None, 0
    for i in range(min(max_scan, len(df))):
        hits = _alias_hits(df.iloc[i].tolist(), fields)
        if hits > best_hits:
            best_hits, best_row = hits, i
    return (best_row, best_hits) if best_hits >= 2 else (None, best_hits)


# --------------------------------------------------------------------------- #
# Column scoring
# --------------------------------------------------------------------------- #

def _header_col_field(header_cell, fields):
    """Best (field, score) for a header cell by alias match."""
    n = _norm_header(header_cell)
    if not n:
        return None, 0.0
    best_f, best_s = None, 0.0
    for fname, spec in fields.items():
        for a in spec["aliases"]:
            if n == a:
                s = 1.0
            elif a in n.split() or (" " in a and a in n):
                s = 0.85
            elif n in a or a in n:
                s = 0.7
            else:
                s = fuzz.token_set_ratio(n, a) / 100.0 * 0.6
            if s > best_s:
                best_s, best_f = s, fname
    return (best_f, best_s) if best_s >= 0.55 else (None, best_s)


def _content_scores(col_vals, fields):
    """Score a column's *content* for each field kind. Returns {field: score}."""
    vals = [v for v in col_vals if clean_text(v)]
    if not vals:
        return {}
    n = len(vals)
    isin_frac = sum(looks_like_isin(v) for v in vals) / n
    num_vals = [parse_amount(v) for v in vals]
    num_frac = sum(x is not None for x in num_vals) / n
    big_num_frac = sum(x is not None and abs(x) >= 1000 for x in num_vals) / n
    pct_vals = [parse_percent_raw(v) for v in vals]
    pct_in_range = sum(x is not None and 0 < x <= 100 for x in pct_vals) / n
    text_frac = sum(parse_amount(v) is None and not looks_like_isin(v)
                    for v in vals) / n

    out = {}
    for fname, spec in fields.items():
        kind = spec["kind"]
        if kind == "isin":
            out[fname] = isin_frac
        elif kind == "number":
            # holding value: big numbers; quantity: any numbers
            out[fname] = big_num_frac if fname == "holding_value" else num_frac
        elif kind == "percent":
            out[fname] = pct_in_range * (0.6 + 0.4 * (1 - big_num_frac))
        elif kind == "text":
            kw = spec.get("keywords", [])
            if kw:
                kwf = sum(any(k in clean_text(v).lower() for k in kw)
                          for v in vals) / n
                out[fname] = text_frac * (0.4 + 0.6 * kwf)
            else:
                out[fname] = text_frac * 0.5
    return out


def _assign_columns(df, header_row, fields):
    """Combine header-name and content evidence -> {field: col}, {field: conf}."""
    ncols = df.shape[1]
    data_start = (header_row + 1) if header_row is not None else 0
    data = df.iloc[data_start:]

    # candidate scores[col][field]
    scored = {}
    for c in range(ncols):
        col_vals = data[c].tolist() if c in data.columns else []
        cscore = _content_scores(col_vals, fields)
        hscore = {}
        if header_row is not None:
            f, s = _header_col_field(df.iat[header_row, c], fields)
            if f:
                hscore[f] = s
        combined = {}
        for fname in fields:
            combined[fname] = 0.65 * hscore.get(fname, 0.0) + \
                0.35 * cscore.get(fname, 0.0)
        scored[c] = combined

    # greedy assignment: highest (field,col) score wins, one col per field
    pairs = []
    for c, fmap in scored.items():
        for fname, s in fmap.items():
            if s > 0:
                pairs.append((s, fname, c))
    pairs.sort(reverse=True)

    columns, confidence, used_cols, used_fields = {}, {}, set(), set()
    for s, fname, c in pairs:
        if fname in used_fields or c in used_cols:
            continue
        if s < 0.25:
            continue
        columns[fname] = c
        confidence[fname] = round(min(1.0, s), 3)
        used_fields.add(fname)
        used_cols.add(c)
    return columns, confidence


# --------------------------------------------------------------------------- #
# Row classification
# --------------------------------------------------------------------------- #

def _is_junk_text(text) -> bool:
    t = clean_text(text).lower()
    return any(tok in t for tok in schema.JUNK_ROW_TOKENS)


def _looks_like_category(text) -> bool:
    t = clean_text(text)
    return bool(_CATEGORY_RE.search(t)) or _is_junk_text(t)


def _classify_rows(df, header_row, columns, headerless):
    hv_col = columns.get("holding_value")
    scheme_col = columns.get("scheme_name")

    # Pre-pass over single-cell rows to decide structure:
    #   * >=2 non-category single-cell rows  -> "marker style" (many users,
    #     IIFL); every non-category single is a user marker, no title.
    #   * exactly 1 non-category single      -> that row is the title/owner.
    #   * 0 non-category singles but a single-cell row sits above the header
    #     (or first in a headerless sheet)   -> that row is the title, even if
    #     its text contains a junk-ish token (e.g. "Total Cash Available").
    singles = []
    for i in range(len(df)):
        row = df.iloc[i]
        if int(row.notna().sum()) == 1:
            only = clean_text(row.dropna().iloc[0])
            singles.append((i, _looks_like_category(only)))
    non_cat = [i for i, cat in singles if not cat]
    if len(non_cat) >= 2:
        title_idx = None
    elif len(non_cat) == 1:
        title_idx = non_cat[0]
    else:
        above = [i for i, _ in singles
                 if header_row is not None and i < header_row]
        if above:
            title_idx = above[-1]
        elif headerless and singles:
            title_idx = singles[0][0]
        else:
            title_idx = None

    labels = []
    for i in range(len(df)):
        row = df.iloc[i]
        nonnull = int(row.notna().sum())
        if nonnull == 0:
            labels.append("blank")
            continue
        if header_row is not None and i == header_row:
            labels.append("header")
            continue

        hv = parse_amount(row.iat[hv_col]) if hv_col is not None and \
            hv_col < len(row) else None
        scheme_txt = clean_text(row.iat[scheme_col]) if scheme_col is not None \
            and scheme_col < len(row) else ""

        # single populated cell -> title / section / marker
        if nonnull == 1:
            only = clean_text(row.dropna().iloc[0])
            if i == title_idx:
                labels.append("title")
            elif _looks_like_category(only):
                labels.append("section")
            else:
                labels.append("marker")
            continue

        # junk (totals / cash / ledger) is checked before data
        if scheme_txt and _is_junk_text(scheme_txt):
            labels.append("junk")
        elif hv is not None and (scheme_txt or columns.get("isin") is not None):
            labels.append("data")
        else:
            labels.append("junk")
    return labels


# --------------------------------------------------------------------------- #
# User mode
# --------------------------------------------------------------------------- #

def _decide_user_mode(df, columns, confidence, labels):
    if "user_id" in columns and confidence.get("user_id", 0) >= 0.5:
        return "column", columns["user_id"], None
    markers = [i for i, l in enumerate(labels) if l == "marker"]
    if markers:
        return "marker", None, None
    titles = [i for i, l in enumerate(labels) if l == "title"]
    if titles:
        raw = clean_text(df.iloc[titles[0]].dropna().iloc[0])
        return "title", None, _title_to_user(raw)
    return "single", None, None


def _title_to_user(raw: str) -> str:
    # "ASRENTER (Total Cash Available- 2.50cr)" -> "ASRENTER"
    head = raw.split("(")[0].strip()
    head = re.split(r"[-:]", head)[0].strip() if len(head) > 25 else head
    return head or raw


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def profile_grid(df: pd.DataFrame, fields) -> FieldMap:
    header_row, _ = _find_header_row(df, fields)
    headerless = header_row is None
    columns, confidence = _assign_columns(df, header_row, fields)
    labels = _classify_rows(df, header_row, columns, headerless)
    user_mode, user_col, title_user = _decide_user_mode(
        df, columns, confidence, labels)

    notes = []
    for fname, spec in fields.items():
        if spec.get("required") and fname not in columns:
            notes.append(f"Required field '{fname}' not detected - please map it.")
    return FieldMap(columns=columns, confidence=confidence, header_row=header_row,
                    row_class=labels, user_mode=user_mode, user_col=user_col,
                    title_user=title_user, headerless=headerless, notes=notes)


def fieldmap_from_columns(df: pd.DataFrame, fields, columns: dict,
                          user_mode: str | None = None) -> FieldMap:
    """Rebuild a FieldMap from a user-edited column mapping (review override)."""
    columns = {f: int(c) for f, c in columns.items() if c is not None}
    header_row, _ = _find_header_row(df, fields)
    headerless = header_row is None
    labels = _classify_rows(df, header_row, columns, headerless)
    if user_mode is None:
        user_mode, user_col, title_user = _decide_user_mode(
            df, columns, {f: 1.0 for f in columns}, labels)
    else:
        user_col = columns.get("user_id")
        title_user = None
        if user_mode == "title":
            titles = [i for i, l in enumerate(labels) if l == "title"]
            if titles:
                title_user = _title_to_user(
                    clean_text(df.iloc[titles[0]].dropna().iloc[0]))
    confidence = {f: 1.0 for f in columns}
    notes = []
    for fname, spec in fields.items():
        if spec.get("required") and fname not in columns:
            notes.append(f"Required field '{fname}' not mapped.")
    return FieldMap(columns=columns, confidence=confidence, header_row=header_row,
                    row_class=labels, user_mode=user_mode, user_col=user_col,
                    title_user=title_user, headerless=headerless, notes=notes)


def fieldmap_from_template(df: pd.DataFrame, fields, tpl: dict) -> FieldMap:
    """Build a FieldMap from a saved broker template (skips auto-detection)."""
    match_by = tpl.get("match_by", "header")
    header_row = None
    columns: dict = {}

    if match_by == "header":
        header_row, _ = _find_header_row(df, fields)
        if header_row is not None:
            header_cells = {c: _norm_header(df.iat[header_row, c])
                            for c in range(df.shape[1])}
            for fname, alias in tpl.get("header_aliases", {}).items():
                target = _norm_header(alias)
                for c, hc in header_cells.items():
                    if hc == target or (target and target in hc):
                        columns[fname] = c
                        break
    else:  # index-based (headerless layouts)
        for fname, idx in tpl.get("columns", {}).items():
            columns[fname] = int(idx)

    headerless = header_row is None
    labels = _classify_rows(df, header_row, columns, headerless)
    user_mode = tpl.get("user_mode", "single")
    user_col = columns.get("user_id")
    title_user = None
    if user_mode == "title":
        titles = [i for i, l in enumerate(labels) if l == "title"]
        if titles:
            title_user = _title_to_user(
                clean_text(df.iloc[titles[0]].dropna().iloc[0]))
    confidence = {f: 1.0 for f in columns}
    return FieldMap(columns=columns, confidence=confidence, header_row=header_row,
                    row_class=labels, user_mode=user_mode, user_col=user_col,
                    title_user=title_user, headerless=headerless,
                    notes=[f"Applied template: {tpl.get('name', '?')}"])

# ===== core/ingest/normalizer.py =====
"""
Normalizer
==========
Takes a Grid + its FieldMap and produces a clean Standard-Format DataFrame
(portfolio or haircut), plus a report of what was kept / dropped / flagged.

This is where messy values become tidy ones:
  - User IDs resolved (column / stacked markers / title / single owner),
  - amounts parsed from Indian formats,
  - haircut % normalised to 0..100 (column-level fraction detection),
  - section / junk / blank rows discarded (and counted).
"""






@dataclass
class NormReport:
    rows_in: int = 0
    rows_kept: int = 0
    rows_dropped: int = 0
    dropped_examples: list = field(default_factory=list)
    n_users: int = 0
    warnings: list = field(default_factory=list)


def _percent_column_is_fraction(values) -> bool:
    """True if a haircut column looks like fractions (0.09) not percents (9)."""
    nums = [v for v in values if v is not None]
    if not nums:
        return False
    return max(nums) <= 1.5


def normalize_portfolio(grid, fmap, default_user: str | None = None):
    df = grid.data
    cols = fmap.columns
    rep = NormReport(rows_in=len(df))

    isin_c = cols.get("isin")
    scheme_c = cols.get("scheme_name")
    qty_c = cols.get("quantity")
    hv_c = cols.get("holding_value")
    user_c = cols.get("user_id")
    hc_c = cols.get("haircut_pct")

    default_user = default_user or _stem(grid)
    current_user = fmap.title_user if fmap.user_mode == "title" else None

    rows = []
    for i in range(len(df)):
        label = fmap.row_class[i]
        row = df.iloc[i]
        if label in ("blank", "header", "section"):
            continue
        if label == "title":
            if fmap.user_mode != "title":
                continue
            current_user = fmap.title_user
            continue
        if label == "marker":
            current_user = clean_text(row.dropna().iloc[0])
            continue
        if label == "junk":
            rep.rows_dropped += 1
            if len(rep.dropped_examples) < 12:
                rep.dropped_examples.append(_row_preview(row))
            continue

        # ---- data row ----
        hv = parse_amount(row.iat[hv_c]) if hv_c is not None and hv_c < len(row) \
            else None
        if hv is None:
            rep.rows_dropped += 1
            continue
        if fmap.user_mode == "column" and user_c is not None and user_c < len(row):
            u = clean_text(row.iat[user_c])
            if u:
                current_user = u
        user = current_user or default_user

        rows.append({
            "user_id": user,
            "isin": (clean_text(row.iat[isin_c]).replace(" ", "")
                     if isin_c is not None and isin_c < len(row) else ""),
            "scheme_name": (clean_text(row.iat[scheme_c])
                            if scheme_c is not None and scheme_c < len(row) else ""),
            "quantity": (parse_amount(row.iat[qty_c])
                         if qty_c is not None and qty_c < len(row) else np.nan),
            "holding_value": hv,
            "_pf_haircut_raw": (parse_percent_raw(row.iat[hc_c])
                                if hc_c is not None and hc_c < len(row) else None),
        })

    out = pd.DataFrame(rows, columns=schema.STD_PORTFOLIO_COLS + ["_pf_haircut_raw"])

    # Per-value normalisation for an embedded portfolio haircut: a value < 1
    # is a fraction (0.09 -> 9%); >= 1 is already a percent (9 -> 9%). This
    # tolerates columns that mix the two conventions across user blocks.
    def _to_pct(v):
        if v is None or pd.isna(v):
            return np.nan
        return v * 100 if v < 1 else v
    out["pf_haircut_pct"] = out["_pf_haircut_raw"].map(_to_pct)
    out = out.drop(columns=["_pf_haircut_raw"])

    rep.rows_kept = len(out)
    rep.n_users = out["user_id"].nunique() if not out.empty else 0
    return out, rep


def normalize_haircut(grid, fmap):
    df = grid.data
    cols = fmap.columns
    rep = NormReport(rows_in=len(df))

    isin_c = cols.get("isin")
    scheme_c = cols.get("scheme_name")
    hc_c = cols.get("haircut_pct")

    raw_pcts = []
    rows = []
    for i in range(len(df)):
        label = fmap.row_class[i]
        if label in ("blank", "header", "section", "title", "marker"):
            continue
        row = df.iloc[i]
        pct = parse_percent_raw(row.iat[hc_c]) if hc_c is not None and \
            hc_c < len(row) else None
        isin = (clean_text(row.iat[isin_c]).replace(" ", "")
                if isin_c is not None and isin_c < len(row) else "")
        scheme = (clean_text(row.iat[scheme_c])
                  if scheme_c is not None and scheme_c < len(row) else "")
        if pct is None or (not isin and not scheme):
            rep.rows_dropped += 1
            continue
        raw_pcts.append(pct)
        rows.append({"isin": isin, "scheme_name": scheme, "_raw": pct})

    out = pd.DataFrame(rows, columns=["isin", "scheme_name", "_raw"])
    frac = _percent_column_is_fraction(raw_pcts)
    if frac:
        rep.warnings.append("Haircut column looked like fractions (<=1) - "
                            "multiplied by 100 to get percentages.")
    out["haircut_pct"] = out["_raw"].map(lambda v: v * 100 if frac else v)
    out = out[schema.STD_HAIRCUT_COLS]

    rep.rows_kept = len(out)
    return out, rep


def _stem(grid) -> str:
    base = grid.origin or grid.name
    return os.path.splitext(os.path.basename(base))[0] or "USER"


def _row_preview(row) -> str:
    vals = [clean_text(v) for v in row.tolist() if clean_text(v)]
    return " | ".join(vals[:5])
