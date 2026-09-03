"""
Layout-independent extraction of holdings from a portfolio PDF.

Broker statements disagree about everything: column order, header wording,
how many columns there are, whether a folio number exists. Rather than
recognise layouts, this reads the three things that are true of all of them.

  1. Every holding line carries an ISIN - "IN" plus ten alphanumerics.
     That anchors the rows, and discards page furniture, notes and totals
     for free, because none of them contain one.
  2. The scheme name is the run of words on the line that is not a number,
     not a date and not the ISIN. Where it wraps onto following lines, those
     lines carry no ISIN and no numbers, which is how they are recognised.
  3. units x NAV = market value. That identity names the money column with
     no reference to any header, and distinguishes market value from cost
     value - a distinction headers express a dozen different ways.

The output is a plain table with canonical headers, so everything downstream
(profiler, normalizer, engine) treats it exactly like a spreadsheet.
"""
from __future__ import annotations

import re

import pandas as pd

# "IN" plus ten alphanumerics. Deliberately not anchored: it has to be found
# *inside* a longer token, because a long folio number and the ISIN that
# follows it are often a single word with no space between them.
ISIN_IN_TEXT = re.compile(r"IN[A-Z0-9]{9}[0-9]")


def is_isin(text: str) -> bool:
    """A real ISIN, not an English word that happens to start with IN.

    "INFRASTRUCTURE" contains a twelve-character run beginning IN, and a
    loose pattern turns the Loads and Fees page into phantom holdings. Real
    ISINs end in a check digit and carry several digits in the body, which
    ordinary words do not.
    """
    t = (text or "").strip().upper()
    if len(t) != 12 or not ISIN_IN_TEXT.fullmatch(t):
        return False
    return sum(c.isdigit() for c in t) >= 3


def find_isin(text: str):
    """The ISIN inside a longer token, or None.

    Requires a non-letter (or nothing) directly after the match, so a run
    inside a longer word is rejected rather than cutting the word in half.
    """
    for m in ISIN_IN_TEXT.finditer((text or "").upper()):
        after = text[m.end():m.end() + 1]
        if after.isalpha():
            continue
        if is_isin(m.group(0)):
            return m
    return None

_NUMBER = re.compile(r"^\(?-?[\d,]+\.?\d*\)?%?$")
_DATE = re.compile(r"^\d{1,2}[-/][A-Za-z]{3,9}[-/]\d{2,4}$|^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$")
# An empty numeric cell. A single "-" is NOT here: statements are full of
# names like "Axis Large Cap Fund - Direct Growth" and dropping the hyphen
# would silently rewrite them.
_PLACEHOLDER = {"--", "---", "n.a.", "na", "nil", ""}

# Two numeric columns belong together when their centres sit within this many
# points of each other. Roughly one character width at statement font sizes.
_COLUMN_TOLERANCE = 18.0

# units x NAV has to land this close to a column for it to be the value.
_PRODUCT_TOLERANCE = 0.01

# NAV is used to identify the value column and then dropped: the app has no
# use for it, and a column of prices in the 10-200 range is readily mistaken
# for a haircut percentage further down the pipeline.
CANONICAL_HEADER = ["isin", "scheme name", "units", "market value"]


