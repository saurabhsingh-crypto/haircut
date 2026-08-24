# Haircut Margin Calculator

Collateral margin for client portfolios, against a broker haircut master.
Upload any broker's holdings file, correct the column mapping if the
auto-detection got it wrong, and get per-client haircut and available margin
with a full Excel export.

Streamlit front end, MySQL for the saved haircut library.

## Layout

```
streamlit_app.py        entry point: config, sign-in gate, navigation
app_support.py          cached core calls, access control, mapping editor
app_pages/
    calculate.py        upload -> map -> calculate -> dashboard -> export
    library.py          the saved haircut masters
    diagnostics.py      storage health and a deployment-readiness check
haircut_core/           framework-free business logic (imports no Streamlit)
    schema.py           the canonical portfolio / haircut tables
    ingest.py           read a file, profile its columns, normalise it
    pipeline.py         file bytes -> canonical DataFrame, with overrides
    engine.py           join holdings to a master, compute margin
    store.py            the saved-haircut library (MySQL / SQLite)
    report.py           Excel export
    templates.py        known broker layouts that skip column detection
assets/
    iifl_haircut_master.xlsx   seeds an empty library on first run
.streamlit/
    config.toml         committed; holds no secrets
    secrets.toml.example  copy to secrets.toml and fill in
legacy/                 the superseded FastAPI build - do not deploy
```

`haircut_core` has no web-framework dependency, so the same code runs from a
notebook or a test as it does behind the web UI.

## Run it locally

```bash
pip install -r requirements.txt
```

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill
in your database details. For local work you can skip the OAuth setup:

```toml
HAIRCUT_ALLOW_ANONYMOUS = "1"
```

Then:

```bash
streamlit run streamlit_app.py
```

`HAIRCUT_ALLOW_ANONYMOUS` disables the sign-in gate entirely. Never set it on
a deployed app. With it unset and no `[auth]` block configured, the app
refuses to let anyone in rather than falling open.

With no database configured the app falls back to a local SQLite file so it
runs with zero setup. That file does **not** survive a redeploy on a hosted
platform, which is why `HAIRCUT_REQUIRE_MYSQL=1` exists.

## Deploy to Streamlit Community Cloud

### 1. Make MySQL reachable and encrypted

Community Cloud runs outside your network, so the database must accept
connections from the internet and must use TLS.

- Allow inbound 3306 from Streamlit's egress addresses, or from anywhere if
  your provider gives you no narrower option.
- Create a dedicated user, not `root`:

  ```sql
  CREATE USER 'haircut_app'@'%' IDENTIFIED BY '<a long random password>' REQUIRE SSL;
  GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, ALTER ON haircut.* TO 'haircut_app'@'%';
  ```

  `CREATE`, `INDEX` and `ALTER` are needed on first run so the app can create
  its tables and apply the schema migration. You can revoke them afterwards.
- Get your provider's CA certificate, commit it as `certs/mysql-ca.pem`, and
  point `HAIRCUT_DB_SSL_CA` at it. That verifies the server, not just the
  channel. If you cannot get a CA file, set `HAIRCUT_DB_SSL = "1"` instead -
  encrypted but unverified, which is still far better than plaintext.

### 2. Register an OAuth client

You need the app's public URL first, so deploy before doing this. The app
will come up showing "Single sign-on is not fully configured" and naming what
is missing - that is it failing closed, as intended. Note the URL it gives
you; your redirect URI is that URL plus `/oauth2callback`.

**Google**

1. `console.cloud.google.com` -> **APIs & Services** -> **OAuth consent
   screen**. Choose **Internal** if your domain is on Google Workspace: it
   restricts sign-in to your organisation and needs no verification review.
   **External** works too but sits in Testing mode, capped at 100 manually
   added test users, until you submit for verification.
2. **Credentials** -> **Create Credentials** -> **OAuth client ID** ->
   application type **Web application**.
3. Under *Authorised redirect URIs* add both, so one client covers hosted and
   local use:

   ```
   https://<your-app>.streamlit.app/oauth2callback
   http://localhost:8501/oauth2callback
   ```

4. Copy the **Client ID** and **Client secret**.

**Microsoft Entra ID**

1. `portal.azure.com` -> **Microsoft Entra ID** -> **App registrations** ->
   **New registration**. Account types: *this organizational directory only*.
2. Redirect URI: platform **Web**, value
   `https://<your-app>.streamlit.app/oauth2callback`. Add the localhost one
   afterwards under **Authentication**.
3. **Certificates & secrets** -> **New client secret**. Copy the value
   immediately; it is shown once.
4. **Overview** gives the **Application (client) ID** and **Directory
   (tenant) ID**. Your metadata URL is then
   `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration`.

The redirect URI must match character for character - scheme included, no
trailing slash - and must end with `/oauth2callback`, which Streamlit checks.

