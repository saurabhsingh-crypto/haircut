"""
Upload a portfolio, check the detected column mapping, compute collateral
margin against a saved haircut master, and export the result.
"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

import app_support as sup
import haircut_core as hc
from haircut_core import engine, report

sup.require_storage()

st.title("Calculate margin")

st.session_state.setdefault("pf_bytes", None)
st.session_state.setdefault("pf_name", None)
st.session_state.setdefault("overrides", None)
st.session_state.setdefault("calc_params", None)


# --------------------------------------------------------------------------- #
# Manual calculator
# --------------------------------------------------------------------------- #
#
# A scratchpad for "what margin would these holdings give me": search the
# chosen haircut master, add the securities you care about, type the amounts.
# The arithmetic is the same as the engine's, so both tabs always agree.

MANUAL_COLS = ["isin", "scheme_name", "haircut_pct", "amount"]
SEARCH_LIMIT = 50


def _manual_search(hc_df, query):
    """Rows whose ISIN or security name contains `query`, case-insensitively."""
    q = (query or "").strip()
    if not q:
        return hc_df.head(0)
    # regex=False: a security name containing a bracket must not be read as a
    # pattern, and a stray one would otherwise raise mid-keystroke.
    hit = (hc_df["isin"].str.contains(q, case=False, na=False, regex=False)
           | hc_df["scheme_name"].str.contains(q, case=False, na=False,
                                               regex=False))
    return hc_df[hit]


def _sync_amounts(edited):
    """Copy typed amounts back into the stored rows.

    Only at the moments the table is about to be rebuilt - adding or clearing
    rows - never on every rerun. Writing back continuously would fight
    st.data_editor for ownership of its own state and make every other edit
    disappear.
    """
    if edited is None or getattr(edited, "empty", True):
        return
    rows = st.session_state["manual_rows"]
    for i, amount in enumerate(edited["amount"].tolist()):
        if i < len(rows):
            rows[i]["amount"] = 0.0 if pd.isna(amount) else float(amount)


def manual_calculator():
    st.session_state.setdefault("manual_rows", [])

    masters = sup.list_masters()
    if not masters:
        st.info("No haircut master is available yet.",
                icon=":material/library_add:")
        return

    by_slug = {m["slug"]: m for m in masters}
    slug = st.selectbox(
        "Haircut master", list(by_slug),
        format_func=lambda s: by_slug[s]["name"] + " ("
                              + format(by_slug[s]["n_rows"], ",") + " rows)",
        key="manual_master")
    hc_df = sup.load_master(slug)

    # ---- find securities --------------------------------------------------
    query = st.text_input(
        "Search by ISIN or security name", key="manual_query",
        placeholder="e.g. INF204K01K15, or nippon liquid")

    found = _manual_search(hc_df, query)
    if query and found.empty:
        st.caption("Nothing in " + by_slug[slug]["name"] + " matches that.")
    elif not found.empty:
        shown = found.head(SEARCH_LIMIT)
        more = (", showing the first " + str(SEARCH_LIMIT)
                if len(found) > SEARCH_LIMIT else "")
        st.caption(format(len(found), ",") + " match(es)" + more
                   + ". Tick the ones you want, then press Add.")
        picked = st.dataframe(
            shown, hide_index=True, on_select="rerun",
            selection_mode="multi-row", key="manual_hits",
            column_config={
                "isin": st.column_config.TextColumn("ISIN", pinned=True),
                "scheme_name": st.column_config.TextColumn("Security name"),
                "haircut_pct": st.column_config.NumberColumn(
                    "Haircut %", format="%.2f%%"),
            })
        chosen = picked.selection.rows if picked and picked.selection else []
        label = ("Add " + str(len(chosen)) + " selected") if chosen else "Add"
        if st.button(label, icon=":material/add:", disabled=not chosen,
                     type="primary"):
            _sync_amounts(st.session_state.get("manual_edited"))
            have = {r["isin"] for r in st.session_state["manual_rows"] if r["isin"]}
            added = 0
            for i in chosen:
                row = shown.iloc[i]
                if row["isin"] and row["isin"] in have:
                    continue
                st.session_state["manual_rows"].append({
                    "isin": row["isin"],
                    "scheme_name": row["scheme_name"],
                    "haircut_pct": float(row["haircut_pct"]),
                    "amount": 0.0,
                })
                added += 1
            if added < len(chosen):
                st.toast(str(len(chosen) - added) + " already in the table")
            st.rerun()

    # ---- the working table ------------------------------------------------
    rows = st.session_state["manual_rows"]
    if not rows:
        st.info("Search above and add securities to start.",
                icon=":material/calculate:")
        return

    st.subheader("Your securities")
    st.caption("Type an amount against each. Everything else comes from the "
               "haircut master and cannot be edited.")

    table = pd.DataFrame(rows, columns=MANUAL_COLS)
    edited = st.data_editor(
        table, hide_index=True, key="manual_editor", width="stretch",
        disabled=["isin", "scheme_name", "haircut_pct"],
        column_config={
            "isin": st.column_config.TextColumn("ISIN", pinned=True),
            "scheme_name": st.column_config.TextColumn("Security name"),
            "haircut_pct": st.column_config.NumberColumn("Haircut %",
                                                         format="%.2f%%"),
            "amount": st.column_config.NumberColumn(
                "Amount", min_value=0.0, step=1000.0, format="localized",
                help="What you hold in this security, in rupees."),
        })
    st.session_state["manual_edited"] = edited

    amount = pd.to_numeric(edited["amount"], errors="coerce").fillna(0.0)
    pct = pd.to_numeric(edited["haircut_pct"], errors="coerce").fillna(0.0)
    haircut_value = amount * pct / 100.0
    margin = amount - haircut_value

    result = edited.copy()
    result["haircut_amount"] = haircut_value
    result["available_margin"] = margin

    total_amount = float(amount.sum())
    total_haircut = float(haircut_value.sum())
    total_margin = float(margin.sum())
    ratio = (total_haircut / total_amount * 100.0) if total_amount else 0.0

    with st.container(horizontal=True):
        st.metric("Securities", format(len(result), ","), border=True)
        st.metric("Total amount", sup.money_compact(total_amount), border=True,
                  help=sup.money(total_amount))
        st.metric("Total haircut", sup.money_compact(total_haircut),
                  delta=format(ratio, ".2f") + "% of amount",
                  delta_color="inverse", border=True,
                  help=sup.money(total_haircut))
        st.metric("Available margin", sup.money_compact(total_margin),
                  border=True, help=sup.money(total_margin))

    st.dataframe(
        result[["isin", "scheme_name", "haircut_pct", "amount",
                "haircut_amount", "available_margin"]],
        hide_index=True,
        column_config={
            "isin": st.column_config.TextColumn("ISIN", pinned=True),
            "scheme_name": st.column_config.TextColumn("Security name"),
            "haircut_pct": st.column_config.NumberColumn("Haircut %",
                                                         format="%.2f%%"),
            "amount": st.column_config.NumberColumn("Amount",
                                                    format="localized"),
            "haircut_amount": st.column_config.NumberColumn(
                "Haircut", format="localized"),
            "available_margin": st.column_config.NumberColumn(
                "Available margin", format="localized"),
        })

    with st.container(horizontal=True):
        st.download_button(
            "Download as Excel", data=sup.manual_excel(result),
            file_name="haircut_manual_calculation.xlsx",
            mime=report.XLSX_MIME, icon=":material/download:", type="primary")
        if st.button("Clear table", icon=":material/delete_sweep:"):
            st.session_state["manual_rows"] = []
            st.session_state.pop("manual_edited", None)
            st.rerun()


# Two ways to do the same sum. The file flow below uses st.stop() in
# several places, and st.stop() ends the whole script rather than just its
# tab - so the manual tab is rendered FIRST and is already complete by the
# time the file flow can stop. Tab order on screen is unaffected.
tab_file, tab_manual = st.tabs(["From a file", "Manual calculator"])

with tab_manual:
    manual_calculator()

with tab_file:
    # --------------------------------------------------------------------------- #
    # 1. Portfolio upload
    # --------------------------------------------------------------------------- #

    upload = st.file_uploader(
        "Portfolio or holdings file",
        type=["xlsx", "xlsm", "xls", "xlsb", "csv", "pdf"],
        help="Any broker layout. Columns are detected automatically and you can "
             "correct them below.",
    )

    if upload is not None:
        data = upload.getvalue()
        if not data:
            st.error("That file is empty.", icon=":material/error:")
            st.stop()
        if data != st.session_state.pf_bytes:
            # A new file invalidates the mapping picked for the previous one.
            st.session_state.pf_bytes = data
            st.session_state.pf_name = upload.name
            st.session_state.overrides = None
            st.session_state.pop("calc_params", None)

    if st.session_state.pf_bytes is None:
        st.info("Upload a portfolio file to begin.", icon=":material/upload_file:")
        st.stop()

    pf_bytes = st.session_state.pf_bytes
    pf_name = st.session_state.pf_name

    # --------------------------------------------------------------------------- #
    # 2. Standardise, with any manual mapping applied
    # --------------------------------------------------------------------------- #

    try:
        with st.spinner("Reading the file..."):
            pf_res = sup.standardize(
                pf_bytes, pf_name, "portfolio",
                sup.overrides_key(st.session_state.overrides))
    except hc.UnsupportedFile as e:
        st.error(str(e), icon=":material/description:")
        st.stop()
    except Exception as e:
        st.error(f"Could not read {pf_name}: {type(e).__name__}. Check that the "
                 f"file opens in Excel and is not password protected.",
                 icon=":material/error:")
        st.stop()

    dropped = sum(getattr(p.report, "rows_dropped", 0) or 0 for p in pf_res.plans)

    with st.container(horizontal=True):
        st.metric("Holdings detected", f"{pf_res.n_rows:,}", border=True)
        st.metric("Clients", f"{pf_res.n_users:,}", border=True)
        st.metric("Non-holding rows dropped", f"{dropped:,}", border=True)

    if pf_res.template:
        st.caption(f":material/bookmark: Recognised layout: "
                   f"**{pf_res.template['name']}**")
    for warning in (pf_res.warnings or []):
        st.warning(warning, icon=":material/warning:")

    if pf_res.n_rows == 0:
        st.error("No holdings were detected in this file. Open the column mapping "
                 "below and point the app at the right columns.",
                 icon=":material/search_off:")

    # --------------------------------------------------------------------------- #
    # 3. Column mapping
    # --------------------------------------------------------------------------- #

    mapping = st.expander("Column mapping and preview",
                          icon=":material/table_chart:",
                          expanded=pf_res.n_rows == 0)
    with mapping:
        st.caption("Fields marked * are required. Change a selection and press "
                   "Apply mapping.")
        picked = sup.mapping_editor(pf_res, "portfolio", "pf")

        with st.container(horizontal=True):
            if st.button("Apply mapping", icon=":material/check:", type="primary"):
                st.session_state.overrides = picked
                st.rerun()
            if st.button("Reset to auto-detected", icon=":material/restart_alt:"):
                st.session_state.overrides = None
                st.rerun()

        for plan in pf_res.plans:
            if plan.std is not None and not plan.std.empty:
                st.markdown(f"**Preview - {plan.grid.name}**")
                st.dataframe(plan.std.head(8), hide_index=True)
            examples = getattr(plan.report, "dropped_examples", None) or []
            if examples:
                with st.container(border=True):
                    st.markdown("**Rows skipped as non-holdings**")
                    for ex in examples[:8]:
                        st.caption(ex)

    if pf_res.n_rows == 0:
        st.stop()

    # --------------------------------------------------------------------------- #
    # 4. Haircut master and options
    # --------------------------------------------------------------------------- #

    masters = sup.list_masters()
    if not masters:
        if st.session_state.get("is_admin"):
            st.warning("No haircut master is saved yet. Add one on the Haircut "
                       "library page.", icon=":material/library_add:")
        else:
            st.warning("No haircut master is available yet. Ask an administrator "
                       "to add one.", icon=":material/library_add:")
        st.stop()

    by_slug = {m["slug"]: m for m in masters}

    # A form so that changing the master, the policy or the threshold does not
    # recompute on every keystroke - and, just as importantly, does not clear the
    # results already on screen. Nothing runs until Calculate is pressed.
    with st.form("calc_settings", border=True):
        st.markdown("**Calculation settings**")
        settings = st.columns([2, 2, 2])
        with settings[0]:
            slug = st.selectbox(
                "Haircut master", list(by_slug),
                format_func=lambda s: f"{by_slug[s]['name']} "
                                      f"({by_slug[s]['n_rows']:,} rows)",
                key="master_slug")
        with settings[1]:
            policy = st.selectbox(
                "Holdings with no haircut match", list(sup.MISSING_POLICIES),
                format_func=lambda p: sup.MISSING_POLICIES[p], key="policy")
        with settings[2]:
            threshold = st.slider(
                "Scheme-name match threshold", min_value=70, max_value=100,
                value=88, key="threshold",
                help="How close a scheme name must be to count as a match when "
                     "there is no ISIN. Higher is stricter.")
        submitted = st.form_submit_button("Calculate", icon=":material/calculate:",
                                          type="primary")

    if submitted:
        st.session_state.calc_params = {"slug": slug, "policy": policy,
                                        "threshold": float(threshold)}

    params = st.session_state.get("calc_params")
    if not params or params["slug"] not in by_slug:
        st.info("Press Calculate to run the margin calculation.",
                icon=":material/play_arrow:")
        st.stop()

    slug = params["slug"]
    policy = params["policy"]
    threshold = params["threshold"]

    try:
        with st.spinner("Matching holdings and computing margin..."):
            calc = sup.calculate(pf_bytes, pf_name,
                                 sup.overrides_key(st.session_state.overrides),
                                 slug, policy, threshold)
    except hc.StorageError as e:
        st.error(str(e), icon=":material/database_off:")
        st.stop()
    except Exception as e:
        st.error(f"The calculation failed: {type(e).__name__}.",
                 icon=":material/error:")
        st.stop()

    hc_df = sup.load_master(slug)
    totals = calc.totals

    # --------------------------------------------------------------------------- #
    # 5. Headline figures
    # --------------------------------------------------------------------------- #

    st.subheader("Result")
    ratio = (totals["haircut_value"] / totals["portfolio_value"] * 100
             if totals["portfolio_value"] else 0.0)

    with st.container(horizontal=True):
        st.metric("Portfolio value", sup.money_compact(totals["portfolio_value"]),
                  border=True, help=sup.money(totals["portfolio_value"]))
        st.metric("Haircut", sup.money_compact(totals["haircut_value"]),
                  delta=f"{ratio:.2f}% of portfolio", delta_color="inverse",
                  border=True, help=sup.money(totals["haircut_value"]))
        st.metric("Available margin",
                  sup.money_compact(totals["available_margin"]), border=True,
                  help=sup.money(totals["available_margin"]))
        st.metric("Clients", f"{totals['n_users']:,}", border=True)
        st.metric("Holdings", f"{totals['n_holdings']:,}", border=True)

    flags = []
    if totals["n_missing"]:
        flags.append(f"{totals['n_missing']:,} holding(s) matched no haircut record")
    if totals["n_scheme_matched"]:
        flags.append(f"{totals['n_scheme_matched']:,} matched by scheme name "
                     f"rather than ISIN")
    if calc.n_dup_rows:
        flags.append(f"{calc.n_dup_rows:,} duplicate ISIN row(s) in the master")
    if flags:
        st.warning("  \n".join(f"- {f}" for f in flags), icon=":material/flag:")

    st.caption(f"Haircut master: **{by_slug[slug]['name']}** "
               f"({len(hc_df):,} records) - portfolio: **{pf_name}**")

    # --------------------------------------------------------------------------- #
    # 6. Charts
    # --------------------------------------------------------------------------- #

    summary = calc.user_summary
    charts = st.columns(2)

    with charts[0]:
        with st.container(border=True, height="stretch"):
            st.markdown("**Available margin by client**")
            top = summary.nlargest(15, "available_margin")
            st.altair_chart(
                alt.Chart(top).mark_bar(color="#0F6E6B").encode(
                    x=alt.X("available_margin:Q", title="Available margin (Rs)"),
                    y=alt.Y("user_id:N", sort="-x", title=None),
                    tooltip=[alt.Tooltip("user_id:N", title="Client"),
                             alt.Tooltip("available_margin:Q", title="Margin",
                                         format=",.0f"),
                             alt.Tooltip("haircut_pct:Q", title="Haircut %",
                                         format=".2f")]))
            if len(summary) > 15:
                st.caption(f"Top 15 of {len(summary):,} clients by available margin.")

    with charts[1]:
        with st.container(border=True, height="stretch"):
            st.markdown("**Haircut as a share of portfolio**")
            st.altair_chart(
                alt.Chart(summary).mark_circle(size=70, color="#8F5400",
                                               opacity=0.65).encode(
                    x=alt.X("portfolio_value:Q", title="Portfolio value (Rs)",
                            scale=alt.Scale(zero=False)),
                    y=alt.Y("haircut_pct:Q", title="Haircut %"),
                    tooltip=[alt.Tooltip("user_id:N", title="Client"),
                             alt.Tooltip("portfolio_value:Q", title="Portfolio",
                                         format=",.0f"),
                             alt.Tooltip("haircut_pct:Q", title="Haircut %",
                                         format=".2f")]))
            st.caption("One point per client. Outliers high on the y-axis are "
                       "carrying the most haircut relative to their holdings.")

    # --------------------------------------------------------------------------- #
    # 7. Per-client table and drill-down
    # --------------------------------------------------------------------------- #

    st.subheader("By client")
    st.caption("Select a row to see that client's holdings.")

    money_col = st.column_config.NumberColumn(format="localized")
    event = st.dataframe(
        summary,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "user_id": st.column_config.TextColumn("Client", pinned=True),
            "holdings": st.column_config.NumberColumn("Holdings"),
            "portfolio_value": st.column_config.NumberColumn(
                "Portfolio value", format="localized"),
            "haircut_value": st.column_config.NumberColumn(
                "Haircut", format="localized"),
            "haircut_pct": st.column_config.NumberColumn(
                "Haircut %", format="%.2f%%"),
            "available_margin": st.column_config.NumberColumn(
                "Available margin", format="localized"),
        },
    )

    selected = event.selection.rows if event and event.selection else []
    if selected:
        user_id = summary.iloc[selected[0]]["user_id"]
        rows = calc.detail[calc.detail["user_id"] == user_id]
        with st.container(border=True):
            st.markdown(f"**{user_id}** - {len(rows):,} holdings")
            st.dataframe(
                rows.drop(columns=["user_id"]),
                hide_index=True,
                column_config={
                    "isin": st.column_config.TextColumn("ISIN"),
                    "scheme": st.column_config.TextColumn("Scheme", pinned=True),
                    "scrip_name": st.column_config.TextColumn("Matched scrip"),
                    "qty": st.column_config.NumberColumn("Qty", format="localized"),
                    "holding_value": st.column_config.NumberColumn(
                        "Holding value", format="localized"),
                    "haircut_pct": st.column_config.NumberColumn(
                        "Haircut %", format="%.2f%%"),
                    "haircut_amount": st.column_config.NumberColumn(
                        "Haircut", format="localized"),
                    "available_margin": st.column_config.NumberColumn(
                        "Available margin", format="localized"),
                    "haircut_source": st.column_config.TextColumn("Match source"),
                    "match_score": st.column_config.NumberColumn(
                        "Score", format="%.0f"),
                },
            )

    # --------------------------------------------------------------------------- #
    # 8. Issues and validation
    # --------------------------------------------------------------------------- #

    st.subheader("Checks")
    tab_missing, tab_fuzzy, tab_dupes, tab_valid = st.tabs(
        [f"Unmatched ({len(calc.missing_isin):,})",
         f"Scheme matches ({len(calc.scheme_matches):,})",
         f"Duplicate ISINs ({len(calc.duplicate_isin):,})",
         "Validation"])

    with tab_missing:
        if calc.missing_isin.empty:
            st.success("Every holding matched a haircut record.",
                       icon=":material/check_circle:")
        else:
            st.caption("These holdings found no haircut record. Under the current "
                       "policy they are treated as "
                       f"{sup.MISSING_POLICIES[policy].lower()}.")
            st.dataframe(calc.missing_isin, hide_index=True)

    with tab_fuzzy:
        if calc.scheme_matches.empty:
            st.caption("No holding needed scheme-name matching.")
        else:
            st.caption("Matched on scheme name because there was no ISIN. Review "
                       "low scores; raise the threshold to reject them.")
            st.dataframe(
                calc.scheme_matches.sort_values("match_score"), hide_index=True,
                column_config={"match_score": st.column_config.ProgressColumn(
                    "Score", min_value=0, max_value=100, format="%.0f")})

    with tab_dupes:
        if calc.duplicate_isin.empty:
            st.success("No ISIN carries conflicting haircut values.",
                       icon=":material/check_circle:")
        else:
            st.caption("The same ISIN appears with different haircut values. The "
                       "first occurrence was used.")
            st.dataframe(calc.duplicate_isin, hide_index=True)

    with tab_valid:
        n_conflict = (0 if calc.duplicate_isin.empty
                      else int(calc.duplicate_isin["isin"].nunique()))
        checks = st.columns(2)
        with checks[0]:
            st.markdown("**Portfolio**")
            st.dataframe(sup.checks_frame(
                engine.validate_portfolio(
                    sup.standardize(pf_bytes, pf_name, "portfolio",
                                    sup.overrides_key(st.session_state.overrides)
                                    ).data, dropped)),
                hide_index=True)
        with checks[1]:
            st.markdown("**Haircut master**")
            st.dataframe(sup.checks_frame(
                engine.validate_haircut(hc_df, calc.n_dup_rows, n_conflict)),
                hide_index=True)

    # --------------------------------------------------------------------------- #
    # 9. Export
    # --------------------------------------------------------------------------- #

    st.download_button(
        "Download full result as Excel",
        data=report.build_excel(calc),
        file_name="haircut_margin_results.xlsx",
        mime=report.XLSX_MIME,
        icon=":material/download:",
        type="primary",
    )
