"""
Operational view: which store the app is pointed at, whether it is reachable,
and whether the deployment settings are the safe ones.

Nothing on this page prints a credential.
"""
from __future__ import annotations

import os
import sys
import time

import streamlit as st

import app_support as sup
import haircut_core as hc
from haircut_core import store

st.title("Diagnostics")

# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

st.subheader("Storage")
target = store.target_description()
st.caption(target)

reachable, latency_ms, error = False, None, None
started = time.perf_counter()
try:
    store.ping()
    reachable = True
    latency_ms = (time.perf_counter() - started) * 1000
except hc.StorageError as e:
    error = str(e)
except Exception as e:
    error = f"{type(e).__name__}"

with st.container(horizontal=True):
    st.metric("Backend", store.backend().upper(), border=True)
    st.metric("Reachable", "Yes" if reachable else "No", border=True)
    st.metric("Round trip", f"{latency_ms:.0f} ms" if reachable else "-",
              border=True)

if error:
    st.error(error, icon=":material/database_off:")
else:
    try:
        masters = sup.list_masters()
        st.metric("Saved masters", f"{len(masters):,}", border=True)
    except Exception as e:
        st.error(f"Connected, but listing masters failed: {type(e).__name__}.",
                 icon=":material/error:")

# --------------------------------------------------------------------------- #
# Configuration review
# --------------------------------------------------------------------------- #

st.subheader("Configuration")


truthy = sup.truthy

mysql = store.mysql_config()
allow_anon = truthy(os.environ.get("HAIRCUT_ALLOW_ANONYMOUS"))
allowlist = (os.environ.get("HAIRCUT_ALLOWED_DOMAINS", "") + "," +
             os.environ.get("HAIRCUT_ALLOWED_EMAILS", "")).strip(",")

rows = [
    {"Setting": "Database backend",
     "Value": "MySQL" if mysql else "SQLite (local file)",
     "Safe for hosting": bool(mysql)},
    {"Setting": "Database TLS",
     "Value": ("CA-verified" if mysql and mysql.get("ssl", {}).get("ca")
               else "Encrypted, unverified" if mysql and "ssl" in mysql
               else "Off" if mysql else "Not applicable"),
     "Safe for hosting": bool(mysql and "ssl" in mysql)},
    {"Setting": "Database password set",
     "Value": "Yes" if (mysql or {}).get("password") else "No",
     "Safe for hosting": bool((mysql or {}).get("password"))},
    {"Setting": "Refuse SQLite fallback",
     "Value": "On" if truthy(os.environ.get("HAIRCUT_REQUIRE_MYSQL")) else "Off",
     "Safe for hosting": truthy(os.environ.get("HAIRCUT_REQUIRE_MYSQL"))},
    {"Setting": "Sign-in required",
     "Value": "No - anonymous access" if allow_anon else "Yes - SSO",
     "Safe for hosting": not allow_anon},
    {"Setting": "Account allowlist",
     "Value": allowlist or "Empty",
     "Safe for hosting": bool(allowlist) or allow_anon},
]

st.dataframe(
    rows, hide_index=True,
    column_config={
        "Setting": st.column_config.TextColumn(pinned=True),
        "Safe for hosting": st.column_config.CheckboxColumn(
            "Safe for hosting"),
    })

unsafe = [r["Setting"] for r in rows if not r["Safe for hosting"]]
if unsafe:
    st.warning("Not ready for a public deployment: "
               + ", ".join(unsafe).lower() + ".", icon=":material/warning:")
else:
    st.success("All deployment settings are in their safe state.",
               icon=":material/verified_user:")

with st.expander("How each setting is supplied",
                 icon=":material/help_outline:"):
    st.markdown(
        "Every value comes from an environment variable, or from the matching "
        "key in the app's secrets. Real environment variables always win, so a "
        "platform secret is never shadowed by a file.\n\n"
        "| Variable | Purpose |\n|---|---|\n"
        "| `HAIRCUT_DB_HOST` `_PORT` `_USER` `_PASSWORD` `_NAME` | MySQL "
        "connection |\n"
        "| `HAIRCUT_DB_SSL_CA` | Path to a CA bundle - TLS with verification |\n"
        "| `HAIRCUT_DB_SSL` | `1` for TLS without CA verification |\n"
        "| `HAIRCUT_REQUIRE_MYSQL` | `1` to refuse the SQLite fallback |\n"
        "| `HAIRCUT_ALLOWED_DOMAINS` | Comma-separated email domains that may "
        "sign in |\n"
        "| `HAIRCUT_ALLOWED_EMAILS` | Comma-separated individual addresses |\n"
        "| `HAIRCUT_ALLOW_ANONYMOUS` | `1` disables sign-in - local "
        "development only |\n")

# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #

st.subheader("Environment")
packages = {}
for mod in ("streamlit", "pandas", "numpy", "rapidfuzz", "openpyxl",
            "pymysql", "pdfplumber", "xlrd", "altair", "authlib"):
    try:
        packages[mod] = __import__(mod).__version__
    except ImportError:
        packages[mod] = "not installed"
    except AttributeError:
        packages[mod] = "installed"

st.dataframe(
    [{"Package": k, "Version": v} for k, v in packages.items()],
    hide_index=True, width="content")
st.caption(f"Python {sys.version.split()[0]} - signed in as "
           f"{st.session_state.get('user_email', 'unknown')}")

if st.button("Clear cached data", icon=":material/refresh:"):
    sup.clear_library_cache()
    st.cache_data.clear()
    st.success("Caches cleared.", icon=":material/check_circle:")
