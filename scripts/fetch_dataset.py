"""Fetch a dataset from the organizers' temporary API (GET with group_id) and check it with the ingest.

    python scripts/fetch_dataset.py DEV            -> data/raw/dev_records.json
    python scripts/fetch_dataset.py <GROUP_ID>     -> data/raw/<group_id>_records.json

The base URL is the "Открыть GET для DEV" link from the Student Guide without group_id, kept in .env as CQA_DATA_API
(never in Git). The raw records are saved as a plain list; ``cqa.ingest`` normalizes them (65/65 on DEV).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cqa import ingest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("group_id", nargs="?", default="DEV")
    ap.add_argument("--no-save", action="store_true", help="only fetch and validate")
    args = ap.parse_args(argv)
    try:
        convs = ingest.fetch_group(args.group_id, save=not args.no_save)
    except ingest.IngestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    by = Counter((c["channel"], c["language"]) for c in convs)
    calls = [c for c in convs if c["channel"] == "call"]
    print(f"{len(convs)} conversations for group {args.group_id}: " + ", ".join(f"{ch}/{lang}={n}" for (ch, lang), n in sorted(by.items())))
    for c in calls:
        m = c["meta"]
        print(f"  call {c['id']}: {len(c['messages'])} messages, recording from {m.get('recording_start_sec')} s, "
              f"pauses {m.get('recording_gaps')}, ~{m.get('recording_duration_sec')} s recorded")
    warnings = [w for c in convs for w in c["meta"].get("warnings", [])]
    for w in warnings:
        print("  warning:", w)
    if not args.no_save:
        print(f"saved -> {ingest.raw_path(args.group_id).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
