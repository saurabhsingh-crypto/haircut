"""
Standardisation pipeline: file bytes -> canonical DataFrame.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import ingest, schema, templates

# ===== core/pipeline.py =====
"""
Ingestion pipeline (orchestrator)
=================================
High-level entry points the app calls:

    standardize(src, filename, target)            -> StandardizeResult
    apply_overrides(result, overrides)            -> StandardizeResult (re-normalised)

It ties together reader -> (template | profiler) -> normalizer for every
grid in a file and concatenates the results into one Standard-Format table,
while keeping every FieldMap around so the UI can show / edit the mapping.
"""






@dataclass
class GridPlan:
    grid: object               # Grid
    fieldmap: object           # FieldMap
    report: object = None      # NormReport
    std: pd.DataFrame = None    # standardised rows from this grid


@dataclass
class StandardizeResult:
    target: str
    data: pd.DataFrame
    plans: list = field(default_factory=list)   # GridPlan per grid
    template: dict | None = None
    warnings: list = field(default_factory=list)

    @property
    def n_rows(self):
        return 0 if self.data is None else len(self.data)

    @property
    def n_users(self):
        if self.target != "portfolio" or self.data is None or self.data.empty:
            return 0
        return self.data["user_id"].nunique()


def _fields(target):
    return schema.PORTFOLIO_FIELDS if target == "portfolio" \
        else schema.HAIRCUT_FIELDS


def _normalize(target, grid, fmap):
    if target == "portfolio":
        return ingest.normalize_portfolio(grid, fmap)
    return ingest.normalize_haircut(grid, fmap)


def standardize(src, filename: str, target: str,
                use_template: bool = True) -> StandardizeResult:
    grids = ingest.read_grids(src, filename)
    fields = _fields(target)
    tpl = templates.find_template(grids, target) if use_template else None

    plans, warnings = [], []
    for g in grids:
        if tpl:
            fmap = ingest.fieldmap_from_template(g.data, fields, tpl)
        else:
            fmap = ingest.profile_grid(g.data, fields)
        std, rep = _normalize(target, g, fmap)
        plans.append(GridPlan(grid=g, fieldmap=fmap, report=rep, std=std))
        warnings.extend(f"[{g.name}] {w}" for w in rep.warnings)

    data = _concat(target, plans)
    return StandardizeResult(target=target, data=data, plans=plans,
                             template=tpl, warnings=warnings)


def apply_overrides(result: StandardizeResult, overrides: list) -> StandardizeResult:
    """
    overrides: list parallel to result.plans; each item is None (keep) or a
    dict {'columns': {field: col}, 'user_mode': str|None}.
    """
    fields = _fields(result.target)
    plans = []
    for plan, ov in zip(result.plans, overrides):
        if ov:
            fmap = ingest.fieldmap_from_columns(
                plan.grid.data, fields, ov.get("columns", {}),
                ov.get("user_mode"))
        else:
            fmap = plan.fieldmap
        std, rep = _normalize(result.target, plan.grid, fmap)
        plans.append(GridPlan(grid=plan.grid, fieldmap=fmap, report=rep, std=std))
    data = _concat(result.target, plans)
    return StandardizeResult(target=result.target, data=data, plans=plans,
                             template=result.template, warnings=result.warnings)


def _concat(target, plans):
    frames = [p.std for p in plans if p.std is not None and not p.std.empty]
    if not frames:
        cols = (schema.STD_PORTFOLIO_COLS + ["pf_haircut_pct"]
                if target == "portfolio" else schema.STD_HAIRCUT_COLS)
        return pd.DataFrame(columns=cols)
    return pd.concat(frames, ignore_index=True)
