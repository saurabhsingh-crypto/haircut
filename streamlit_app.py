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
    "HAIRCUT_ADMIN_EMAILS", "HAIRCUT_ALLOW_ANONYMOUS",
    "HAIRCUT_ANONYMOUS_EMAIL", "HAIRCUT_ADMIN_PASSWORD",
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


def host_identity() -> tuple[str | None, str | None]:
    """The viewer's identity as supplied by the hosting platform, if any.

    `st.user` is populated by the host, not only by our own OIDC config, so a
    platform that authenticates viewers itself (Streamlit Community Cloud for
    a private app, for instance) can still tell us who is here. Where it does
    not, both values are None and we simply have no identity.
    """
    try:
        return (getattr(st.user, "email", None) or None,
                getattr(st.user, "name", None) or None)
    except Exception:
        return None, None


if ALLOW_ANONYMOUS:
    # No sign-in screen at all. Identity, if we get one, comes from the host
    # platform; HAIRCUT_ANONYMOUS_EMAIL is a stand-in for local development so
    # the admin checks remain testable. With neither, there is no identity -
    # the audit log records "unknown" and nobody is an admin.
    USER_EMAIL, USER_NAME = host_identity()
    USER_EMAIL = USER_EMAIL or os.environ.get("HAIRCUT_ANONYMOUS_EMAIL") or None
    USER_NAME = USER_NAME or USER_EMAIL or "Anonymous"
else:
    if not AUTH_CONFIGURED or not st.user.is_logged_in:
        login_screen()
    USER_EMAIL = getattr(st.user, "email", None)
    USER_NAME = getattr(st.user, "name", None) or USER_EMAIL
    if not is_allowed(USER_EMAIL):
        denied_screen(USER_EMAIL)

# Pages read these instead of touching st.user.
#
# An admin who unlocked with the password stays unlocked for the rest of the
# session. This has to be checked here, before the identity is written: the
# script re-runs on every interaction, and recomputing is_admin from the
# (absent) identity each time would undo the unlock immediately.
UNLOCKED_AS = st.session_state.get("admin_unlocked_as")
if UNLOCKED_AS:
    st.session_state["user_email"] = UNLOCKED_AS
    st.session_state["user_name"] = UNLOCKED_AS
    st.session_state["is_admin"] = True
else:
    st.session_state["user_email"] = USER_EMAIL
    st.session_state["user_name"] = USER_NAME
    st.session_state["is_admin"] = sup.is_admin(USER_EMAIL)

# One audit row per session, not per rerun: the script re-executes on every
# interaction, so this is guarded rather than called unconditionally.
if not st.session_state.get("signin_logged"):
    st.session_state["signin_logged"] = True
    sup.log_event(USER_EMAIL, sup.SIGN_IN)

# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.subheader("Haircut margin")
    # Only worth saying when there is an identity to name. With sign-in off
    # and no identity, "Signed in as Anonymous" is noise on every page.
    if USER_EMAIL:
        st.caption(f"Signed in as {USER_NAME}")
    if MISPLACED_KEYS:
        st.warning(
            f"{len(MISPLACED_KEYS)} setting(s) sit below the `[auth]` header in "
            f"your secrets, so TOML treats them as part of it: "
            f"`{'`, `'.join(MISPLACED_KEYS)}`. They were still read, but move "
            f"them above `[auth]` - a plain key must precede any table header.",
            icon=":material/warning:")
    if not ALLOW_ANONYMOUS and st.button("Sign out", icon=":material/logout:",
                                         width="stretch"):
        st.logout()

    # --- admin unlock, for deployments with no identity provider ------------ #
    # A typed password is weaker than a real sign-in: it can be shared and it
    # does not expire. It is the second lock, not the first - reaching the app
    # at all is still controlled by the host's viewer list.
    ADMIN_LIST = sorted(sup.admin_emails())
    if ADMIN_LIST and sup.admin_password() and not st.session_state["is_admin"]:
        st.session_state.setdefault("admin_tries", 0)
        with st.expander("Admin", icon=":material/lock:"):
            if st.session_state["admin_tries"] >= 5:
                st.error("Too many attempts. Reload the page to try again.",
                         icon=":material/block:")
            else:
                who = st.selectbox("Who are you?", ADMIN_LIST, key="admin_who")
                pw = st.text_input("Admin password", type="password",
                                   key="admin_pw")
                if st.button("Unlock", icon=":material/key:", width="stretch"):
                    if sup.check_admin_password(pw):
                        st.session_state["admin_unlocked_as"] = who
                        st.session_state["admin_tries"] = 0
                        sup.log_event(who, sup.ADMIN_UNLOCK)
                        st.rerun()
                    else:
                        st.session_state["admin_tries"] += 1
                        sup.log_event(who, sup.ADMIN_FAILED,
                                      f"attempt {st.session_state['admin_tries']}")
                        st.error("Incorrect password.", icon=":material/error:")
    elif st.session_state["is_admin"] and ALLOW_ANONYMOUS:
        st.caption(f":material/lock_open: Admin: {st.session_state['user_email']}")
        if st.button("Lock admin", icon=":material/lock:", width="stretch"):
            st.session_state.pop("admin_unlocked_as", None)
            st.rerun()

pages = [
    st.Page("app_pages/calculate.py", title="Calculate margin",
            icon=":material/calculate:", default=True),
    st.Page("app_pages/library.py", title="Haircut library",
            icon=":material/library_books:"),
]

# Diagnostics is registered only for admins. This is real access control, not
# a hidden link: st.navigation resolves the URL against the page list built
# for THIS session, so a page that was never registered cannot be reached by
# typing its path - the request falls back to the default page.
if st.session_state["is_admin"]:
    pages.append(st.Page("app_pages/diagnostics.py", title="Diagnostics",
                         icon=":material/monitor_heart:"))

page = st.navigation(pages)
page.run()
