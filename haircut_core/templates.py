"""
Embedded broker templates (was core/ingest/templates/*.json).

A template short-circuits column detection when a file's headers match a
known broker layout exactly.
"""
from __future__ import annotations

# ===== embedded templates =====
# ===== embedded broker templates (was core/ingest/templates/*.json) =====
_EMBEDDED_TEMPLATES = [{'name': 'IIFL Portfolio', 'target': 'portfolio', 'fingerprint': ['isin name', 'holding value', 'product type'], 'match_by': 'header', 'header_aliases': {'isin': 'isin', 'scheme_name': 'isin name', 'quantity': 'qty', 'holding_value': 'holding value', 'haircut_pct': 'haircut'}, 'user_mode': 'marker'}, {'name': 'IIFL Haircut Master', 'target': 'haircut', 'fingerprint': ['scripname', 'collateralhaircut'], 'match_by': 'header', 'header_aliases': {'isin': 'isin', 'scheme_name': 'scripname', 'haircut_pct': 'collateralhaircut'}, 'user_mode': 'single'}, {'name': 'ASRENTER Allocation', 'target': 'portfolio', 'fingerprint': ['total cash available', 'gold fund name'], 'match_by': 'index', 'columns': {'scheme_name': 0, 'holding_value': 1, 'haircut_pct': 2}, 'user_mode': 'title'}]

def load_templates():
    return [dict(t) for t in _EMBEDDED_TEMPLATES]

def grids_text(grids) -> str:
    """Concatenate a sample of cell text from all grids (lower-cased)."""
    chunks = []
    for g in grids:
        sample = g.data.head(40).values.ravel().tolist()
        chunks.append(" ".join(str(x) for x in sample))
    return " ".join(chunks).lower()

def find_template(grids, target: str) -> dict | None:
    text = grids_text(grids)
    for tpl in load_templates():
        if tpl.get("target") != target:
            continue
        fp = [s.lower() for s in tpl.get("fingerprint", [])]
        if fp and all(s in text for s in fp):
            return tpl
    return None
