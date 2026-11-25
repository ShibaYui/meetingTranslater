#!/usr/bin/env python3
"""Export a saved transcript (.jsonl) to the plain-text layout the terminal shows while running:

    [01:43:28] They came to the judgment that they would like to
    → 他們做出判斷，表示他們希望
       seg 2.8s · asr 0.5s · 翻譯 0.6s (首字 0.2s)

    python3 tools/export_transcript.py transcripts/20260101-0900.jsonl            # writes transcripts/20260101-0900.txt
    python3 tools/export_transcript.py transcripts/20260101-0900.jsonl -o out.txt

live_subs.py writes this .txt itself during a session. Use this tool for .jsonl files recorded by older versions
(their records have no tr_first_ms, so the "(首字 …)" part is omitted) or to rebuild a deleted .txt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from live_subs import format_transcript_entry  # noqa: E402


def entry_from_record(rec: dict) -> str:
    """Format one .jsonl record; tolerates records written before tr_first_ms / null tr_ms existed."""
    time_str = str(rec.get("time", ""))[11:19] or "??:??:??"
    zh = rec.get("zh") or ""
    tr_ms = rec.get("tr_ms")
    if tr_ms is None or (not zh and not tr_ms):  # no translation recorded for this sentence
        tr_ms = None
    tr_first_ms = rec.get("tr_first_ms") if tr_ms is not None else None
    seg_s = float(rec.get("end", 0.0)) - float(rec.get("start", 0.0))
    return format_transcript_entry(time_str, str(rec.get("en", "")), zh, seg_s, float(rec.get("asr_ms", 0)),
                                   tr_ms, tr_first_ms)


def main() -> int:
    p = argparse.ArgumentParser(description="Convert a transcript .jsonl into the terminal-style .txt layout")
    p.add_argument("jsonl", type=Path, help="transcripts/<date>-<time>.jsonl written by live_subs.py")
    p.add_argument("-o", "--output", type=Path, help="output file (default: same name with .txt)")
    p.add_argument("--force", action="store_true", help="overwrite the output file if it exists")
    a = p.parse_args()

    out = a.output or a.jsonl.with_suffix(".txt")
    if out.exists() and not a.force:
        print(f"{out} already exists; pass --force to overwrite it", file=sys.stderr)
        return 1
    try:
        lines = a.jsonl.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        print(f"cannot read {a.jsonl}: {e}", file=sys.stderr)
        return 1

    entries: list[str] = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entries.append(entry_from_record(json.loads(line)))
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            print(f"{a.jsonl}:{n}: skipped unreadable record ({e})", file=sys.stderr)
    out.write_text("".join(entries), encoding="utf-8")
    print(f"Wrote {len(entries)} segments to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
