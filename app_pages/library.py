"""
The saved haircut library: what is stored, adding a master, removing one.

Re-uploading under an existing name replaces that master's rows, so this is
also how you refresh a broker's haircut file month to month.
"""
from __future__ import annotations

import streamlit as st

import app_support as sup
import haircut_core as hc
from haircut_core import engine, store

status = sup.require_storage()

st.title("Haircut library")
st.caption(f"Stored in {status['target']}")
if status["seeded"]:
    st.success("Seeded the bundled IIFL haircut master into an empty library.",
               icon=":material/auto_awesome:")
for warning in status["warnings"]:
    st.warning(warning, icon=":material/warning:")

# --------------------------------------------------------------------------- #
# Saved masters
# --------------------------------------------------------------------------- #

masters = sup.list_masters()
if not masters:
    st.info("No haircut master saved yet. Add one below.",
            icon=":material/library_add:")
else:
    st.dataframe(
        masters, hide_index=True,
        column_config={
            "name": st.column_config.TextColumn("Name", pinned=True),
            "n_rows": st.column_config.NumberColumn("Records",
                                                    format="localized"),
            "source_file": st.column_config.TextColumn("Source file"),
            "created": st.column_config.TextColumn("First added"),
            "updated": st.column_config.TextColumn("Last updated"),
            "slug": None,
        })

    if not st.session_state.get("is_admin"):
        st.caption(":material/lock: Removing a master is restricted to "
                   "administrators. Anyone signed in can add one, or refresh "
                   "an existing one by re-uploading under the same name.")
    else:
        with st.expander("Remove a master", icon=":material/delete:"):
            by_slug = {m["slug"]: m for m in masters}
            victim = st.selectbox(
                "Master to remove", list(by_slug),
                format_func=lambda s: f"{by_slug[s]['name']} "
                                      f"({by_slug[s]['n_rows']:,} records)")
            st.caption("This deletes the master and all of its haircut records. "
                       "It cannot be undone.")
            confirm = st.text_input(
                "Type the master's name to confirm",
                placeholder=by_slug[victim]["name"])
            if st.button("Remove permanently", icon=":material/delete_forever:",
                         disabled=confirm.strip() != by_slug[victim]["name"]):
                try:
                    store.delete_master(victim)
                except hc.StorageError as e:
                    st.error(str(e), icon=":material/database_off:")
                else:
                    sup.clear_library_cache()
                    st.success(f"Removed {by_slug[victim]['name']}.",
                               icon=":material/check_circle:")
                    st.rerun()

# --------------------------------------------------------------------------- #
# Add or replace a master
# --------------------------------------------------------------------------- #

st.subheader("Add or replace a master")

st.session_state.setdefault("hc_bytes", None)
st.session_state.setdefault("hc_overrides", None)

upload = st.file_uploader(
    "Haircut master file",
    type=["xlsx", "xlsm", "xls", "xlsb", "csv", "pdf"],
    help="Needs a haircut percentage plus an ISIN or a scheme name per row.")

if upload is not None:
    data = upload.getvalue()
    if data != st.session_state.hc_bytes:
        st.session_state.hc_bytes = data
        st.session_state.hc_name_file = upload.name
        st.session_state.hc_overrides = None

if st.session_state.hc_bytes is None:
    st.stop()

raw = st.session_state.hc_bytes
source_file = st.session_state.get("hc_name_file", "upload.xlsx")

try:
    with st.spinner("Reading the file..."):
        res = sup.standardize(raw, source_file, "haircut",
                              sup.overrides_key(st.session_state.hc_overrides))
except hc.UnsupportedFile as e:
    st.error(str(e), icon=":material/description:")
    st.stop()
except Exception as e:
    st.error(f"Could not read {source_file}: {type(e).__name__}. Check that "
             f"the file opens in Excel and is not password protected.",
             icon=":material/error:")
    st.stop()

with st.container(horizontal=True):
    st.metric("Haircut records detected", f"{res.n_rows:,}", border=True)
    with_isin = 0
    if res.n_rows:
        with_isin = int((res.data["isin"].astype(str).str.len() > 0).sum())
    st.metric("With an ISIN", f"{with_isin:,}", border=True)

if res.template:
    st.caption(f":material/bookmark: Recognised layout: **{res.template['name']}**")
for warning in (res.warnings or []):
    st.warning(warning, icon=":material/warning:")

with st.expander("Column mapping and preview", icon=":material/table_chart:",
                 expanded=res.n_rows == 0):
    picked = sup.mapping_editor(res, "haircut", "hc")
    with st.container(horizontal=True):
        if st.button("Apply mapping", icon=":material/check:"):
            st.session_state.hc_overrides = picked
            st.rerun()
        if st.button("Reset to auto-detected", icon=":material/restart_alt:",
                     key="hc_reset"):
            st.session_state.hc_overrides = None
            st.rerun()
    if res.n_rows:
        st.dataframe(res.data.head(10), hide_index=True)

if res.n_rows == 0:
    st.error("No haircut records were detected. Use the column mapping above "
             "to point the app at the haircut column.",
             icon=":material/search_off:")
    st.stop()

st.dataframe(sup.checks_frame(engine.validate_haircut(res.data)),
             hide_index=True)

existing_names = {m["name"].lower(): m for m in masters}
name = st.text_input(
    "Save as", value=st.session_state.get("hc_save_name", ""),
    placeholder="IIFL", key="hc_save_name",
    help="Re-using an existing name replaces that master's records.")

clean = name.strip()
if clean and clean.lower() in existing_names:
    prior = existing_names[clean.lower()]
    st.warning(f"This will replace **{prior['name']}** and its "
               f"{prior['n_rows']:,} existing records.",
               icon=":material/swap_horiz:")

if st.button("Save to library", icon=":material/save:", type="primary",
             disabled=not clean):
    try:
        meta = store.save_master(clean, source_file, res.data)
    except hc.StorageError as e:
        st.error(str(e), icon=":material/database_off:")
    except Exception as e:
        st.error(f"Could not save: {type(e).__name__}.",
                 icon=":material/error:")
    else:
        sup.clear_library_cache()
        st.success(f"Saved **{meta['name']}** with {meta['n_rows']:,} records.",
                   icon=":material/check_circle:")
        st.session_state.hc_bytes = None
        st.session_state.hc_overrides = None
        st.rerun()
