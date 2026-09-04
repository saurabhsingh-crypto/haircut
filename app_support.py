"""
Shared plumbing between the Streamlit pages.

Everything Streamlit-aware that is not page layout lives here: cached calls
into `haircut_core`, one-time storage bootstrap, the column-mapping editor,
and the currency helpers used across pages.
"""
from __future__ import annotations

import hmac
import io
import json
import os

import pandas as pd
import streamlit as st

import haircut_core as hc
from haircut_core import engine, pipeline, schema, store

SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "assets", "iifl_haircut_master.xlsx")
SEED_NAME = "IIFL"

USER_MODES = ["auto", "column", "marker", "title", "single"]
USER_MODE_HELP = {
    "auto": "Let the app decide",
    "column": "A column holds the client code",
    "marker": "Rows above each block name the client",
    "title": "The sheet or file name is the client",
    "single": "The whole file is one client",
}

MISSING_POLICIES = {
    "zero": "Treat as 0% haircut (full margin)",
    "fallback": "Use the haircut in the portfolio file, if it has one",
}


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #

def truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def csv_set(value) -> set[str]:
    return {p.strip().lower() for p in str(value or "").split(",") if p.strip()}


_AUTH_REQUIRED = ("redirect_uri", "cookie_secret", "client_id",
                  "client_secret", "server_metadata_url")

# Substrings that mean "you were meant to replace this".
_PLACEHOLDER_HINTS = ("paste", "replace", "your-", "your_", "xxx", "<", "changeme")


def auth_config_problems(auth: dict | None) -> list[str]:
    """What is still missing from the [auth] block, in plain language.

    Without this an incomplete secrets file reaches Authlib and fails with an
    opaque provider error. Naming the specific field is the difference between
    a two-minute fix and an afternoon.
    """
    if not auth:
        return ["The [auth] block is missing entirely."]
    problems = []
    for key in _AUTH_REQUIRED:
        raw = auth.get(key)
        val = "" if raw is None else str(raw).strip()
        if not val:
            problems.append(f"`{key}` is missing or empty.")
        elif any(h in val.lower() for h in _PLACEHOLDER_HINTS):
            problems.append(f"`{key}` still holds the placeholder value.")
    uri = str(auth.get("redirect_uri") or "")
    if uri and not uri.endswith("/oauth2callback"):
        problems.append("`redirect_uri` must end with `/oauth2callback`.")
    return problems


def allowed_domains() -> set[str]:
    return csv_set(os.environ.get("HAIRCUT_ALLOWED_DOMAINS"))


def allowed_emails() -> set[str]:
    return csv_set(os.environ.get("HAIRCUT_ALLOWED_EMAILS"))


def admin_emails() -> set[str]:
    """Addresses allowed to see Diagnostics and delete saved masters."""
    return csv_set(os.environ.get("HAIRCUT_ADMIN_EMAILS"))


def allow_anonymous() -> bool:
    return truthy(os.environ.get("HAIRCUT_ALLOW_ANONYMOUS"))


def admin_password() -> str:
    """The shared password that unlocks admin rights, or "" if unset."""
    return (os.environ.get("HAIRCUT_ADMIN_PASSWORD") or "").strip()


def check_admin_password(candidate: str | None) -> bool:
    """Compare in constant time, so the password cannot be guessed by timing.

    An unset password unlocks nothing: without this the empty string would
    match an empty setting and hand admin rights to anyone who pressed the
    button.
    """
    expected = admin_password()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(str(candidate).strip(), expected)


def normalize_email(email: str | None) -> str | None:
    """A well-formed address, lower-cased, or None.

    Shared by every access check. Without the structural test a bare domain
    such as "company.com" would satisfy a domain comparison, because
    rpartition returns the whole string when the separator is absent.
    """
    if not email:
        return None
    email = str(email).strip().lower()
    local, at, domain = email.partition("@")
    if not at or not local or not domain or "@" in domain:
        return None
    return email


def is_allowed(email: str | None, domains: set[str] | None = None,
               emails: set[str] | None = None) -> bool:
    """Whether an authenticated identity is on the allowlist.

    With no allowlist configured nobody gets in. A public identity provider
    will happily authenticate any account on the internet, so an empty
    allowlist is a misconfiguration, not an invitation.
    """
    email = normalize_email(email)
    if not email:
        return False
    domains = allowed_domains() if domains is None else domains
    emails = allowed_emails() if emails is None else emails
    return email in emails or email.rpartition("@")[2] in domains