| Symptom | Cause |
|---|---|
| `redirect_uri_mismatch` | The console URI and `redirect_uri` in secrets differ |
| Redirects back but still signed out | `cookie_secret` missing, or changed between restarts |
| "not approved for this app" | Sign-in worked; the address is not on the allowlist |
| `access_denied` on an External Google client | The account is not in the test-users list |

### 3. Set the app's secrets

Paste this into the app's secrets box (Manage app -> Settings -> Secrets):

**Plain keys must come before any `[table]` header.** Everything below
`[auth]` becomes part of `[auth]`, so keep that block last:

```toml
HAIRCUT_ALLOWED_DOMAINS = "yourcompany.com"

HAIRCUT_DB_HOST = "..."
HAIRCUT_DB_PORT = "3306"
HAIRCUT_DB_USER = "haircut_app"
HAIRCUT_DB_PASSWORD = "..."
HAIRCUT_DB_NAME = "haircut"
HAIRCUT_DB_SSL_CA = "certs/mysql-ca.pem"
HAIRCUT_REQUIRE_MYSQL = "1"

[auth]
redirect_uri = "https://<your-app>.streamlit.app/oauth2callback"
cookie_secret = "<python -c 'import secrets; print(secrets.token_hex(32))'>"
client_id = "..."
client_secret = "..."
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

If you get this wrong the app still reads the settings, but warns in the
sidebar - otherwise it would quietly fall back to SQLite and lose your saved
masters on the next restart.

**`HAIRCUT_ALLOWED_DOMAINS` is not optional.** Google will authenticate any
Gmail account on the internet; the allowlist is what actually restricts
access. With both allowlist keys empty, every account is refused - the app
fails closed, not open.

### 4. Check it

Open the **Diagnostics** page. Every row under "Safe for hosting" should be
ticked. If any is not, the page names it.

## Configuration reference

Every setting comes from an environment variable or the matching key in the
app's secrets. Real environment variables always win, so a platform secret is
never shadowed by a file committed by mistake.

| Variable | Default | Purpose |
|---|---|---|
| `HAIRCUT_DB_HOST` | - | MySQL host. Unset means the SQLite fallback. |
| `HAIRCUT_DB_PORT` | `3306` | |
| `HAIRCUT_DB_USER` | `root` | |
| `HAIRCUT_DB_PASSWORD` | empty | |
| `HAIRCUT_DB_NAME` | `haircut` | |
| `HAIRCUT_DB_SSL_CA` | - | Path to a CA bundle. TLS with server verification. |
| `HAIRCUT_DB_SSL` | - | `1` for TLS without verification. |
| `HAIRCUT_REQUIRE_MYSQL` | - | `1` refuses to start on the SQLite fallback. |
| `HAIRCUT_DB` | next to the app | SQLite file path, local development only. |
| `HAIRCUT_ALLOWED_DOMAINS` | - | Comma-separated email domains that may sign in. |
| `HAIRCUT_ALLOWED_EMAILS` | - | Comma-separated individual addresses. |
| `HAIRCUT_ALLOW_ANONYMOUS` | - | `1` disables sign-in. Local development only. |

Upload size is capped at 25 MB in `.streamlit/config.toml`
(`server.maxUploadSize`).

## Database schema

The app creates and migrates its own tables on first run.

```sql
masters(slug PK, name, source_file, n_rows, created DATETIME, updated DATETIME)
haircut_rows(id PK, slug, isin, scheme_name, haircut_pct)
```

Saving a master under a name that already exists replaces that master's rows
in a single transaction - that is how you refresh a broker's haircut file each
month.

A database created by an earlier version is migrated in place on startup:
`haircut_rows` gains its primary key and the `masters` timestamps become
`DATETIME`. Both steps check before acting, so restarts are no-ops. The
migration is MySQL-only; SQLite cannot add a primary key with `ALTER TABLE`,
and a fresh file already gets the current schema.

Timestamps are stored in UTC.

## Notes on behaviour

- **Matching order.** ISIN first, then exact normalised scheme name, then
  fuzzy scheme name above the threshold. Anything left over is reported as
  unmatched and handled by the chosen policy.
- **Fuzzy matching cost** scales with the number of *distinct* unmatched
  scheme names, not the number of holdings, because the comparison is batched
  per unique name. 20,000 holdings across 300 distinct funds takes a few
  seconds against a 9,000-row master.
- **Sessions** are Streamlit's own, one per browser tab. Nothing is shared
  between users, and the only durable state is the haircut library in MySQL.
- **Duplicate ISINs** in a master are kept and calculated with the first
  occurrence; conflicting values are listed on the Checks tab.

## Housekeeping

`.env` from the previous build is no longer read by anything. Delete it, and
rotate the password it contains if that file has ever been on a deployed
machine or in a shared folder.

`legacy/` holds the superseded FastAPI build for reference only. Delete it
once you are happy with this app.
