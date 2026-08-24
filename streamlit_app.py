"""
Haircut Margin Calculator - Streamlit entry point.

Responsibilities, in order:
  1. Push configuration from `st.secrets` into the environment so the
     framework-free `haircut_core` package can read it.
  2. Gate the app behind single sign-on, including an allowlist - OIDC only
     proves *who* someone is, not that they are allowed in here.
  3. Register navigation and hand off to a page.

Run locally:   streamlit run streamlit_app.py
"""
from __future__ import annotations

import os

import streamlit as st

st.set_page_config(
    page_title="Haircut margin calculator",
    page_icon=":material/content_cut:",
    layout="wide",
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Keys that haircut_core reads from the environment. Anything else in secrets
# (the [auth] block, for instance) is left for Streamlit itself to consume.
_CONFIG_KEYS = (
    "HAIRCUT_DB_HOST", "HAIRCUT_DB_PORT", "HAIRCUT_DB_USER",
    "HAIRCUT_DB_PASSWORD", "HAIRCUT_DB_NAME", "HAIRCUT_DB_SSL",
    "HAIRCUT_DB_SSL_CA", "HAIRCUT_REQUIRE_MYSQL", "HAIRCUT_DB",
    "HAIRCUT_ALLOWED_DOMAINS", "HAIRCUT_ALLOWED_EMAILS",
    "HAIRCUT_ALLOW_ANONYMOUS",
)


def read_secrets() -> dict:
    """Secrets as a plain dict, or {} when no secrets file exists.

    Accessing `st.secrets` raises when there is no secrets file at all, which
    is the normal state on a fresh clone - so this must never propagate.
    """
    try:
        return dict(st.secrets)
    except Exception:
        return {}


SECRETS = read_secrets()

# A settings key placed after the [auth] header in secrets.toml becomes part of
# the auth table, because that is how TOML works. Left undetected the app would
# quietly fall back to SQLite and lose every saved master on the next restart,
# so accept those keys from either place and say so.
MISPLACED_KEYS = [k for k in _CONFIG_KEYS
                  if k not in SECRETS and k in (SECRETS.get("auth") or {})]

# Real environment variables win over the secrets file, so a platform-provided
# secret is never shadowed by a file committed by mistake.
for _key in _CONFIG_KEYS:
    _val = SECRETS.get(_key)
    if _val is None:
        _val = (SECRETS.get("auth") or {}).get(_key)
    if _val is not None:
        os.environ.setdefault(_key, str(_val))


# Access-control helpers live in app_support so they can be unit-tested
# without executing a page.
import app_support as sup  # noqa: E402  (must follow the env setup above)

ALLOWED_DOMAINS = sup.allowed_domains()
ALLOWED_EMAILS = sup.allowed_emails()
ALLOW_ANONYMOUS = sup.allow_anonymous()
AUTH_PROBLEMS = sup.auth_config_problems(SECRETS.get("auth"))
AUTH_CONFIGURED = not AUTH_PROBLEMS


def is_allowed(email: str | None) -> bool:
    return sup.is_allowed(email, ALLOWED_DOMAINS, ALLOWED_EMAILS)


# --------------------------------------------------------------------------- #
# Sign-in gate
# --------------------------------------------------------------------------- #

def login_screen() -> None:
    st.title("Haircut margin calculator")
    st.caption("Collateral margin against a broker haircut master")

    if not AUTH_CONFIGURED:
        st.error(
            "Single sign-on is not fully configured, so the app cannot let "
            "anyone in.", icon=":material/lock:")
        with st.container(border=True):
            st.markdown("**What is still needed**")
            for problem in AUTH_PROBLEMS:
                st.markdown(f"- {problem}")
            st.markdown(
                "Add these to **Manage app -> Settings -> Secrets** when "
                "hosted, or to `.streamlit/secrets.toml` locally. "
                "`.streamlit/secrets.toml.example` has a working template."
            )
            st.warning(
                "Ordering matters. In TOML every plain key *below* a "
                "`[table]` header belongs to that table, so the "
                "`HAIRCUT_*` settings must come **above** `[auth]`. Put them "
                "the other way round and they are read as part of `[auth]`, "
                "and the app silently falls back to a temporary database.",
                icon=":material/warning:")
            st.code(
                'HAIRCUT_ALLOWED_DOMAINS = "yourcompany.com"\n'
                '\n'
                '# ... any HAIRCUT_DB_* settings go here too, above [auth] ...\n'
                '\n'
                '[auth]\n'
                'redirect_uri = '
                '"https://<your-app>.streamlit.app/oauth2callback"\n'
                'cookie_secret = "<run: python -c \'import secrets;'
                ' print(secrets.token_hex(32))\'>"\n'
                'client_id = "<from your identity provider>"\n'
                'client_secret = "<from your identity provider>"\n'
                'server_metadata_url = "https://accounts.google.com/'
                '.well-known/openid-configuration"\n',
                language="toml")
            st.caption(
                "Locally, `redirect_uri` is "
                "`http://localhost:8501/oauth2callback` instead. Register both "
                "with your identity provider and one client covers each.")
            st.caption(
                "For local development only, set "
                "`HAIRCUT_ALLOW_ANONYMOUS = \"1\"` to skip sign-in. Never set "
                "it on a hosted deployment.")
        st.stop()

    with st.container(border=True):
        st.subheader("Sign in to continue")
        st.write("This app holds client holding data, so access is restricted "
                 "to approved accounts.")
        if st.button("Sign in", icon=":material/login:", type="primary"):
            st.login()
    st.stop()


def denied_screen(email: str | None) -> None:
    st.title("Haircut margin calculator")
    st.error(
        f"The account {email or 'you signed in with'} is not approved for this "
        "app.", icon=":material/block:")
    if not (ALLOWED_DOMAINS or ALLOWED_EMAILS):
        st.warning(
            "No allowlist is configured. Set `HAIRCUT_ALLOWED_DOMAINS` (for "
            "example `yourcompany.com`) or `HAIRCUT_ALLOWED_EMAILS` in the "
            "app's secrets, otherwise every account is refused.",
            icon=":material/warning:")
    else:
        st.caption("Ask an administrator to add your account to the allowlist.")
    if st.button("Sign out", icon=":material/logout:"):
        st.logout()
    st.stop()


if ALLOW_ANONYMOUS:
    USER_EMAIL = "anonymous@localhost"
    USER_NAME = "Local user"
else:
    if not AUTH_CONFIGURED or not st.user.is_logged_in:
        login_screen()
    USER_EMAIL = getattr(st.user, "email", None)
    USER_NAME = getattr(st.user, "name", None) or USER_EMAIL
    if not is_allowed(USER_EMAIL):
        denied_screen(USER_EMAIL)

# Signed in and approved. Pages read these instead of touching st.user.
st.session_state["user_email"] = USER_EMAIL
st.session_state["user_name"] = USER_NAME

# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.subheader("Haircut margin")
    st.caption(f"Signed in as {USER_NAME}")
    if MISPLACED_KEYS:
        st.warning(
            f"{len(MISPLACED_KEYS)} setting(s) sit below the `[auth]` header in "
            f"your secrets, so TOML treats them as part of it: "
            f"`{'`, `'.join(MISPLACED_KEYS)}`. They were still read, but move "
            f"them above `[auth]` - a plain key must precede any table header.",
            icon=":material/warning:")
    if ALLOW_ANONYMOUS:
        st.warning("Sign-in is disabled (`HAIRCUT_ALLOW_ANONYMOUS`). Use this "
                   "for local development only.", icon=":material/warning:")
    elif st.button("Sign out", icon=":material/logout:", width="stretch"):
        st.logout()

page = st.navigation([
    st.Page("app_pages/calculate.py", title="Calculate margin",
            icon=":material/calculate:", default=True),
    st.Page("app_pages/library.py", title="Haircut library",
            icon=":material/library_books:"),
    st.Page("app_pages/diagnostics.py", title="Diagnostics",
            icon=":material/monitor_heart:"),
])
page.run()
