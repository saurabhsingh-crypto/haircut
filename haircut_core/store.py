"""
Saved-haircut library: the only durable state this app has.

Backends
--------
MySQL is the production backend and the only one that survives a redeploy.
SQLite is a local-development convenience. On a hosted platform the app
directory is usually ephemeral, so a silent SQLite fallback there would lose
every saved master on the next restart - set ``HAIRCUT_REQUIRE_MYSQL=1`` and
the app refuses to start rather than pretending to work.

Tables
------
    masters(slug PK, name, source_file, n_rows, created_utc, updated_utc)
    haircut_rows(id PK, slug, isin, scheme_name, haircut_pct)

``save_master`` is an UPSERT keyed on the slugified name: re-uploading the
same broker name DELETEs its old rows and inserts the new ones (a full
replace), in one transaction.

Configuration (environment variables, or Streamlit secrets via `configure`)
--------------------------------------------------------------------------
    HAIRCUT_DB_HOST       MySQL host. Unset -> SQLite fallback.
    HAIRCUT_DB_PORT       default 3306
    HAIRCUT_DB_USER       default root
    HAIRCUT_DB_PASSWORD
    HAIRCUT_DB_NAME       default haircut
    HAIRCUT_DB_SSL        "1" to require TLS with no CA verification
    HAIRCUT_DB_SSL_CA     path to a CA bundle; implies TLS with verification
    HAIRCUT_REQUIRE_MYSQL "1" to refuse the SQLite fallback
    HAIRCUT_DB            SQLite file path (local development only)
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import schema

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_MCOLS = ["slug", "name", "source_file", "n_rows", "created", "updated"]

# One row per insert batch is not worth a round trip; one giant INSERT can
# blow past max_allowed_packet on a managed instance. 2000 sits comfortably
# between the two.
_INSERT_BATCH = 2_000

# A hung database must not hold a Streamlit script thread indefinitely.
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 30
_WRITE_TIMEOUT = 30

_DBLOCK = threading.Lock()


class StorageError(RuntimeError):
    """A storage failure with a message safe to show an end user."""


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def configure(values: dict) -> None:
    """Seed configuration from a mapping (e.g. Streamlit secrets).

    Real environment variables always win, so a platform-provided secret is
    never overridden by a file. Call this before the first database use.
    """
    for key, val in (values or {}).items():
        if val is None:
            continue
        os.environ.setdefault(str(key), str(val))


def mysql_config() -> dict | None:
    """The MySQL connection kwargs, or None when MySQL is not configured."""
    host = (os.environ.get("HAIRCUT_DB_HOST") or "").strip()
    if not host:
        return None
    cfg = {
        "host": host,
        "port": int(os.environ.get("HAIRCUT_DB_PORT") or 3306),
        "user": os.environ.get("HAIRCUT_DB_USER") or "root",
        "password": os.environ.get("HAIRCUT_DB_PASSWORD") or "",
        "database": os.environ.get("HAIRCUT_DB_NAME") or "haircut",
    }
    ca = (os.environ.get("HAIRCUT_DB_SSL_CA") or "").strip()
    if ca:
        cfg["ssl"] = {"ca": ca}
    elif _truthy(os.environ.get("HAIRCUT_DB_SSL")):
        # TLS without CA verification: still encrypts the wire, which is what
        # a managed provider requires, but does not authenticate the server.
        cfg["ssl"] = {}
    return cfg


def sqlite_path() -> str:
    """Where the SQLite fallback file lives (local development only)."""
    env = os.environ.get("HAIRCUT_DB")
    if env:
        return env
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [os.path.join(here, "haircut.db"),
                  os.path.join(tempfile.gettempdir(), "haircut.db")]
    for path in candidates:
        folder = os.path.dirname(path) or "."
        try:
            os.makedirs(folder, exist_ok=True)
            probe = os.path.join(folder, ".haircut_write_test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            return path
        except OSError:
            continue
    return candidates[0]


def backend() -> str:
    return "mysql" if mysql_config() else "sqlite"


def target_description() -> str:
    """A human-readable name for the active store, with no credentials in it."""
    cfg = mysql_config()
    if cfg:
        tls = "TLS" if "ssl" in cfg else "no TLS"
        return (f"MySQL {cfg['database']} at {cfg['host']}:{cfg['port']} ({tls})")
    return f"SQLite file {sqlite_path()}"


def check_config() -> list[str]:
    """Configuration problems worth blocking or warning on, most severe first.

    Returned strings are shown to the operator on the diagnostics page.
    """
    problems: list[str] = []
    cfg = mysql_config()
    if cfg is None:
        msg = ("MySQL is not configured (HAIRCUT_DB_HOST is empty), so the app "
               "is using a local SQLite file. On a hosted platform that file "
               "is deleted on every restart and all saved masters are lost.")
        if _truthy(os.environ.get("HAIRCUT_REQUIRE_MYSQL")):
            raise StorageError(
                "HAIRCUT_REQUIRE_MYSQL is set but HAIRCUT_DB_HOST is empty. "
                "Set the database credentials in your secrets, or unset "
                "HAIRCUT_REQUIRE_MYSQL to run on the local SQLite file.")
        problems.append(msg)
        return problems
    if "ssl" not in cfg:
        problems.append(
            "The MySQL connection is not using TLS. Set HAIRCUT_DB_SSL_CA to a "
            "CA bundle (preferred) or HAIRCUT_DB_SSL=1, otherwise credentials "
            "and holding data cross the network unencrypted.")
    if not cfg["password"]:
        problems.append("The MySQL user has an empty password.")
    return problems


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #

class _DBConn:
    """A uniform wrapper over sqlite3 / pymysql.

    The storage functions below use one API and one placeholder style ('?');
    for MySQL the placeholders are translated to '%s'.
    """

    def __init__(self):
        cfg = mysql_config()
        if cfg:
            try:
                import pymysql
            except ImportError as e:
                raise StorageError(
                    "MySQL is configured but the 'pymysql' package is not "
                    "installed. Add PyMySQL to requirements.txt.") from e
            try:
                self._raw = pymysql.connect(
                    charset="utf8mb4", autocommit=False,
                    connect_timeout=_CONNECT_TIMEOUT,
                    read_timeout=_READ_TIMEOUT,
                    write_timeout=_WRITE_TIMEOUT,
                    **cfg)
            except Exception as e:
                raise StorageError(
                    f"Could not connect to {target_description()}: "
                    f"{type(e).__name__}. Check the host, port, credentials "
                    f"and that this server's IP is allowed through the "
                    f"database firewall.") from e
            self._ph = "%s"
        else:
            self._raw = sqlite3.connect(sqlite_path(), timeout=30)
            self._ph = "?"

    def _sql(self, sql: str) -> str:
        return sql if self._ph == "?" else sql.replace("?", self._ph)

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def executemany(self, sql, seq):
        cur = self._raw.cursor()
        cur.executemany(self._sql(sql), seq)
        return cur

    def commit(self):
        self._raw.commit()

    def rollback(self):
        try:
            self._raw.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass


_SCHEMA_READY = False


def _create_schema(c: _DBConn) -> None:
    if backend() == "mysql":
        c.execute("""CREATE TABLE IF NOT EXISTS masters(
            slug VARCHAR(191) PRIMARY KEY,
            name TEXT,
            source_file TEXT,
            n_rows INT,
            created DATETIME,
            updated DATETIME) CHARACTER SET utf8mb4""")
        c.execute("""CREATE TABLE IF NOT EXISTS haircut_rows(
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            slug VARCHAR(191),
            isin VARCHAR(64),
            scheme_name TEXT,
            haircut_pct DOUBLE,
            INDEX ix_rows_slug (slug)) CHARACTER SET utf8mb4""")
        c.execute("""CREATE TABLE IF NOT EXISTS access_log(
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            at DATETIME NOT NULL,
            email VARCHAR(191),
            event VARCHAR(32),
            detail TEXT,
            INDEX ix_log_at (at)) CHARACTER SET utf8mb4""")
    else:
        c.execute("""CREATE TABLE IF NOT EXISTS masters(
            slug TEXT PRIMARY KEY, name TEXT, source_file TEXT,
            n_rows INTEGER, created TEXT, updated TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS haircut_rows(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT, isin TEXT, scheme_name TEXT, haircut_pct REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_rows_slug ON haircut_rows(slug)")
        c.execute("""CREATE TABLE IF NOT EXISTS access_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL, email TEXT, event TEXT, detail TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_log_at ON access_log(at)")
    c.commit()


def _migrate(c: _DBConn) -> list[str]:
    """Bring an already-existing MySQL schema up to the current shape.

    `CREATE TABLE IF NOT EXISTS` silently skips a table that already exists,
    so a database created by an earlier version keeps its old definition. Each
    step below is idempotent and checks before it acts.

    SQLite is not migrated: it cannot add a primary key with ALTER TABLE, and
    it is only used for local development, where a fresh file gets the current
    schema anyway.
    """
    if backend() != "mysql":
        return []
    done: list[str] = []

    # haircut_rows needs a primary key. Without one, MySQL Group Replication
    # and Aurora multi-writer refuse the table, and row-based replication
    # degrades to a full scan per change.
    has_pk = c.execute(
        "SELECT COUNT(*) FROM information_schema.STATISTICS WHERE "
        "TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'haircut_rows' "
        "AND INDEX_NAME = 'PRIMARY'").fetchone()[0]
    if not has_pk:
        c.execute("ALTER TABLE haircut_rows "
                  "ADD COLUMN id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY "
                  "FIRST")
        c.commit()
        done.append("added a primary key to haircut_rows")

    # Timestamps belong in DATETIME columns so they can be filtered and
    # indexed as dates. Existing 'YYYY-MM-DD HH:MM' strings convert cleanly.
    kind = c.execute(
        "SELECT DATA_TYPE FROM information_schema.COLUMNS WHERE "
        "TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'masters' "
        "AND COLUMN_NAME = 'created'").fetchone()
    if kind and str(kind[0]).lower().startswith("varchar"):
        try:
            # Blank out anything that would not survive the cast, so the
            # ALTER cannot fail halfway under strict mode.
            for col in ("created", "updated"):
                c.execute(
                    f"UPDATE masters SET {col} = NULL WHERE {col} IS NOT NULL "
                    f"AND STR_TO_DATE({col}, '%%Y-%%m-%%d %%H:%%i:%%s') IS NULL "
                    f"AND STR_TO_DATE({col}, '%%Y-%%m-%%d %%H:%%i') IS NULL")
            c.execute("ALTER TABLE masters "
                      "MODIFY created DATETIME NULL, "
                      "MODIFY updated DATETIME NULL")
            c.commit()
            done.append("converted masters.created/updated to DATETIME")
        except Exception:
            # Not worth failing startup over: the VARCHAR form still sorts
            # correctly because the timestamps are zero-padded and ISO-like.
            c.rollback()
            done.append("could not convert masters timestamps to DATETIME "
                        "(left as text, which still works)")
    return done


MIGRATIONS: list[str] = []


def init_schema() -> None:
    """Create and migrate the tables once per process, not on every query."""
    global _SCHEMA_READY
    with _DBLOCK:
        if _SCHEMA_READY:
            return
        with closing(_DBConn()) as c:
            _create_schema(c)
            MIGRATIONS[:] = _migrate(c)
        _SCHEMA_READY = True


def _conn() -> _DBConn:
    if not _SCHEMA_READY:
        init_schema()
    return _DBConn()


def ping() -> None:
    """Raise StorageError if the database is unreachable."""
    with closing(_conn()) as c:
        c.execute("SELECT 1").fetchone()


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_") or "master"


def _to_meta(row) -> dict:
    meta = dict(zip(_MCOLS, row))
    for key in ("created", "updated"):
        val = meta.get(key)
        if isinstance(val, datetime):
            meta[key] = val.strftime("%Y-%m-%d %H:%M UTC")
        elif val:
            meta[key] = f"{val} UTC"
    return meta


def list_masters() -> list[dict]:
    """All saved masters (meta dicts), newest-updated first."""
    with closing(_conn()) as c:
        rows = c.execute("SELECT slug,name,source_file,n_rows,created,updated "
                         "FROM masters ORDER BY updated DESC").fetchall()
    return [_to_meta(r) for r in rows]


def get_meta(slug: str) -> dict | None:
    with closing(_conn()) as c:
        row = c.execute("SELECT slug,name,source_file,n_rows,created,updated "
                        "FROM masters WHERE slug=?", (slug,)).fetchone()
    return _to_meta(row) if row else None


def load_master(slug: str) -> pd.DataFrame:
    """The standardised haircut table for a saved master."""
    with closing(_conn()) as c:
        rows = c.execute("SELECT isin,scheme_name,haircut_pct "
                         "FROM haircut_rows WHERE slug=?", (slug,)).fetchall()
    if not rows:
        return schema.empty_haircut()
    df = pd.DataFrame(rows, columns=schema.STD_HAIRCUT_COLS)
    df["isin"] = df["isin"].fillna("").astype(str)
    df["scheme_name"] = df["scheme_name"].fillna("").astype(str)
    df["haircut_pct"] = pd.to_numeric(df["haircut_pct"], errors="coerce")
    return df[schema.STD_HAIRCUT_COLS]


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

def save_master(name: str, source_file: str, std_df: pd.DataFrame,
                actor: str | None = None) -> dict:
    """Upsert by name: replace the master's rows with `std_df`, atomically.

    Auditing happens here rather than at the call site, so a future caller
    cannot change the library without the change being recorded.
    """
    slug = slugify(name)
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    df = std_df[schema.STD_HAIRCUT_COLS]
    records = [(slug,
                "" if pd.isna(r[0]) else str(r[0])[:64],
                "" if pd.isna(r[1]) else str(r[1]),
                None if pd.isna(r[2]) else float(r[2]))
               for r in df.itertuples(index=False, name=None)]

    is_mysql = backend() == "mysql"
    if is_mysql:
        upsert = ("INSERT INTO masters(slug,name,source_file,n_rows,created,updated)"
                  " VALUES(?,?,?,?,?,?) ON DUPLICATE KEY UPDATE "
                  "name=VALUES(name), source_file=VALUES(source_file), "
                  "n_rows=VALUES(n_rows), updated=VALUES(updated)")
    else:
        upsert = ("INSERT INTO masters(slug,name,source_file,n_rows,created,updated)"
                  " VALUES(?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
                  "name=excluded.name, source_file=excluded.source_file, "
                  "n_rows=excluded.n_rows, updated=excluded.updated")
    stamp = now if is_mysql else now.strftime("%Y-%m-%d %H:%M:%S")

    with _DBLOCK, closing(_conn()) as c:
        try:
            prev = c.execute("SELECT created FROM masters WHERE slug=?",
                             (slug,)).fetchone()
            existed = prev is not None
            created = prev[0] if prev else stamp
            c.execute("DELETE FROM haircut_rows WHERE slug=?", (slug,))
            for start in range(0, len(records), _INSERT_BATCH):
                c.executemany(
                    "INSERT INTO haircut_rows(slug,isin,scheme_name,haircut_pct)"
                    " VALUES(?,?,?,?)",
                    records[start:start + _INSERT_BATCH])
            c.execute(upsert, (slug, (name or "").strip(), source_file,
                               int(len(df)), created, stamp))
            c.commit()
        except StorageError:
            c.rollback()
            raise
        except Exception as e:
            c.rollback()
            raise StorageError(
                f"Could not save '{name}' to {target_description()}: "
                f"{type(e).__name__}. Nothing was changed.") from e
    meta = get_meta(slug) or {"slug": slug, "name": (name or "").strip(),
                              "source_file": source_file,
                              "n_rows": int(len(df))}
    log_event(actor, MASTER_REPLACED if existed else MASTER_SAVED,
              f"{meta['name']} ({meta['n_rows']:,} records) from {source_file}")
    return meta


# --------------------------------------------------------------------------- #
# Access log
# --------------------------------------------------------------------------- #
#
# Records who signed in and who changed a haircut master. Deliberately never
# records holdings, client codes or portfolio values: this is a margin tool
# holding client data, and an audit trail must not become a second copy of it.

SIGN_IN = "sign_in"
MASTER_SAVED = "master_saved"
MASTER_REPLACED = "master_replaced"
MASTER_DELETED = "master_deleted"

EVENT_LABELS = {
    SIGN_IN: "Signed in",
    "admin_unlock": "Unlocked admin",
    "admin_failed": "Failed admin password",
    MASTER_SAVED: "Added a master",
    MASTER_REPLACED: "Replaced a master",
    MASTER_DELETED: "Deleted a master",
}


def log_event(email: str | None, event: str, detail: str = "") -> None:
    """Append one audit row. Never raises.

    Logging is not worth failing a user's action over, so a storage problem
    here is swallowed. The events that matter are also visible in the data
    itself (a master's `updated` timestamp, for instance).
    """
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        stamp = now if backend() == "mysql" else now.strftime("%Y-%m-%d %H:%M:%S")
        with closing(_conn()) as c:
            c.execute("INSERT INTO access_log(at,email,event,detail) "
                      "VALUES(?,?,?,?)",
                      (stamp, (email or "unknown")[:191], event[:32],
                       (detail or "")[:500]))
            c.commit()
    except Exception:
        pass


def recent_events(limit: int = 200) -> list[dict]:
    """The most recent audit rows, newest first."""
    limit = max(1, min(int(limit), 2000))
    with closing(_conn()) as c:
        rows = c.execute(
            f"SELECT at,email,event,detail FROM access_log "
            f"ORDER BY at DESC, id DESC LIMIT {limit}").fetchall()
    out = []
    for at, email, event, detail in rows:
        if isinstance(at, datetime):
            at = at.strftime("%Y-%m-%d %H:%M")
        out.append({"when_utc": str(at), "who": email,
                    "what": EVENT_LABELS.get(event, event), "detail": detail})
    return out


def prune_events(keep_days: int = 365) -> int:
    """Drop audit rows older than `keep_days`. Returns how many went."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) \
        - timedelta(days=int(keep_days))
    stamp = cutoff if backend() == "mysql" else cutoff.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with closing(_conn()) as c:
            cur = c.execute("DELETE FROM access_log WHERE at < ?", (stamp,))
            n = cur.rowcount or 0
            c.commit()
        return n
    except Exception:
        return 0


def delete_master(slug: str, actor: str | None = None) -> None:
    """Remove a master and all of its rows. Audited here, not at the call site."""
    gone = get_meta(slug)
    with _DBLOCK, closing(_conn()) as c:
        try:
            c.execute("DELETE FROM haircut_rows WHERE slug=?", (slug,))
            c.execute("DELETE FROM masters WHERE slug=?", (slug,))
            c.commit()
            log_event(actor, MASTER_DELETED,
                      f"{gone['name']} ({gone['n_rows']:,} records)"
                      if gone else slug)
        except Exception as e:
            c.rollback()
            raise StorageError(
                f"Could not delete '{slug}' from {target_description()}: "
                f"{type(e).__name__}. Nothing was changed.") from e