def is_admin(email: str | None, admins: set[str] | None = None) -> bool:
    """Whether this identity may see Diagnostics and delete saved masters.

    Admin is a strict subset of signed-in: being on the allowlist gets you
    into the app, being on this list gets you the destructive controls. An
    empty admin list grants nobody, matching how the sign-in gate behaves.

    This depends only on the address, never on how it was obtained, so it
    works the same whether the identity came from our own OIDC sign-in or
    from the hosting platform. With no identity at all - sign-in disabled and
    a host that supplies none - nobody is an admin, and the audit log is read
    directly from the database instead.
    """
    email = normalize_email(email)
    if not email:
        return False
    return email in (admin_emails() if admins is None else admins)


# --------------------------------------------------------------------------- #
# Storage bootstrap
# --------------------------------------------------------------------------- #

@st.cache_resource(show_spinner=False)
def bootstrap_storage() -> dict:
    """Create tables and seed the bundled IIFL master, once per process.

    Returns a status dict rather than raising: a database problem should show
    up as a readable message on the page, not a stack trace over the whole app.
    """
    status = {"ok": False, "target": store.target_description(),
              "warnings": [], "error": None, "seeded": False,
              "migrations": []}
    try:
        status["warnings"] = store.check_config()
    except hc.StorageError as e:
        status["error"] = str(e)
        return status

    try:
        store.init_schema()
        existing = store.list_masters()
    except hc.StorageError as e:
        status["error"] = str(e)
        return status
    except Exception as e:
        status["error"] = (f"Could not reach {status['target']}: "
                           f"{type(e).__name__}.")
        return status

    status["ok"] = True
    status["migrations"] = list(store.MIGRATIONS)
    # Retention, applied once per process rather than on a schedule: the table
    # is tiny, but it should not grow without bound either.
    store.prune_events(keep_days=365)
    if existing or not os.path.exists(SEED_FILE):
        return status

    # Empty library: seed the bundled master so the app is usable immediately.
    try:
        with open(SEED_FILE, "rb") as fh:
            raw = fh.read()
        res = pipeline.standardize(io.BytesIO(raw),
                                   "IIFL Haircut Master.xlsx", "haircut")
        if res.n_rows:
            store.save_master(SEED_NAME, "IIFL Haircut Master.xlsx", res.data,
                              actor="system (first-run seed)")
            status["seeded"] = True
    except Exception as e:
        status["warnings"].append(
            f"Could not seed the bundled IIFL master: {type(e).__name__}. "
            f"Upload a haircut master on the Haircut library page.")
    return status


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #

ADMIN_UNLOCK = "admin_unlock"
ADMIN_FAILED = "admin_failed"

SIGN_IN = store.SIGN_IN
MASTER_SAVED = store.MASTER_SAVED
MASTER_REPLACED = store.MASTER_REPLACED
MASTER_DELETED = store.MASTER_DELETED


def log_event(email: str | None, event: str, detail: str = "") -> None:
    """Record an audit event. Never raises, never blocks the caller's action."""
    store.log_event(email, event, detail)


def current_user() -> str | None:
    return st.session_state.get("user_email")


def require_storage() -> dict:
    """Bootstrap storage, or stop the page with a readable error."""
    status = bootstrap_storage()
    if status["error"]:
        st.error(status["error"], icon=":material/database_off:")
        st.caption("Check the Diagnostics page for the active configuration.")
        st.stop()
    return status


# --------------------------------------------------------------------------- #
# Cached core calls
# --------------------------------------------------------------------------- #

@st.cache_data(ttl="10m", max_entries=4, show_spinner=False)
def list_masters() -> list[dict]:
    return store.list_masters()


@st.cache_data(ttl="30m", max_entries=6, show_spinner=False)
def load_master(slug: str) -> pd.DataFrame:
    return store.load_master(slug)


def clear_library_cache() -> None:
    list_masters.clear()
    load_master.clear()
    calculate.clear()


@st.cache_data(ttl="30m", max_entries=6, show_spinner=False)
def standardize(file_bytes: bytes, filename: str, target: str,
                overrides_json: str = "") -> object:
    """Standardise an uploaded file, optionally applying manual overrides.

    `overrides_json` is part of the cache key, so re-picking the same mapping
    is instant while a changed mapping recomputes.
    """
    base = pipeline.standardize(io.BytesIO(file_bytes), filename, target)
    overrides = json.loads(overrides_json) if overrides_json else None
    if overrides and len(overrides) == len(base.plans) and any(overrides):
        return pipeline.apply_overrides(base, overrides)
    return base


@st.cache_data(ttl="30m", max_entries=4, show_spinner=False)
def calculate(file_bytes: bytes, filename: str, overrides_json: str,
              slug: str, policy: str, threshold: float) -> object:
    """Standardise the portfolio, then compute margin against a saved master."""
    pf = standardize(file_bytes, filename, "portfolio", overrides_json)
    return engine.compute(pf.data, store.load_master(slug),
                          missing_policy=policy, scheme_threshold=threshold)


