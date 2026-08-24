"""
Copy the haircut library from one database to another.

Reads every saved master from a source database and writes it to a target,
reusing the app's own storage layer - so the target gets the current schema,
the batched inserts and the primary key, not a raw table dump.

Usage
-----
Set the SOURCE with the normal HAIRCUT_DB_* variables and the TARGET with
HAIRCUT_TARGET_* equivalents, then:

    python tools/migrate_db.py            # copy, refusing to overwrite
    python tools/migrate_db.py --replace  # overwrite masters of the same name
    python tools/migrate_db.py --dry-run  # report what would move, change nothing

Example (local MySQL -> a managed instance):

    HAIRCUT_DB_HOST=127.0.0.1 \
    HAIRCUT_DB_USER=root \
    HAIRCUT_DB_PASSWORD=... \
    HAIRCUT_DB_NAME=haircut \
    HAIRCUT_TARGET_HOST=mysql-xxxx.aivencloud.com \
    HAIRCUT_TARGET_PORT=23456 \
    HAIRCUT_TARGET_USER=avnadmin \
    HAIRCUT_TARGET_PASSWORD=... \
    HAIRCUT_TARGET_NAME=haircut \
    HAIRCUT_TARGET_SSL_CA=certs/mysql-ca.pem \
    python tools/migrate_db.py

Nothing is written to the source. Re-running is safe: without --replace a
master that already exists on the target is skipped.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from haircut_core import store  # noqa: E402

SOURCE_KEYS = ("HAIRCUT_DB_HOST", "HAIRCUT_DB_PORT", "HAIRCUT_DB_USER",
               "HAIRCUT_DB_PASSWORD", "HAIRCUT_DB_NAME", "HAIRCUT_DB_SSL",
               "HAIRCUT_DB_SSL_CA", "HAIRCUT_DB")


def _snapshot(keys) -> dict:
    return {k: os.environ.get(k) for k in keys}


def _apply(values: dict) -> None:
    for key, val in values.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(val)
    # the storage layer caches its schema check per process
    store._SCHEMA_READY = False


def _target_env() -> dict:
    """Map HAIRCUT_TARGET_* onto the names the storage layer reads."""
    host = (os.environ.get("HAIRCUT_TARGET_HOST") or "").strip()
    if not host:
        sys.exit("HAIRCUT_TARGET_HOST is not set. See the docstring for usage.")
    env = {
        "HAIRCUT_DB_HOST": host,
        "HAIRCUT_DB_PORT": os.environ.get("HAIRCUT_TARGET_PORT") or "3306",
        "HAIRCUT_DB_USER": os.environ.get("HAIRCUT_TARGET_USER") or "root",
        "HAIRCUT_DB_PASSWORD": os.environ.get("HAIRCUT_TARGET_PASSWORD") or "",
        "HAIRCUT_DB_NAME": os.environ.get("HAIRCUT_TARGET_NAME") or "haircut",
        "HAIRCUT_DB_SSL": os.environ.get("HAIRCUT_TARGET_SSL") or None,
        "HAIRCUT_DB_SSL_CA": os.environ.get("HAIRCUT_TARGET_SSL_CA") or None,
        "HAIRCUT_DB": None,
    }
    return env


def main() -> int:
    replace = "--replace" in sys.argv
    dry_run = "--dry-run" in sys.argv

    source_env = _snapshot(SOURCE_KEYS)
    target_env = _target_env()

    # ---- read everything from the source first, then switch over ----
    _apply(source_env)
    print(f"source: {store.target_description()}")
    try:
        metas = store.list_masters()
    except Exception as e:
        return f"could not read the source: {type(e).__name__}: {e}"
    if not metas:
        print("the source has no saved masters - nothing to copy")
        return 0

    payload = []
    for meta in metas:
        df = store.load_master(meta["slug"])
        payload.append((meta, df))
        print(f"  read  {meta['name']!r}: {len(df):,} rows")

    # ---- write to the target ----
    _apply(target_env)
    print(f"target: {store.target_description()}")
    try:
        store.init_schema()
        if store.MIGRATIONS:
            for step in store.MIGRATIONS:
                print(f"  schema: {step}")
        existing = {m["name"].lower(): m for m in store.list_masters()}
    except Exception as e:
        return f"could not reach the target: {type(e).__name__}: {e}"

    copied = skipped = 0
    for meta, df in payload:
        name = meta["name"]
        prior = existing.get(name.lower())
        if prior and not replace:
            print(f"  SKIP  {name!r}: already on the target with "
                  f"{prior['n_rows']:,} rows (use --replace to overwrite)")
            skipped += 1
            continue
        if dry_run:
            verb = "would replace" if prior else "would copy"
            print(f"  {verb} {name!r}: {len(df):,} rows")
            copied += 1
            continue
        written = store.save_master(name, meta["source_file"], df)
        print(f"  {'replaced' if prior else 'copied  '} {name!r}: "
              f"{written['n_rows']:,} rows")
        copied += 1

    # ---- verify ----
    if not dry_run and copied:
        print("\nverifying:")
        for meta, df in payload:
            slug = store.slugify(meta["name"])
            back = store.load_master(slug)
            if len(back) != len(df):
                print(f"  MISMATCH {meta['name']!r}: "
                      f"source {len(df):,} vs target {len(back):,}")
            else:
                a = df.sort_values(["isin", "scheme_name"]).reset_index(drop=True)
                b = back.sort_values(["isin", "scheme_name"]).reset_index(drop=True)
                same = a["haircut_pct"].equals(b["haircut_pct"])
                print(f"  ok {meta['name']!r}: {len(back):,} rows, "
                      f"values match: {same}")

    print(f"\n{'would copy' if dry_run else 'copied'} {copied}, skipped {skipped}")
    return 0


if __name__ == "__main__":
    result = main()
    if isinstance(result, str):
        sys.exit(f"ERROR: {result}")
    sys.exit(result)