def looks_numeric(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and t.lower() not in _PLACEHOLDER and bool(_NUMBER.match(t))


def looks_like_date(text: str) -> bool:
    return bool(_DATE.match((text or "").strip()))


def to_number(text):
    """'1,234.56' -> 1234.56 ; '(123)' -> -123 ; '--' -> None."""
    t = (text or "").strip()
    if not t or t.lower() in _PLACEHOLDER:
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").rstrip("%").replace(",", "")
    try:
        val = float(t)
    except ValueError:
        return None
    return -val if neg else val


def split_glued(word: dict) -> list[dict]:
    """Cut a word wherever an ISIN appears inside it.

    `91096451512/0INF846K01DP8` is one word in the PDF - the folio column and
    the ISIN column abut with no space, so no amount of geometry separates
    them. Splitting on the pattern is the only thing that does. Character
    widths are assumed even across the token, which is close enough to keep
    the pieces in their own columns.
    """
    text = word["text"]
    match = find_isin(text)
    if not match or len(text) == len(match.group(0)):
        return [word]

    x0, x1 = word["x0"], word["x1"]
    per_char = (x1 - x0) / max(len(text), 1)
    pieces = []
    for start, end in ((0, match.start()), (match.start(), match.end()),
                       (match.end(), len(text))):
        chunk = text[start:end]
        if not chunk:
            continue
        piece = dict(word)
        piece["text"] = chunk
        piece["x0"] = x0 + per_char * start
        piece["x1"] = x0 + per_char * end
        pieces.append(piece)
    return pieces


def page_words(page) -> list[dict]:
    """Words on a page, with glued folio/ISIN tokens already separated."""
    out = []
    for w in page.extract_words(use_text_flow=False, keep_blank_chars=False):
        out.extend(split_glued(w))
    return out


def group_lines(words: list[dict], tolerance: float = 3.0) -> list[list[dict]]:
    """Words bucketed into printed lines by vertical position."""
    lines: dict[int, list[dict]] = {}
    for w in words:
        lines.setdefault(round(w["top"] / tolerance), []).append(w)
    return [sorted(lines[k], key=lambda w: w["x0"]) for k in sorted(lines)]


def cluster_columns(centres: list[float],
                    tolerance: float = _COLUMN_TOLERANCE) -> list[float]:
    """One representative x per column, from every numeric token's centre.

    Numbers in a statement are right-aligned under their heading, so their
    positions form tight clusters however ragged the text around them is.
    """
    if not centres:
        return []
    ordered = sorted(centres)
    groups = [[ordered[0]]]
    for c in ordered[1:]:
        if c - groups[-1][-1] <= tolerance:
            groups[-1].append(c)
        else:
            groups.append([c])
    return [sum(g) / len(g) for g in groups]


def _nearest(value: float, columns: list[float]) -> int:
    return min(range(len(columns)), key=lambda i: abs(columns[i] - value))


def find_value_column(matrix: list[list], n_cols: int) -> tuple[int, int, int]:
    """Which numeric columns are units, NAV and market value.

    Tries every ordered pair as (units, NAV) and looks for the column their
    product lands on. The combination that works on the most rows wins. This
    is what separates market value from cost value without reading a header:
    only market value satisfies the identity.

    Returns (units, nav, value) column indices, any of which may be -1.
    """
    best = (-1, -1, -1)
    best_hits = 0
    for i in range(n_cols):
        for j in range(n_cols):
            if i == j:
                continue
            for k in range(n_cols):
                if k in (i, j):
                    continue
                hits = 0
                for row in matrix:
                    a, b, c = row[i], row[j], row[k]
                    if a is None or b is None or c is None or c == 0:
                        continue
                    if abs(a * b - c) / abs(c) <= _PRODUCT_TOLERANCE:
                        hits += 1
                if hits > best_hits:
                    best_hits, best = hits, (i, j, k)
    # Needs to hold on a real share of the rows, not one lucky coincidence.
    if best_hits >= max(2, len([r for r in matrix if any(v is not None for v in r)]) // 2):
        return best
    return (-1, -1, -1)


def fallback_value_column(matrix: list[list], n_cols: int) -> int:
    """No units/NAV to work with: take the column with the largest values.

    A holding value is orders of magnitude bigger than a unit count or a
    price, so the biggest median is the best guess available.
    """
    best, best_median = -1, -1.0
    for k in range(n_cols):
        vals = sorted(abs(r[k]) for r in matrix if r[k] is not None)
        if not vals:
            continue
        median = vals[len(vals) // 2]
        if median > best_median:
            best, best_median = k, median
    return best


def build_bands(lines: list[list[dict]], gap: float = 6.0) -> list[list[float]]:
    """Column x-ranges, from the spans of every token on the holding lines.

    Tokens in one column overlap the same horizontal range, whether the
    column is left-aligned text or right-aligned numbers, so merging
    overlapping spans recovers the columns without knowing the layout. This
    is what keeps a folio number and a registrar out of the scheme name -
    they occupy their own bands, however wide the name beside them runs.
    """
    spans = sorted((w["x0"], w["x1"]) for line in lines for w in line)
    if not spans:
        return []
    bands = [list(spans[0])]
    for x0, x1 in spans[1:]:
        if x0 <= bands[-1][1] + gap:
            bands[-1][1] = max(bands[-1][1], x1)
        else:
            bands.append([x0, x1])
    return bands


def band_of(word: dict, bands: list[list[float]]) -> int:
    """The band a word sits in - the one its centre falls inside, else nearest."""
    centre = (word["x0"] + word["x1"]) / 2.0
    for i, (lo, hi) in enumerate(bands):
        if lo <= centre <= hi:
            return i
    return min(range(len(bands)),
               key=lambda i: min(abs(bands[i][0] - centre),
                                 abs(bands[i][1] - centre))) if bands else -1


def name_band(anchor_lines: list[list[dict]], bands: list[list[float]],
              exclude: set[int]) -> int:
    """The band holding the scheme name: the most alphabetic text of any band.

    Folio numbers and registrar codes are short; a scheme name is long. Total
    letters per band separates them without caring which side they sit on.
    """
    weight = {}
    for line in anchor_lines:
        for w in line:
            i = band_of(w, bands)
            if i < 0 or i in exclude:
                continue
            letters = sum(c.isalpha() for c in w["text"])
            weight[i] = weight.get(i, 0) + letters
    return max(weight, key=weight.get) if weight else -1


def is_name_word(text: str) -> bool:
    """Whether a token can be part of a scheme name.

    Excludes numbers, dates, empty-cell markers, and folio-like tokens - the
    latter being mostly digits, which no fund name is.
    """
    t = (text or "").strip()
    if not t or t.lower() in _PLACEHOLDER:
        return False
    if looks_numeric(t) or looks_like_date(t):
        return False
    # A folio number, not part of a name: they carry a slash, or run to many
    # digits. Scheme codes such as D812, P8180 or 128EFDGG are short and do
    # belong to the name, so length is what separates the two.
    if "/" in t:
        return False
    if len(t) >= 7 and all(c.isdigit() for c in t):
        return False
    return True


def name_from_line(line: list[dict]) -> str:
    """The scheme name on a holding line: its longest run of name words.

    Reading left to right, a folio number, the ISIN and the numeric columns
    all break the run, so the surviving longest stretch is the name - wherever
    the layout happens to put it, and whatever the columns are called. A
    registrar code sits past the numbers and so lands in a different, shorter
    run.
    """
    runs, current = [], []
    for w in line:
        if is_isin(w["text"]) or not is_name_word(w["text"]):
            if current:
                runs.append(current)
                current = []
            continue
        current.append(w["text"])
    if current:
        runs.append(current)
    if not runs:
        return ""
    best = max(runs, key=lambda r: sum(len(t) for t in r))
    return " ".join(best).strip()


def name_runs(line: list[dict]) -> list[list[dict]]:
    """Contiguous stretches of name words on a line, in reading order."""
    runs, current = [], []
    for w in line:
        if is_isin(w["text"]) or not is_name_word(w["text"]):
            if current:
                runs.append(current)
                current = []
            continue
        current.append(w)
    if current:
        runs.append(current)
    return runs


def name_span(lines: list[list[dict]]) -> tuple[float, float]:
    """The horizontal range the scheme names occupy.

    Taken from the longest text run on each line: across a whole statement
    those runs pile up in one place - the name column - while folio numbers
    and registrar codes sit in their own, narrower places. Calibrating from
    the document itself means no layout has to be known in advance.

    Returns (left, right) with a little slack, or (-inf, inf) if there is
    nothing to go on, which keeps every candidate rather than none.
    """
    spans = []
    for line in lines:
        runs = name_runs(line)
        if not runs:
            continue
        best = max(runs, key=lambda r: sum(len(w["text"]) for w in r))
        if sum(len(w["text"]) for w in best) >= 8:
            spans.append((best[0]["x0"], best[-1]["x1"]))
    if not spans:
        return (float("-inf"), float("inf"))
    lefts = sorted(s[0] for s in spans)
    return (lefts[len(lefts) // 2] - 12.0, float("inf"))


def numeric_left_edge(anchor_lines: list[list[dict]]) -> float:
    """Where the numeric columns begin.

    Taken as the median of each row's leftmost number, so a stray digit
    inside a name - "BHARAT 22 FOF" - does not drag the boundary left and
    truncate every name in the document. Everything to the left of this is
    name, whatever it looks like.
    """
    firsts = []
    for line in anchor_lines:
        xs = [w["x0"] for w in line if looks_numeric(w["text"])]
        if xs:
            firsts.append(min(xs))
    if not firsts:
        return float("inf")
    firsts.sort()
    return firsts[len(firsts) // 2] - 4.0


def name_in_span(line: list[dict], span: tuple[float, float]) -> str:
    """Name words on a line that sit inside the name column."""
    left, right = span
    words = []
    for w in line:
        if is_isin(w["text"]):
            continue
        centre = (w["x0"] + w["x1"]) / 2.0
        if not (left <= centre <= right):
            continue
        # Inside the name column, a numeric-looking token is part of the
        # name - "BHARAT 22 FOF" - not a column of its own.
        if is_name_word(w["text"]) or looks_numeric(w["text"]):
            words.append(w["text"])
    return " ".join(words).strip()


def extract_holdings(pdf):
    """Holdings from an open pdfplumber document, or None if there are none.

    The returned frame carries CANONICAL_HEADER as its first row, so the
    ordinary profiler recognises the columns by name and nothing downstream
    needs to know a PDF was involved.
    """
    # --- pass 1: every printed line, and which of them carry an ISIN --------
    all_lines: list[list[dict]] = []
    for page in pdf.pages:
        words = page_words(page)
        if words:
            all_lines.extend(group_lines(words))

    anchor_at = [i for i, line in enumerate(all_lines)
                 if sum(1 for w in line if is_isin(w["text"])) == 1]
    if not anchor_at:
        return None

    # --- pass 2: calibrate where the names and the numbers live -------------
    # Only lines at or just after an anchor: page furniture and the notes page
    # would otherwise drag the name column somewhere it is not.
    near: list[list[dict]] = []
    for pos, i in enumerate(anchor_at):
        end = anchor_at[pos + 1] if pos + 1 < len(anchor_at) else len(all_lines)
        near.extend(all_lines[i:min(end, i + 4)])
    left, _ = name_span(near)
    span = (left, numeric_left_edge([all_lines[i] for i in anchor_at]))

    bands = build_bands([all_lines[i] for i in anchor_at])
    numeric_bands = sorted({band_of(w, bands) for i in anchor_at
                            for w in all_lines[i] if looks_numeric(w["text"])
                            and (w["x0"] + w["x1"]) / 2.0 > span[1]})
    index_of = {b: k for k, b in enumerate(numeric_bands)}
    n_cols = len(numeric_bands)

    # --- pass 3: one record per anchor, names gathered from its own line ----
    # and from the continuation lines beneath it.
    records, matrix = [], []
    for pos, i in enumerate(anchor_at):
        line = all_lines[i]
        isin = next(w["text"].upper() for w in line if is_isin(w["text"]))

        numbers = [None] * n_cols
        for w in line:
            if looks_numeric(w["text"]) and (w["x0"] + w["x1"]) / 2.0 > span[1]:
                b = band_of(w, bands)
                if b in index_of:
                    numbers[index_of[b]] = to_number(w["text"])

        parts = [name_in_span(line, span)]
        end = anchor_at[pos + 1] if pos + 1 < len(anchor_at) else len(all_lines)
        for j in range(i + 1, end):
            follow = all_lines[j]
            if any(looks_numeric(w["text"]) for w in follow):
                break          # a new table row, not a wrapped name
            text = name_in_span(follow, span)
            if not text:
                break          # nothing in the name column: the block ended
            parts.append(text)

        records.append({"isin": isin,
                        "name": " ".join(p for p in parts if p).strip()})
        matrix.append(numbers)

    units_i, nav_i, value_i = find_value_column(matrix, n_cols)
    if value_i < 0:
        value_i = fallback_value_column(matrix, n_cols)

    rows = [CANONICAL_HEADER]
    for rec, nums in zip(records, matrix):
        rows.append([
            rec["isin"],
            rec["name"],
            nums[units_i] if units_i >= 0 else None,
            nums[value_i] if value_i >= 0 else None,
        ])
    return pd.DataFrame(rows)
