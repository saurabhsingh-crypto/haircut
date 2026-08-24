"""
Point the app at a managed MySQL, in one step.

Takes the connection URI your provider gives you (Aiven calls it the "Service
URI"), checks it actually connects, writes it into the local and production
secrets files, and optionally copies your existing library across.

Usage
-----
    python tools/setup_db.py "mysql://user:pass@host:12345/defaultdb?ssl-mode=REQUIRED"
    python tools/setup_db.py "<uri>" --ca certs/mysql-ca.pem
    python tools/setup_db.py "<uri>" --migrate        # also copy the library over
    python tools/setup_db.py "<uri>" --check-only     # test the connection, write nothing

Pass --ca with your provider's CA certificate to verify the server, not just
encrypt the channel. Without it the connection is still encrypted (ssl-mode
REQUIRED) but the server is unverified.

Neither secrets file is ever committed - both are covered by .gitignore.
"""
from __future__ import annotations

import io
import os
import re
import sys
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

LOCAL_SECRETS = os.path.join(HERE, ".streamlit", "secrets.toml")
PROD_SECRETS = os.path.join(HERE, ".streamlit", "secrets.toml.production")


def parse_uri(uri: str) -> dict:
    """Split a mysql:// connection URI into the app's settings."""
    if "://" not in uri:
        sys.exit("That does not look like a connection URI. It should start "
                 "with mysql:// - copy the 'Service URI' from your provider.")
    parsed = urlparse(uri)
    if parsed.scheme not in ("mysql", "mysqls"):
        sys.exit(f"Unsupported scheme {parsed.scheme!r}; expected mysql://")
    if not parsed.hostname:
        sys.exit("The URI has no host in it.")
    database = (parsed.path or "/").lstrip("/") or "defaultdb"
    return {
        "HAIRCUT_DB_HOST": parsed.hostname,
        "HAIRCUT_DB_PORT": str(parsed.port or 3306),
        "HAIRCUT_DB_USER": unquote(parsed.username or "root"),
        "HAIRCUT_DB_PASSWORD": unquote(parsed.password or ""),
        "HAIRCUT_DB_NAME": database,
    }


def apply_env(settings: dict, ca: str | None) -> None:
    for key, val in settings.items():
        os.environ[key] = val
    os.environ.pop("HAIRCUT_DB", None)
    if ca:
        os.environ["HAIRCUT_DB_SSL_CA"] = ca
        os.environ.pop("HAIRCUT_DB_SSL", None)
    else:
        os.environ["HAIRCUT_DB_SSL"] = "1"
        os.environ.pop("HAIRCUT_DB_SSL_CA", None)


def rewrite(path: str, settings: dict, ca: str | None) -> bool:
    """Replace the HAIRCUT_DB_* lines in a secrets file, in place."""
    if not os.path.exists(path):
        print(f"  skipped {os.path.basename(path)} (not present)")
        return False
    text = io.open(path, encoding="utf-8").read()

    for key, val in settings.items():
        pattern = rf'(?m)^{key}\s*=\s*".*"$'
        line = f'{key} = "{val}"'
        text = (re.sub(pattern, line, text) if re.search(pattern, text)
                else text)

    # TLS: exactly one of the two, and never commented out
    text = re.sub(r'(?m)^#?\s*HAIRCUT_DB_SSL_CA\s*=.*$', "", text)
    text = re.sub(r'(?m)^#?\s*HAIRCUT_DB_SSL\s*=.*$', "", text)
    tls_line = (f'HAIRCUT_DB_SSL_CA = "{ca}"' if ca else 'HAIRCUT_DB_SSL = "1"')
    anchor = f'HAIRCUT_DB_NAME = "{settings["HAIRCUT_DB_NAME"]}"'
    text = text.replace(anchor, f"{anchor}\n{tls_line}", 1)

    # a hosted database means the SQLite fallback should never be used
    if not re.search(r'(?m)^HAIRCUT_REQUIRE_MYSQL\s*=', text):
        text = re.sub(r'(?m)^#\s*HAIRCUT_REQUIRE_MYSQL\s*=.*$',
                      'HAIRCUT_REQUIRE_MYSQL = "1"', text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    io.open(path, "w", encoding="utf-8", newline="\n").write(text)
    print(f"  wrote {os.path.basename(path)}")
    return True


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if not args or args[0].startswith("--"):
        sys.exit(__doc__.strip())
    uri = args[0]
    ca = None
    if "--ca" in args:
        ca = args[args.index("--ca") + 1]
        if not os.path.exists(os.path.join(HERE, ca)) and not os.path.exists(ca):
            sys.exit(f"CA file not found: {ca}")
    check_only = "--check-only" in args
    migrate = "--migrate" in args

    settings = parse_uri(uri)
    print("parsed connection details:")
    for key, val in settings.items():
        shown = "*" * 8 if "PASSWORD" in key else val
        print(f"  {key:22s} {shown}")
    print(f"  TLS                    {'CA-verified via ' + ca if ca else 'encrypted, unverified'}")

    # keep the current (source) settings before switching over
    source = {k: os.environ.get(k) for k in
              ("HAIRCUT_DB_HOST", "HAIRCUT_DB_PORT", "HAIRCUT_DB_USER",
               "HAIRCUT_DB_PASSWORD", "HAIRCUT_DB_NAME")}

    apply_env(settings, ca)
    from haircut_core import store
    store._SCHEMA_READY = False

    print(f"\nconnecting to {store.target_description()} ...")
    try:
        store.ping()
    except Exception as e:
        return (f"could not connect: {type(e).__name__}: {e}\n"
                f"Check the URI, and that your provider allows connections "
                f"from this machine.")
    print("  connected")

    try:
        store.init_schema()
        for step in store.MIGRATIONS:
            print(f"  schema: {step}")
        masters = store.list_masters()
        print(f"  tables ready; {len(masters)} master(s) already there")
    except Exception as e:
        return f"connected, but could not prepare the schema: {type(e).__name__}: {e}"

    if check_only:
        print("\n--check-only: nothing written")
        return 0

    print("\nupdating secrets files:")
    rewrite(LOCAL_SECRETS, settings, ca)
    rewrite(PROD_SECRETS, settings, ca)

    if migrate:
        print("\ncopying the library across:")
        for key, val in source.items():
            if val:
                os.environ[f"HAIRCUT_TARGET_{key[len('HAIRCUT_DB_'):]}"] = \
                    settings[key]
        # simplest reliable path: hand off to the tested migrator
        target = {f"HAIRCUT_TARGET_{k[len('HAIRCUT_DB_'):]}": v
                  for k, v in settings.items()}
        if ca:
            target["HAIRCUT_TARGET_SSL_CA"] = ca
        else:
            target["HAIRCUT_TARGET_SSL"] = "1"
        env = dict(os.environ)
        env.update({k: (v or "") for k, v in source.items() if v})
        env.update(target)
        import subprocess
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "tools", "migrate_db.py")],
            env=env, capture_output=True, text=True)
        print(proc.stdout.strip() or proc.stderr.strip()[-800:])
        if proc.returncode != 0:
            return "the migration step failed - see the output above"

    print("\nDone. Paste the contents of .streamlit/secrets.toml.production "
          "into\nManage app -> Settings -> Secrets, then reboot the app.")
    return 0


if __name__ == "__main__":
    result = main()
    if isinstance(result, str):
        sys.exit(f"ERROR: {result}")
    sys.exit(result)
