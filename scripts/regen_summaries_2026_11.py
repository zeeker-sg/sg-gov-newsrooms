#!/usr/bin/env python3
"""Regenerate summaries for rows fixed by fix_outstanding_data_2026_11.py.

Run from the host shell, NOT the build container: .env points LLM_BASE_URL at
http://host.docker.internal:11434/v1 which only resolves inside the container.
From the host, override with localhost:

    set -a; source .env; set +a
    export LLM_BASE_URL=http://localhost:11434/v1
    uv run python scripts/regen_summaries_2026_11.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "resources"))

import agc_news  # noqa: E402
import ccs_news  # noqa: E402
import judiciary_news  # noqa: E402
import mlaw_news  # noqa: E402
import mom_news  # noqa: E402

DB = PROJECT_DIR / "sg-gov-newsrooms.db"

# (table, module, id) — content backfilled by the fix script; summaries are
# either title-only (generated while content_text was empty) or empty.
ROWS = [
    ("mom_news", mom_news, "16100ea76989"),
    ("mom_news", mom_news, "19218595cb85"),
    ("mom_news", mom_news, "42c70c448bea"),
    ("mom_news", mom_news, "00f0dda6522b"),
    ("mlaw_news", mlaw_news, "da90e322d151"),
    ("judiciary_news", judiciary_news, "18c22eb7360f"),
    ("ccs_news", ccs_news, "a06180140c64"),
    ("agc_news", agc_news, "c5244ea73033"),
    ("agc_news", agc_news, "fc30b8b83074"),
    ("agc_news", agc_news, "7bd6693b445d"),
]


def main() -> None:
    conn = sqlite3.connect(str(DB), timeout=30)
    conn.execute("PRAGMA busy_timeout=10000")
    for table, mod, rid in ROWS:
        row = conn.execute(
            f"SELECT title, content_text, summary FROM {table} WHERE id=?", (rid,)
        ).fetchone()
        if row is None:
            print(f"{table}/{rid}: NOT FOUND — skipped")
            continue
        title, content, old_sum = row
        if not content:
            print(f"{table}/{rid}: no content — skipped")
            continue
        try:
            new_sum = asyncio.run(mod.get_summary(content, title))
        except Exception as e:  # noqa: BLE001
            print(f"{table}/{rid}: FAILED {type(e).__name__}: {e}")
            continue
        if new_sum:
            conn.execute(f"UPDATE {table} SET summary=? WHERE id=?", (new_sum, rid))
            conn.commit()
            print(f"{table}/{rid}: summary {len(old_sum or '')} -> {len(new_sum)} chars  ({title[:40]!r})")
        else:
            print(f"{table}/{rid}: LLM returned empty — old summary kept")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    print("done")


if __name__ == "__main__":
    main()