# --------------------------------------------------------------------------- #
# Column-mapping editor
# --------------------------------------------------------------------------- #

def _column_labels(df: pd.DataFrame) -> dict[int, str]:
    """A readable label per raw column: its position and first non-empty cell."""
    labels = {}
    for c in range(df.shape[1]):
        sample = ""
        for v in df[c].tolist():
            text = "" if v is None else str(v).strip()
            if text and text.lower() != "nan":
                sample = text
                break
        labels[c] = f"col {c}" + (f" - {sample[:34]}" if sample else "")
    return labels


def _field_label(name: str) -> str:
    return name.replace("_", " ").replace("pct", "%").strip().capitalize()


def mapping_editor(res, target: str, key_prefix: str) -> list:
    """Render the per-sheet mapping controls; return an overrides list.

    The returned list is parallel to `res.plans`, in the shape
    `pipeline.apply_overrides` expects.
    """
    fields = (schema.PORTFOLIO_FIELDS if target == "portfolio"
              else schema.HAIRCUT_FIELDS)
    overrides = []

    for i, plan in enumerate(res.plans):
        df = plan.grid.data
        labels = _column_labels(df)
        options = [None] + list(labels)
        current = plan.fieldmap.columns or {}

        if len(res.plans) > 1:
            st.markdown(f"**{plan.grid.name}**")
        for note in (plan.fieldmap.notes or []):
            st.caption(f":material/info: {note}")

        cols = st.columns(3)
        picked = {}
        for j, (fname, spec) in enumerate(fields.items()):
            cur = current.get(fname)
            label = _field_label(fname) + (" *" if spec.get("required") else "")
            with cols[j % 3]:
                picked[fname] = st.selectbox(
                    label,
                    options,
                    index=options.index(cur) if cur in options else 0,
                    format_func=lambda c: "- not mapped -" if c is None
                    else labels.get(c, f"col {c}"),
                    key=f"{key_prefix}_{i}_{fname}",
                )

        user_mode = None
        if target == "portfolio":
            cur_mode = plan.fieldmap.user_mode or "auto"
            user_mode = st.selectbox(
                "How is the client identified?",
                USER_MODES,
                index=USER_MODES.index(cur_mode) if cur_mode in USER_MODES else 0,
                format_func=lambda m: f"{m} - {USER_MODE_HELP[m]}",
                key=f"{key_prefix}_{i}_usermode",
            )

        overrides.append({
            "columns": {f: int(c) for f, c in picked.items() if c is not None},
            "user_mode": None if user_mode in (None, "auto") else user_mode,
        })

    return overrides


def overrides_key(overrides: list | None) -> str:
    """A stable cache key for an overrides list."""
    if not overrides or not any(overrides):
        return ""
    return json.dumps(overrides, sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# Display helpers
# --------------------------------------------------------------------------- #

def money(x) -> str:
    """Rupees, full precision, Indian digit grouping."""
    try:
        return engine.inr_full(float(x))
    except (TypeError, ValueError):
        return "-"


def money_compact(x) -> str:
    """Rupees as lakh / crore, for headline figures."""
    try:
        return engine.inr_compact(float(x))
    except (TypeError, ValueError):
        return "-"


@st.cache_data(ttl="30m", max_entries=4, show_spinner=False)
def manual_options(slug: str) -> dict:
    """One searchable label per security in a master, keyed by row index.

    Built once per master and cached: the browser filters this list as the
    user types, so it is sent whole rather than queried per keystroke.
    """
    df = store.load_master(slug)
    return {
        i: f"{r.isin}  -  {r.scheme_name}  -  {float(r.haircut_pct):.2f}%"
        for i, r in enumerate(df.itertuples(index=False))
    }


def manual_excel(result: pd.DataFrame) -> bytes:
    """The manual calculator's table as a one-sheet workbook."""
    labels = {"isin": "ISIN", "scheme_name": "Security Name",
              "haircut_pct": "Haircut %", "amount": "Amount",
              "haircut_amount": "Haircut Amount",
              "available_margin": "Available Margin"}
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        result.rename(columns=labels).to_excel(
            xw, sheet_name="Manual Calculation", index=False)
    return buf.getvalue()


def checks_frame(checks: list) -> pd.DataFrame:
    """Validation tuples -> a frame ready for st.dataframe."""
    return pd.DataFrame(
        [{"": "Pass" if ok else "Check", "Check": name, "Detail": detail}
         for name, ok, detail in checks])
